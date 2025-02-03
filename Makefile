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
	$(SERIAL) --initstring="$$(python3 install.py)"$$'\r\r'

.PHONY: shell
shell:
	$(SERIAL)
