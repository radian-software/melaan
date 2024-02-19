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

func waitCondTimeout(c *sync.Cond, crit func() bool, timeout time.Duration) bool {
	expired := false
	time.AfterFunc(timeout, func() {
		c.L.Lock()
		expired = true
		c.L.Unlock()
		c.Broadcast()
	})
	c.L.Lock()
	for crit() || expired {
		c.Wait()
	}
	c.L.Unlock()
	return expired
}

type config struct {
	DoorOpenSeconds    int    `env:"DOOR_OPEN_SECONDS,notEmpty"`
	ControllerPassword string `env:"CONTROLLER_PASSWORD,notEmpty"`
	RemotePassword     string `env:"REMOTE_PASSWORD,notEmpty"`
	DisableTLS         bool   `env:"DISABLE_TLS"`
}

type server struct {
	doorOpenSeconds int

	lock                  *sync.Mutex
	controller            *controller
	lastControllerCheckin *time.Time
	doorLastOpened        *time.Time
}

type controller struct {
	lock sync.Mutex

	conn   net.Conn
	stream *bufio.ReadWriter

	openRequestReceived *time.Time
	openRequestCond     *sync.Cond

	doorOpened   *time.Time
	doorOpenCond *sync.Cond

	isAlive   bool
	aliveCond *sync.Cond
}

func NewServer() *server {
	return &server{
		lock:                  &sync.Mutex{},
		controller:            nil,
		lastControllerCheckin: nil,
		doorLastOpened:        nil,
	}
}

func (s *server) NewController(conn net.Conn, stream *bufio.ReadWriter) *controller {
	c := controller{
		conn:                conn,
		stream:              stream,
		openRequestReceived: nil,
		openRequestCond:     sync.NewCond(&sync.Mutex{}),
		doorOpened:          nil,
		doorOpenCond:        sync.NewCond(&sync.Mutex{}),
		isAlive:             true,
		aliveCond:           sync.NewCond(&sync.Mutex{}),
	}
	go c.doReads(s)
	go c.doCloses(s)
	return &c
}

func (s *server) Register(conn net.Conn, stream *bufio.ReadWriter) {
	s.lock.Lock()
	c := s.controller
	c.aliveCond.L.Lock()
	c.isAlive = false
	c.aliveCond.Broadcast()
	c.aliveCond.L.Unlock()
	s.controller = s.NewController(conn, stream)
	s.lock.Unlock()
}

func (s *server) Open() error {
	s.lock.Lock()
	if s.controller == nil {
		s.lock.Unlock()
		return errors.New("controller is offline")
	}
	if s.doorLastOpened.After(time.Now().Add(time.Duration(-s.doorOpenSeconds) * time.Second)) {
		s.lock.Unlock()
		return errors.New("door already opened")
	}
	c := s.controller
	s.lock.Unlock()
	return c.Open(s)
}

func (s *server) getController() *controller {
	s.lock.Lock()
	defer s.lock.Unlock()
	return s.controller
}

func (s *server) isHealthy() (bool, *time.Duration) {
	s.lock.Lock()
	defer s.lock.Unlock()
	if s.lastControllerCheckin == nil {
		return false, nil
	}
	since := time.Now().Sub(*s.lastControllerCheckin)
	return s.controller != nil && s.lastControllerCheckin.After(time.Now().Add(-60*time.Second)), &since
}

func (s *server) isHealthyString() (bool, string) {
	if ok, last := s.isHealthy(); ok {
		return true, fmt.Sprintf("ok, last update %d seconds ago\n", int(last.Seconds()))
	} else if last != nil {
		return false, fmt.Sprintf("bad, last update %d minutes ago\n", int(last.Minutes()))
	}
	return false, fmt.Sprintf("bad, no update yet\n")
}

func (s *server) recordCheckin(ts *time.Time, c *controller) {
	s.lock.Lock()
	if s.controller != c {
		return
	}
	s.lastControllerCheckin = ts
	s.lock.Unlock()
}

func (s *server) recordOpening(ts *time.Time, c *controller) {
	s.lock.Lock()
	if s.controller != c {
		return
	}
	s.doorLastOpened = ts
	s.lock.Unlock()
}

