# MeLaan

This is a small project that makes it easier for me to open the
apartment door when someone is delivering a package and I am not at
home. There is a Raspberry Pi Pico W that is wired to the door buzzer
circuit, which makes a persistent TLS connection to a small Golang
server running on a VPS, which proxies door opening requests submitted
via Tasker on Android and exposes status monitoring.

It has some problems.

## Usage

Create some files in the repository:

* `CONTROLLER_PASSWORD`: a randomly generated password
* `SERVER_ADDRESS`: IPv4 address and port separated by colon
* `WIFI_CREDENTIALS`: wireless SSID and password separated by colon
* `melaan-ca.crt`: Self-signed certificate authority, PEM
* `melaan-server.crt`: Leaf certificate, PEM
* `melaan-server.key`: Leaf private key, PEM
* `melaan-ca.der`: DER version of the CA
* `.env`: values for `CONTROLLER_PASSWORD`, `REMOTE_PASSWORD`, and
  `REMOTE_PASSWORD_READONLY` (all randomly generated passwords) as
  well as `SERVER_PORT` (probably >=1025)

Here's an example, though you would certainly have to adjust the
IP/port and wifi credentials at least:

```
head -c20 /dev/urandom | xxd -p > CONTROLLER_PASSWORD
echo "$(curl -4 https://icanhazip.com):52739" > SERVER_ADDRESS
echo xfinitywifi:password > WIFI_CREDENTIALS
step certificate create melaan-ca melaan-ca.crt melaan-ca.key --profile root-ca --no-password --insecure --not-after 876000h
step certificate create melaan-server melaan-server.crt melaan-server.key --profile leaf --ca melaan-ca.crt --ca-key melaan-ca.key --no-password --insecure --not-after 876000h
step certificate format melaan-ca.crt > melaan-ca.der
echo CONTROLLER_PASSWORD="$(< CONTROLLER_PASSWORD)" >> .env
echo REMOTE_PASSWORD="$(head -c20 /dev/urandom | xxd -p)" >> .env
echo REMOTE_PASSWORD_READONLY="$(head -c20 /dev/urandom | xxd -p)" >> .env
echo SERVER_PORT="$(awk -F: '{print $2}' SERVER_ADDRESS)" >> .env
```

The controller will run on the Raspberry Pi and connect to a local
wireless network, then contact the server at the provided IPv4
address. The controller uses `CONTROLLER_PASSWORD` to authenticate to
the server, while door-opening and health-check clients use
`REMOTE_PASSWORD` and `REMOTE_PASSWORD_READONLY` to authenticate to
the server.

You can run `server.go` on a standard Linux server. Copy the `.env`
file as well as the leaf cert and key to it first.

You can run `controller.py` using a standard Linux Python
installation, for testing. To install onto the Raspberry Pi, [install
MicroPython](https://micropython.org/download/RPI_PICO_W/) to the
board via UF2, then once you have `make shell` working (install
picocom and udev rules, make sure you have a data-capable microUSB
cable), run `make install` to copy the script and necessary files onto
the filesystem. Then terminate the shell to restart into the
controller code.

To update the code, interrupt with ctrl-C so that you get back to the
shell, then `make install` and restart again. If it gets completely
bricked, see [wiping flash from
UF2](https://www.raspberrypi.com/documentation/microcontrollers/pico-series.html#reset-flash-memory)
and reinstall MicroPython.
