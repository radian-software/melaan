import _thread
import socket
import ssl
import struct
import time

# These files should be created manually on the micropython filesystem
with open("CONTROLLER_PASSWORD") as f:
    CONTROLLER_PASSWORD = f.read().strip()
with open("SERVER_ADDRESS") as f:
    SERVER_ADDRESS = f.read().strip()


ssl_context = ssl.create_default_context(cafile="melaan-ca.crt")


# https://stackoverflow.com/a/56613595
def get_ntp():
    REF_TIME_1970 = 2208988800  # Reference time
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    query = b"\x1b" + 47 * b"\0"
    sockaddr = socket.getaddrinfo("pool.ntp.org", 123)[0][-1]
    client.sendto(query, sockaddr)
    resp = client.recv(1024)
    client.close()
    secs = struct.unpack("!12I", resp)[10] - REF_TIME_1970
    return time.gmtime(secs)


try:
    import machine
except ImportError:
    pass
else:
    machine.RTC().datetime(get_ntp())


class Connection:
    def __init__(self, addr, method, path, headers, recv_callback):
        ip, port = addr.split(":")
        port = int(port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        sock.connect((ip, port))
        sock.setblocking(True)
        self.sock = ssl_context.wrap_socket(sock, server_hostname="melaan-server")
        self.sock.sendall(f"{method} {path} HTTP/1.1\r\n".encode())
        self.sock.sendall(f"Host: {addr}\r\n".encode())
        for key, val in headers.items():
            self.sock.sendall(f"{key}: {val}\r\n".encode())
        self.sock.sendall(b"\r\n")
        self._recv_callback = recv_callback
        self._lock = _thread.allocate_lock()
        _thread.start_new_thread(self._recv_loop, ())

    def _recv_loop(self):
        found_http_statusline = False
        found_http_body = False
        buf = b""
        while True:
            try:
                buf += self.sock.recv(1024)
            except Exception as e:
                print(f"failed to receive data, closing: {e}")
                self.sock.close()
                return
            while b"\n" in buf:
                line, buf = buf.split(b"\n", maxsplit=1)
                if not found_http_statusline:
                    print("found http statusline")
                    assert line.startswith(b"HTTP/1.1 101 "), line
                    found_http_statusline = True
                    continue
                if not found_http_body:
                    if line == b"\r":
                        print("found end of http headers")
                        found_http_body = True
                    continue
                self._recv_callback(line.decode())

    def send(self, line):
        with self._lock:
            self.sock.sendall((line + "\n").encode())


class Controller:
    def __init__(self):
        self.close_callback = None

    def recv(self, line):
        if line == "server ok":
            print("got server ok")
            return
        if line == "open sesame":
            print("would open door")
            return
        print("unexpected message from server:", line)


ctl = Controller()
while True:
    print("starting connection")
    try:
        conn = Connection(
            SERVER_ADDRESS,
            "PUT",
            "/api/v0/controller/register",
            {"Authorization": f"MeLaan {CONTROLLER_PASSWORD}", "Upgrade": "MeLaan"},
            ctl.recv,
        )
    except Exception as e:
        print(f"failed to connect, {e}")
        time.sleep(1)
        continue
    time.sleep(0.5)
    while True:
        try:
            print("sending client ok")
            conn.send("client ok")
            time.sleep(1)
        except Exception as e:
            print(f"failed to send client ok, closing: {e}")
            try:
                conn.sock.close()
            except Exception:
                pass
            break
    time.sleep(1)