func (c *controller) Open(s *server) error {
	c.openRequestCond.L.Lock()
	now := time.Now()
	c.openRequestReceived = &now
	c.openRequestCond.Broadcast()
	c.openRequestCond.L.Unlock()
	expired := waitCondTimeout(c.doorOpenCond, func() bool {
		return c.doorOpened != nil && c.doorOpened.After(time.Now().Add(time.Duration(-s.doorOpenSeconds)))
	}, 3*time.Second)
	if expired {
		return errors.New("controller didn't respond in time")
	}
	return nil
}

func (c *controller) doReads(s *server) {
	for {
		c.conn.SetReadDeadline(time.Now().Add(5 * time.Second))
		line, prefix, err := c.stream.ReadLine()
		if err != nil {
			log.Println("error reading line:", err)
			return
		}
		if prefix {
			log.Println("error reading line: unexpectedly long line received")
			return
		}
		switch string(line) {
		case "client ok":
			now := time.Now()
			s.recordCheckin(&now, c)
		case "opened":
			now := time.Now()
			s.recordOpening(&now, c)
		}
	}
}

func (c *controller) doWrites(s *server) {
	for {
		lastPing := time.Unix(0, 0)
		c.conn.SetWriteDeadline(time.Now().Add(5 * time.Second))
		shouldOpen := !waitCondTimeout()
	}
}

func (c *controller) doCloses(s *server) {
	c.aliveCond.L.Lock()
	for c.isAlive {
		c.aliveCond.Wait()
	}
	c.aliveCond.L.Unlock()
	c.conn.Close()
	s.lock.Lock()
	s.controller = nil
	s.lock.Unlock()
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
		if req.Header.Get("authorization") != fmt.Sprintf("MeLaan %s", cfg.ControllerPassword) {
			w.WriteHeader(http.StatusUnauthorized)
		}
		if req.Header.Get("upgrade") != "MeLaan" {
			w.Header().Add("upgrade", "MeLaan")
			w.WriteHeader(http.StatusUpgradeRequired)
		}
		h, ok := w.(http.Hijacker)
		if !ok {
			log.Println("http.ResponseWriter does not implement http.Hijacker")
			w.WriteHeader(http.StatusInternalServerError)
		}
		conn, stream, err := h.Hijack()
		defer conn.Close()
		if err != nil {
			log.Println("unable to hijack http.ResponseWriter:", err)
			w.WriteHeader(http.StatusInternalServerError)
		}
		s.Register(conn, stream)
	}).Methods("PUT")
	r.HandleFunc("/api/v0/remote/status", func(w http.ResponseWriter, req *http.Request) {
		if req.Header.Get("authorization") != fmt.Sprintf("MeLaan %s", cfg.RemotePassword) {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		ok, status := s.isHealthyString()
		if ok {
			w.WriteHeader(http.StatusOK)
		} else {
			w.WriteHeader(http.StatusBadGateway)
		}
		w.Write([]byte(status))
	}).Methods("GET")
	r.HandleFunc("/api/v0/remote/open", func(w http.ResponseWriter, req *http.Request) {
		if req.Header.Get("authorization") != fmt.Sprintf("MeLaan %s", cfg.RemotePassword) {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		err := s.Open()
		if err != nil {
			w.WriteHeader(http.StatusBadGateway)
			w.Write([]byte(err.Error() + "\n"))
		}
		w.WriteHeader(http.StatusCreated)
	}).Methods("POST")
	port, err := net.Listen("tcp", "0.0.0.0:8793")
	if err != nil {
		return err
	}
	server := http.Server{
		Handler: r,
		// Disable HTTP/2 because it does not support http.Hijacker
		TLSNextProto: map[string]func(*http.Server, *tls.Conn, http.Handler){},
	}
	if cfg.DisableTLS {
		log.Println("listening on http://0.0.0.0:8793")
		return server.Serve(port)
	}
	log.Println("listening on https://0.0.0.0:8793")
	return server.ServeTLS(port, "melaan-server.crt", "melaan-server.key")
}

func main() {
	err := mainE()
	if err != nil {
		panic(err)
	}
}
