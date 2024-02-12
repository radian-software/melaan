package main

import (
	"bufio"
	"crypto/tls"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"sync"
	"time"

	"github.com/caarlos0/env/v10"
	"github.com/gorilla/mux"
)

type config struct {
	ControllerPassword string `env:"CONTROLLER_PASSWORD,notEmpty"`
	RemotePassword     string `env:"REMOTE_PASSWORD,notEmpty"`
}

type server struct {
	Synchronizer          *sync.Mutex
	DoorShouldOpen        *sync.Cond
	DoorOpened            *sync.Cond
	IsControllerHealthy   bool
	LastControllerCheckin *time.Time
	SendChannel           chan string
}

func NewServer() *server {
	return &server{
		Synchronizer:          &sync.Mutex{},
		DoorShouldOpen:        sync.NewCond(&sync.Mutex{}),
		DoorOpened:            sync.NewCond(&sync.Mutex{}),
		IsControllerHealthy:   false,
		LastControllerCheckin: nil,
	}
}

func (s *server) isHealthy() (bool, *time.Duration) {
	s.Synchronizer.Lock()
	defer s.Synchronizer.Unlock()
	if s.LastControllerCheckin == nil {
		return false, nil
	}
	since := time.Now().Sub(*s.LastControllerCheckin)
	return (s.IsControllerHealthy && s.LastControllerCheckin.After(time.Now().Add(-60*time.Second))), &since
}

func (s *server) isHealthyString() (bool, string) {
	if ok, last := s.isHealthy(); ok {
		return true, fmt.Sprintf("ok, last update %d seconds ago\n", int(last.Seconds()))
	} else if last != nil {
		return false, fmt.Sprintf("bad, last update %d minutes ago\n", int(last.Minutes()))
	}
	return false, fmt.Sprintf("bad, no update yet\n")
}

func (s *server) handleRegisterReceive(conn net.Conn, brw *bufio.ReadWriter) error {
	for {
		conn.SetReadDeadline(time.Now().Add(60 * time.Second))
		line, prefix, err := brw.ReadLine()
		if err != nil {
			return err
		}
		if prefix {
			return errors.New("unexpectedly long line received")
		}
		switch string(line) {
		case "client ok":
			s.Synchronizer.Lock()
			s.IsControllerHealthy = true
			now := time.Now()
			s.LastControllerCheckin = &now
			s.Synchronizer.Unlock()
		case "opened":

		}
	}
}

func (s *server) handleRegister(controllerPassword string, w http.ResponseWriter, req *http.Request) error {
	if req.Header.Get("authorization") != fmt.Sprintf("MeLaan %s", controllerPassword) {
		w.WriteHeader(http.StatusUnauthorized)
		return errors.New("bad controller auth")
	}
	if req.Header.Get("upgrade") != "MeLaan" {
		w.Header().Add("upgrade", "MeLaan")
		w.WriteHeader(http.StatusUpgradeRequired)
		return errors.New("bad upgrade header")
	}
	h, ok := w.(http.Hijacker)
	if !ok {
		return errors.New("http.ResponseWriter does not implement Hijacker")
	}
	conn, brw, err := h.Hijack()
	defer conn.Close()
	if err != nil {
		return err
	}
	ch := make(chan string)
	s.Synchronizer.Lock()
	s.SendChannel = ch
	s.Synchronizer.Unlock()
	go func() {
		err := s.handleRegisterReceive(conn, brw)
		if err != nil {
			log.Println("error in controller receive loop:", err)
			s.Synchronizer.Lock()
			s.IsControllerHealthy = false
			close(ch)
			s.Synchronizer.Unlock()
			conn.Close()
			return
		}
	}()
	go func() {
		for {
			select {
			case ch <- "server ok":
			case <-time.After(5 * time.Second):
				conn.Close()
				return
			}
			time.Sleep(15 * time.Second)
		}
	}()
	for msg := range ch {
		conn.SetWriteDeadline(time.Now().Add(60 * time.Second))
		_, err := conn.Write([]byte(msg + "\n"))
		if err != nil {
			log.Println("error sending message:", err)
			conn.Close()
			break
		}
	}
	return nil
}

func mainE() error {
	cfg := config{}
	err := env.Parse(&cfg)
	if err != nil {
		return err
	}
	s := NewServer()
	r := mux.NewRouter()
	r.HandleFunc("/health", func(w http.ResponseWriter, req *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write([]byte("ok\n"))
	}).Methods("GET")
	r.HandleFunc("/api/v0/controller/register", func(w http.ResponseWriter, req *http.Request) {
		err := s.handleRegister(cfg.ControllerPassword, w, req)
		if err != nil {
			log.Println("error in controller loop:", err)
		}
	}).Methods("POST")
	r.HandleFunc("/api/v0/remote/status", func(w http.ResponseWriter, req *http.Request) {
		ok, status := s.isHealthyString()
		if ok {
			w.WriteHeader(http.StatusOK)
		} else {
			w.WriteHeader(http.StatusBadGateway)
		}
		w.Write([]byte(status))
	})
	r.HandleFunc("/api/v0/remote/open", func(w http.ResponseWriter, req *http.Request) {
		ok, status := s.isHealthyString()
		if !ok {
			w.WriteHeader(http.StatusBadGateway)
			w.Write([]byte("health: " + status))
			return
		}
		select {
		case s.SendChannel <- "open sesame":
		case <-time.After(1 * time.Second):
			w.WriteHeader(http.StatusBadGateway)
			w.Write([]byte("pipes clogged, can't open door\n"))
			return
		}
	})
	port, err := net.Listen("tcp", "0.0.0.0:8793")
	if err != nil {
		return err
	}
	log.Println("listening on 0.0.0.0:8793")
	server := http.Server{
		Handler: r,
		// Disable HTTP/2 because it does not support http.Hijacker
		TLSNextProto: map[string]func(*http.Server, *tls.Conn, http.Handler){},
	}
	return server.ServeTLS(port, "melaan-server.crt", "melaan-server.key")
}

func main() {
	err := mainE()
	if err != nil {
		panic(err)
	}
}
