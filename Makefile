.PHONY: server
server:
	exec poetry run gunicorn server:app -b 0.0.0.0:8793 --access-logfile - -R --certfile melaan-server.crt --keyfile melaan-server.key --do-handshake-on-connect

.PHONY: shell
shell:
	picocom /dev/serial/by-id/usb-MicroPython_Board_in_FS_mode_*-if00 -b115200

.PHONY: ca
ca:
	step certificate create melaan-ca melaan-ca.crt melaan-ca.key --profile root-ca --no-password --insecure --not-after 876000h

.PHONY: cert
cert:
	step certificate create melaan-server melaan-server.crt melaan-server.key --profile leaf --ca melaan-ca.crt --ca-key melaan-ca.key --no-password --insecure --not-after 876000h
