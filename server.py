import dotenv

dotenv.load_dotenv()

from dataclasses import dataclass
from datetime import datetime
import os
import threading
from typing import cast

import flask

CONTROLLER_PASSWORD = os.environ["CONTROLLER_PASSWORD"]
REMOTE_PASSWORD = os.environ["REMOTE_PASSWORD"]


class DirectResponse:
    def __init__(self, handler):
        self.handler = handler

    def __call__(self, environ, start_response):
        start_response(101, {"connection": "upgrade", "upgrade": "MeLaan"})
        threading.Thread(
            target=lambda: self.handler(environ["gunicorn.socket"]), daemon=True
        ).start()
        return [""]


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

    def close(self):
        self.sock.close()


@dataclass
class State:
    controller_is_healthy = False
    last_controller_checkin = None

    def mark_healthy(self):
        self.controller_is_healthy = True
        self.last_controller_checkin = datetime.now()

    def mark_unhealthy(self):
        self.controller_is_healthy = False


class Server:
    def __init__(self):
        self.app = flask.Flask(__name__)
        self.state = State()
        self.socket = None

        @self.app.route("/api/v0/controller/register", methods=["POST"])
        def route_controller_register():
            auth = flask.request.headers.get("authorization")
            if auth != f"MeLaan {CONTROLLER_PASSWORD}":
                return "", 401
            if flask.request.headers.get("upgrade") != "MeLaan":
                return "", 426, {"upgrade": "MeLaan"}

            def handler(raw_sock):
                if self.socket:
                    self.state.mark_unhealthy()
                    self.socket.close()
                self.socket = LineBasedSocket(raw_sock)
                self.socket.send("server ok")
                threading.Thread(target=recv_loop, daemon=True).start()

            def recv_loop():
                if not self.socket:
                    return
                while True:
                    try:
                        line = self.socket.recv()
                        print("recv:", line)
                        if line == "client ok":
                            self.state.mark_healthy()
                    except Exception:
                        self.state.mark_unhealthy()

            return cast(flask.Response, DirectResponse(handler))

        @self.app.route("/api/v0/remote/open", methods=["POST"])
        def route_remote_open():
            auth = flask.request.headers.get("Authorization")
            if auth != f"Bearer {REMOTE_PASSWORD}":
                return "Wrong or missing authentication", 401
            if not self.state.controller_is_healthy:
                return "Controller is offline", 503
            return "Doing it!", 202

        _ = route_controller_register
        _ = route_remote_open


app = Server().app
