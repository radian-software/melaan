import dotenv

dotenv.load_dotenv()

import base64
import hmac
import os
import time

import flask
import werkzeug.datastructures

CONTROLLER_PASSWORD = base64.b64decode(os.environ["CONTROLLER_PASSWORD"])
REMOTE_PASSWORD = os.environ["REMOTE_PASSWORD"]


app = flask.Flask(__name__)


class State:
    def __init__(self):
        self.controller_is_healthy = False
        self.last_controller_checkin = None


state = State()


@app.route("/api/v0/remote/open", methods=["POST"])
def route_remote_open():
    auth = flask.request.headers.get("Authorization")
    if auth != f"Bearer {REMOTE_PASSWORD}":
        return "Wrong or missing authentication", 401
    if not state.controller_is_healthy:
        return "Controller is offline", 503


def sign_message(message, key, ts=None):
    ts = ts or int(time.time())
    signature = base64.b64encode(
        hmac.digest(key, f"{ts}:{message}".encode(), "sha256")
    ).decode()
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


class DirectResponse(flask.Response):
    def __init__(self, handler, status, headers):
        super().__init__(None, status, headers)
        self.handler = handler
        self.raw_headers = headers

    def get_wsgi_response(self, environ):
        def iterator():
            yield b""
            self.handler(environ["werkzeug.socket"])

        return (
            iterator(),
            self.status,
            list(werkzeug.datastructures.Headers(self.raw_headers)),
        )


class LineBasedSocket:
    def __init__(self, sock):
        self.sock = sock
        self.recvline_buffer = b""

    def send(self, line):
        self.sock.sendall(line.encode() + b"\n")

    def recv(self):
        while b"\n" not in self.recvline_buffer:
            self.recvline_buffer += self.sock.recv(1024)
        line, self.recvline_buffer = self.recvline_buffer.split(b"\n", maxsplit=1)
        return line.decode()


class SignedLineBasedSocket(LineBasedSocket):
    def __init__(self, sock, key):
        super().__init__(sock)
        self.key = key

    def send(self, line):
        super().send(sign_message(line, self.key))

    def recv(self):
        while not verify_signed_message(line := super().recv(), self.key):
            pass
        return line


@app.route("/api/v0/controller/register", methods=["POST"])
def route_controller_register():
    auth = flask.request.headers.get("Authorization")
    if not auth:
        return "", 401
    if not auth.startswith("MeLaan "):
        return "", 401
    _, auth_str = auth.split(" ", maxsplit=1)
    if not verify_signed_message(auth_str, CONTROLLER_PASSWORD, ttl=3600):
        return "", 401

    def handler(raw_sock):
        sock = SignedLineBasedSocket(raw_sock, CONTROLLER_PASSWORD)
        sock.send("hello world")

    return DirectResponse(handler, 101, {"Upgrade": "MeLaan"})
