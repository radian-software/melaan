import _thread
import binascii
import hashlib
import machine
import socket
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
        for key, val in {**self.auth(), **headers}.items():
            self.sock.sendall(f"{key}: {val}\r\n".encode())
        self.sock.sendall(b"\r\n")
        self.recv_callback = recv_callback
        _thread.start_new_thread(self.recv_loop, tuple())

    def recv_loop(self):
        buf = b""
        while True:
            buf += self.sock.recv(1024)
            while b"\n" in buf:
                line, buf = buf.split(b"\n", maxsplit=1)
                self.recv(line.decode())

    def auth(self):
        return {}

    def recv(self, line):
        self.recv_callback(line)

    def send(self, line):
        self.sock.sendall((line + "\n").encode())


def get_sha256(data):
    h = hashlib.sha256()
    h.update(data)
    return h.digest()


# https://datatracker.ietf.org/doc/html/rfc2104.html
# https://stackoverflow.com/a/29409299
def get_sha256_hmac(message, key):
    B = 32
    ipad = b"\u0036" * B
    opad = b"\u005C" * B
    # Step 1
    assert len(key) == B
    # Step 2
    ipad_with_key = bytes(a ^ b for a, b in zip(key, ipad))
    # Step 3-4
    hashed_text = get_sha256(ipad_with_key + message)
    # Step 5
    opad_with_key = bytes(a ^ b for a, b in zip(key, opad))
    # Step 6-7
    return get_sha256(opad_with_key + hashed_text)


def sign_message(message, key, ts=None):
    ts = ts or int(time.time())
    signature = binascii.b2a_base64(get_sha256_hmac(f"{ts}:{message}".encode(), key))
    return f"{ts}:{signature}:{message}"


def verify_signed_message(signed_message, key, ttl=60):
    cur_ts = int(time.time())
    parts = signed_message.split(":", maxsplit=2)
    if len(parts) != 3:
        return False
    ts, _, message = parts
    try:
        ts = int(ts)
    except ValueError:
        return False
    if abs(ts - cur_ts) > ttl:
        return False
    return sign_message(message, key, ts=ts) == signed_message


class SignedConnection(Connection):
    def __init__(self, addr, method, path, headers, recv_callback, key):
        self.key = key
        super().__init__(addr, method, path, headers, recv_callback)

    def auth(self):
        message = sign_message("auth", self.key)
        return {"Authorization": f"MeLaan {message}"}

    def send(self, line):
        super().send(sign_message(line, self.key))

    def recv(self, line):
        if verify_signed_message(line, self.key):
            super().recv(line)


machine.RTC().datetime(get_ntp())


conn = SignedConnection(
    SERVER_ADDRESS,
    "POST",
    "/api/v0/controller/register",
    {},
    lambda msg: print(f"recv msg: {msg}"),
    CONTROLLER_PASSWORD,
)
