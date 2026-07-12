import _thread
import socket
import ssl
import struct
import time


def log(msg):
    year, month, day, hour, minute, second, *_ = time.gmtime()
    print(
        f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{second:02d} {msg}"
    )


global_failure_count = 0


def deal_with_failure(num_failures=1):
    try:
        import machine
    except ImportError:
        return
    global global_failure_count
    global_failure_count += num_failures
    if global_failure_count > 30:
        log("something is fucked up, rebooting")
        machine.reset()


def deal_with_success():
    global global_failure_count
    global_failure_count = 0


def set_door_state(is_open):
    try:
        from machine import Pin
    except ImportError:
        return
    Pin(0, Pin.OUT).value(is_open)


def set_health_state(status):
    offline, online, connected, door_open = status
    try:
        from machine import Pin
    except ImportError:
        return
    machine.Pin(2, Pin.OUT).value(offline)
    machine.Pin(4, Pin.OUT).value(online)
    machine.Pin(6, Pin.OUT).value(connected)
    machine.Pin(8, Pin.OUT).value(door_open)


STATUS_OFFLINE = (1, 0, 0, 0)
STATUS_ONLINE = (0, 1, 0, 0)
STATUS_CONNECTED = (0, 0, 1, 0)
STATUS_OPEN = (0, 0, 1, 1)
STATUS_OPEN_DISCONNECTED = (0, 1, 0, 1)
STATUS_FUCKED = (1, 1, 0, 0)


# Safety
set_door_state(False)
set_health_state(STATUS_OFFLINE)


# these files should be created manually on the micropython filesystem
with open("CONTROLLER_PASSWORD") as f:
    CONTROLLER_PASSWORD = f.read().strip()
with open("SERVER_ADDRESS") as f:
    SERVER_ADDRESS = f.read().strip()
with open("WIFI_CREDENTIALS") as f:
    WIFI_SSID, WIFI_PASSWORD = f.read().strip().split(":")
with open("melaan-ca.der", "rb") as f:
    CA_DATA = f.read()


# https://stackoverflow.com/a/56613595
def get_ntp():
    REF_TIME_1970 = 2208988800  # Reference time
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(10)
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


ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
ssl_context.load_verify_locations(cadata=CA_DATA)
ssl_context.verify_mode = ssl.CERT_REQUIRED
ssl_wrapper = lambda sock: ssl_context.wrap_socket(
    sock,
    server_hostname="melaan-server",
)


