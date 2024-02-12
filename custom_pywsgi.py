#!/usr/bin/env python3

import time


def iterator(environ):
    yield b""  # write headers
    yield b"server ok, waiting for recv\n"
    inp = environ["wsgi.input"]
    while char := inp.read(1):
        yield f"got char {repr(char)}\n".encode()
        time.sleep(1)
    yield b"done, closing\n"


def app(environ, start_response):
    start_response(
        "200 OK",
        # "101 Switching Protocols",
        [
            ("connection", "upgrade"),
            ("upgrade", "CustomProtocol"),
        ],
    )
    return iterator(environ)


if __name__ == "__main__":
    from gevent import pywsgi

    server = pywsgi.WSGIServer(("127.0.0.1", 8888), app)
    server.serve_forever()
