# MeLaan

This is a small project that makes it easier for me to open the
apartment door when someone is delivering a package and I am not at
home. There is a Raspberry Pi Pico W that is wired to the door buzzer
circuit, which makes a persistent TLS connection to a small Golang
server running on a VPS, which proxies door opening requests submitted
via Tasker on Android and exposes status monitoring.

It has some problems.
