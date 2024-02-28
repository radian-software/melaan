SHELL := bash
SERIAL := picocom /dev/serial/by-id/usb-MicroPython_Board_in_FS_mode_*-if00 -b115200

.PHONY: server
server:
	go run server.go

.PHONY: server-watch
server-watch:
	watchexec -f server.go -r go run server.go

.PHONY: controller
controller:
	python3 controller.py

.PHONY: controller-watch
controller-watch:
	watchexec -f controller.py -r python3 controller.py

.PHONY: install
install:
	$(SERIAL) -x250 --initstring="$$(python3 install.py)"$$'\r\r'

.PHONY: controller-onboard
controller-onboard:
	$(SERIAL) --initstring="with open('main.py') as f: exec(f.read())"$$'\r\r'

.PHONY: shell
shell:
	$(SERIAL)

.PHONY: ca
ca:
	step certificate create melaan-ca melaan-ca.crt melaan-ca.key --profile root-ca --no-password --insecure --not-after 876000h

.PHONY: cert
cert:
	step certificate create melaan-server melaan-server.crt melaan-server.key --profile leaf --ca melaan-ca.crt --ca-key melaan-ca.key --no-password --insecure --not-after 876000h
