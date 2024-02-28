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
with open("WIFI_CREDENTIALS") as f:
    WIFI_SSID, WIFI_PASSWORD = f.read().strip().split(":")
with open("melaan-ca.crt", "rb") as f:
    CA_DATA = f.read()


# https://stackoverflow.com/a/56613595
def get_ntp():
    REF_TIME_1970 = 2208988800  # Reference time
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(3)
    try:
        query = b"\x1b" + 47 * b"\0"
        sockaddr = socket.getaddrinfo("pool.ntp.org", 123)[0][-1]
        client.sendto(query, sockaddr)
        resp = client.recv(1024)
    finally:
        client.close()
    secs = struct.unpack("!12I", resp)[10] - REF_TIME_1970
    return time.gmtime(secs)


def struct_time_to_rtc(struct):
    year, month, day, hour, minute, second, wday, yday = struct
    _ = yday
    return year, month, day, wday, hour, minute, second, 0


try:
    import machine

    onboard = True
except ImportError:
    onboard = False
    ssl_context = ssl.create_default_context(cafile="melaan-ca.crt")
    ssl_wrapper = lambda sock: ssl_context.wrap_socket(
        sock, server_hostname="melaan-server"
    )
else:
    ssl_wrapper = lambda sock: ssl.wrap_socket(sock)


class Connection:
    def __init__(self, addr, method, path, headers, recv_callback):
        ip, port = addr.split(":")
        port = int(port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        sock.connect((ip, port))
        sock.setblocking(True)
        self.sock = ssl_wrapper(sock)
        self.sock.write(f"{method} {path} HTTP/1.1\r\n".encode())
        self.sock.write(f"Host: {addr}\r\n".encode())
        for key, val in headers.items():
            self.sock.write(f"{key}: {val}\r\n".encode())
        self.sock.write(b"\r\n")
        self._recv_callback = recv_callback
        self._lock = _thread.allocate_lock()
        _thread.start_new_thread(self._recv_loop, ())

    def _recv_loop(self):
        found_http_statusline = False
        found_http_body = False
        buf = b""
        while True:
            try:
                char = self.sock.read(1)
                buf += char
                if not char:
                    time.sleep(0.5)
            except Exception as e:
                print(f"failed to receive data, closing: {e}")
                self.sock.close()
                return
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
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
            self.sock.write((line + "\n").encode())


class Controller:
    def __init__(self):
        self.close_callback = None
        self.last_server_ok = time.time()

    def recv(self, line):
        if line == "server ok":
            print("got server ok")
            self.last_server_ok = time.time()
            return
        if line == "open sesame":
            print("would open door")
            return
        print("unexpected message from server:", line)


if onboard:
    import network

    nic = network.WLAN(network.STA_IF)
    print("activating network card")
    nic.active(True)
    print("connecting to WLAN")
    nic.connect(WIFI_SSID, WIFI_PASSWORD)
    while (status := nic.status()) != network.STAT_GOT_IP:
        print(f"waiting for wifi connectivity, status {status}")
        time.sleep(1)
    print("confirming online status")
    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            sock.connect(("9.9.9.9", 53))
        except Exception as e:
            print(f"not online, retrying: {e}")
            time.sleep(5)
        else:
            break
    print("syncing rtc to ntp")
    while True:
        try:
            machine.RTC().datetime(struct_time_to_rtc(get_ntp()))
        except Exception as e:
            print(f"failed to sync ntp, retrying: {e}")
            time.sleep(5)
        else:
            break
    print("board online")


ctl = Controller()
while True:
    try:
        print("starting connection")
        conn = Connection(
            SERVER_ADDRESS,
            "PUT",
            "/api/v0/controller/register",
            {"Authorization": f"MeLaan {CONTROLLER_PASSWORD}", "Upgrade": "MeLaan"},
            ctl.recv,
        )
        print("connected successfully")
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
            print(e)
            try:
                conn.sock.close()
            except Exception:
                pass
            break
        if time.time() - ctl.last_server_ok > 5:
            print("connection became stale, closing")
            try:
                conn.sock.close()
            except Exception:
                pass
    time.sleep(1)
