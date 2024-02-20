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


def keep_ntp_current():
    try:
        import machine
    except ImportError:
        return
    while True:
        machine.RTC().datetime(get_ntp())
        time.sleep(3600)


class Connection:
    def __init__(self, addr, method, path, headers, recv_callback):
        ip, port = addr.split(":")
        port = int(port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((ip, port))
        self.sock.setblocking(True)
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
            buf += self.sock.recv(1024)
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
        self.last_server_ok = 0

    def recv(self, line):
        if line == "server ok":
            print("got server ok")
            self.last_server_ok = time.time()
            return
        if line == "open sesame":
            print("would open door")
            return
        print("unexpected message from server:", line)

    def monitor_loop(self):
        while True:
            time.sleep(1)
            if time.time() - self.last_server_ok > 3 and self.close_callback:
                print("dead connection, closing")
                try:
                    cb = self.close_callback
                    self.close_callback = None
                    cb()
                except Exception:
                    pass


_thread.start_new_thread(keep_ntp_current, ())


ctl = Controller()
_thread.start_new_thread(ctl.monitor_loop, ())
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
        ctl.close_callback = conn.sock.close
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
            print(f"failed to send client ok, {e}")
            break
    time.sleep(1)
