import _thread
import binascii
import hashlib
import socket
import ssl
import struct
import time

# These files should be created manually on the micropython filesystem
with open("CONTROLLER_PASSWORD") as f:
    CONTROLLER_PASSWORD = binascii.a2b_base64(f.read().strip())
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
        sockaddr = socket.getaddrinfo(ip, port)[0][-1]
        self.sock = socket.socket()
        self.sock.setblocking(True)
        self.sock.bind(sockaddr)
        self.sock.sendall(f"{method} {path} HTTP/1.1\r\n".encode())
        self.sock.sendall(f"Host: {addr}\r\n".encode())
        for key, val in headers.items():
            self.sock.sendall(f"{key}: {val}\r\n".encode())
        self.sock.sendall(b"\r\n")
        self._recv_callback = recv_callback
        _thread.start_new_thread(self.recv_loop, ())

    def _recv_loop(self):
        buf = b""
        while True:
            buf += self.sock.recv(1024)
            while b"\n" in buf:
                line, buf = buf.split(b"\n", maxsplit=1)
                self.recv_callback(line.decode())

    def send(self, line):
        self.sock.sendall((line + "\n").encode())


_thread.start_new_thread(keep_ntp_current, ())


while True:
    conn = Connection(
        SERVER_ADDRESS,
        "POST",
        "/api/v0/controller/register",
        {"Authorization": f"MeLaan {CONTROLLER_PASSWORD}"},
        lambda msg: print(f"recv msg: {msg}"),
    )
    while True:
        try:
            conn.send("client ok")
        except Exception:
            break
        time.sleep(60)