class Connection:
    def __init__(self, addr, method, path, headers, recv_callback):
        ip, port = addr.split(":")
        port = int(port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(10)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Documentation says to always use getaddrinfo first, but
            # unfortunately there is no way to set a timeout on
            # getaddrinfo, so if you use it, there's a chance your
            # entire board will be bricked forever until you manually
            # reset it. We thus can't handle resolving hostnames, as
            # far as I can tell doing so is impossible without risking
            # a freeze.
            sock.connect((ip, port))
            self.sock = ssl_wrapper(sock)
            self.sock.write(f"{method} {path} HTTP/1.1\r\n".encode())
            self.sock.write(f"Host: {addr}\r\n".encode())
            for key, val in headers.items():
                self.sock.write(f"{key}: {val}\r\n".encode())
            self.sock.write(b"\r\n")
            self._recv_callback = recv_callback
            self._lock = _thread.allocate_lock()
            _thread.start_new_thread(self._recv_loop, ())
        except Exception:
            sock.close()
            raise

    def _recv_loop(self):
        found_http_statusline = False
        found_http_body = False
        buf = b""
        while True:
            try:
                char = self.sock.read(1)
                buf += char
                if not char:
                    log("receive socket is closed, terminating")
                    return
            except Exception as e:
                log(f"failed to receive data, closing: {e}")
                try:
                    self.sock.close()
                except Exception as e:
                    log(f"failed to close: {e}")
                return
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not found_http_statusline:
                    log("found http statusline")
                    assert line.startswith(b"HTTP/1.1 101 "), line
                    found_http_statusline = True
                    continue
                if not found_http_body:
                    if line == b"\r":
                        log("found end of http headers")
                        found_http_body = True
                    continue
                self._recv_callback(line.decode(), self.send)

    def send(self, line):
        with self._lock:
            self.sock.write((line + "\n").encode())


class Controller:
    def __init__(self):
        self.close_callback = None
        self.last_server_ok = time.time()
        self.last_door_open = 0

    def recv(self, line, write_callback):
        if line == "server ok":
            log("got server ok")
            self.last_server_ok = time.time()
            return
        if line == "open sesame":
            log("preparing to open door")
            self.last_door_open = time.time()
            return
        log(f"unexpected message from server: {line}")


if onboard:
    import network

    nic = network.WLAN(network.STA_IF)
    log("activating network card")
    nic.active(True)
    log("connecting to WLAN")
    nic.connect(WIFI_SSID, WIFI_PASSWORD)
    while (status := nic.status()) != network.STAT_GOT_IP:
        log(f"waiting for wifi connectivity, status {status}")
        deal_with_failure()
        time.sleep(1)
    deal_with_success()
    log("confirming online status")
    while True:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(10)
            sock.connect(("9.9.9.9", 53))
        except Exception as e:
            log(f"not online, retrying: {e}")
            deal_with_failure(5)
            time.sleep(5)
        else:
            break
        finally:
            try:
                sock.close()
            except Exception:
                log(f"failed to close: {e}")
    deal_with_success()
    log("syncing rtc to ntp")
    while True:
        try:
            machine.RTC().datetime(struct_time_to_rtc(get_ntp()))
        except Exception as e:
            log(f"failed to sync ntp, retrying: {e}")
            deal_with_failure(5)
            time.sleep(5)
        else:
            break
    deal_with_success()
    log("synced rtp, board online")
    set_health_state(STATUS_ONLINE)


ctl = Controller()
while True:
    try:
        log("starting connection")
        conn = Connection(
            SERVER_ADDRESS,
            "PUT",
            "/api/v0/controller/register",
            {"Authorization": f"MeLaan {CONTROLLER_PASSWORD}", "Upgrade": "MeLaan"},
            ctl.recv,
        )
        deal_with_success()
        log("connected successfully")
        set_health_state(STATUS_CONNECTED)
    except Exception as e:
        log(f"failed to connect, {e}")
        set_health_state(STATUS_FUCKED)
        if "EINPROGRESS" in str(e):
            # Board is fucked
            deal_with_failure(1000)
        else:
            # Something unexpected
            deal_with_failure(1)
        time.sleep(1)
        continue
    time.sleep(0.5)
    while True:
        if time.time() - ctl.last_door_open < 15:
            # Make sure that under no circumstances do we leave the
            # door open. Thus, do the entire open/close cycle in
            # synchronous code. We can't just put it in another thread
            # because there are only allowed to be 2 threads in
            # micropython and the other one is busy already listening
            # for inbound messages.
            try:
                log("opening door")
                set_door_state(True)
                set_health_state(STATUS_OPEN)
                try:
                    log("reporting door open")
                    conn.send("opened")
                except Exception as e:
                    log(
                        f"failed to report door open, but deferring close while door open: {e}"
                    )
                    set_health_state(STATUS_OPEN_DISCONNECTED)
                while time.time() - ctl.last_door_open < 15:
                    try:
                        log("sending client ok while door open")
                        conn.send("client ok")
                        time.sleep(3)
                    except Exception as e:
                        log(
                            f"failed to send client ok, but deferring close while door open: {e}"
                        )
                        set_health_state(STATUS_OPEN_DISCONNECTED)
            finally:
                log("closing door")
                set_door_state(False)
                set_health_state(STATUS_CONNECTED)
        try:
            log("sending client ok")
            conn.send("client ok")
            time.sleep(3)
        except Exception as e:
            log(f"failed to send client ok, closing: {e}")
            try:
                conn.sock.close()
            except Exception as e:
                log(f"failed to close: {e}")
            set_health_state(STATUS_ONLINE)
            break
        if time.time() - ctl.last_server_ok > 30:
            log("connection became stale, closing")
            try:
                conn.sock.close()
            except Exception as e:
                log(f"failed to close: {e}")
            set_health_state(STATUS_ONLINE)
    time.sleep(1)
