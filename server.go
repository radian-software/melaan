package main

import (
	"bufio"
	"crypto/tls"
	"errors"
	"fmt"
	"log"
	"math/rand"
	"net"
	"net/http"
	"sync"
	"time"

	"github.com/caarlos0/env/v11"
	"github.com/joho/godotenv"
)

const (
	Timing_RequestStaleness  = 15 * time.Second
	Timing_DoorOpenDuration  = 10 * time.Second
	Timing_IODeadline        = 3 * time.Second
	Timing_DoorOpenCooldown  = 1 * time.Second
	Timing_HeartbeatInterval = 1 * time.Second
)

type config struct {
	ControllerPassword     string `env:"CONTROLLER_PASSWORD,notEmpty"`
	RemotePassword         string `env:"REMOTE_PASSWORD,notEmpty"`
	RemotePasswordReadOnly string `env:"REMOTE_PASSWORD_READONLY,notEmpty"`
	DisableTLS             bool   `env:"DISABLE_TLS"`
}

type syncvar[T any] struct {
	Value T
	Cond  *sync.Cond
}

func NewSyncvar[T any]() syncvar[T] {
	return syncvar[T]{
		Cond: sync.NewCond(&sync.Mutex{}),
	}
}

func (s *syncvar[T]) Get() T {
	s.Cond.L.Lock()
	defer s.Cond.L.Unlock()
	return s.Value
}

func (s *syncvar[T]) Set(value T) {
	s.Cond.L.Lock()
	defer s.Cond.L.Unlock()
	s.Value = value
	s.Cond.Broadcast()
}

func (s *syncvar[T]) WithValue(task func(T)) {
	s.Cond.L.Lock()
	defer s.Cond.L.Unlock()
	task(s.Value)
}

func (s *syncvar[T]) Update(xform func(T) T) {
	s.Cond.L.Lock()
	defer s.Cond.L.Unlock()
	s.Value = xform(s.Value)
	s.Cond.Broadcast()
}

func (s *syncvar[T]) Wait(pred func(T) bool) {
	s.WaitTimeout(pred, 0)
}

func (s *syncvar[T]) WaitTimeout(pred func(T) bool, timeout time.Duration) error {
	expired := false
	if timeout > 0 {
		time.AfterFunc(timeout, func() {
			s.Cond.L.Lock()
			defer s.Cond.L.Unlock()
			expired = true
			s.Cond.Broadcast()
		})
	}
	s.Cond.L.Lock()
	defer s.Cond.L.Unlock()
	for !(pred(s.Value) || expired) {
		s.Cond.Wait()
	}
	if expired {
		return errors.New("timed out")
	}
	return nil
}

type syncvarTimePtr struct {
	syncvar[*time.Time]
}

func NewSyncvarTimePtr() syncvarTimePtr {
	return syncvarTimePtr{NewSyncvar[*time.Time]()}
}

func (s *syncvarTimePtr) UpdateToMaxWith(value time.Time) {
	s.Update(func(existing *time.Time) *time.Time {
		if existing == nil {
			return &value
		}
		if value.After(*existing) {
			return &value
		}
		return existing
	})
}

type server struct {
	activeController      syncvar[*controller]
	lastControllerConnect syncvarTimePtr
	lastControllerCheckin syncvarTimePtr
	openRequestReceived   syncvarTimePtr
	doorOpened            syncvarTimePtr
}

type controller struct {
	server *server
	id     int

	conn   net.Conn
	stream *bufio.ReadWriter

	openAttemptMade syncvarTimePtr
}

func (c *controller) Log(format string, args ...interface{}) {
	log.Println(fmt.Sprintf("controller %d: ", c.id) + fmt.Sprintf(format, args...))
}

func NewServer() *server {
	return &server{
		activeController:      NewSyncvar[*controller](),
		lastControllerConnect: NewSyncvarTimePtr(),
		lastControllerCheckin: NewSyncvarTimePtr(),
		openRequestReceived:   NewSyncvarTimePtr(),
		doorOpened:            NewSyncvarTimePtr(),
	}
}

func (s *server) RegisterController(conn net.Conn, stream *bufio.ReadWriter) {
	c := controller{
		server:          s,
		id:              rand.Int(),
		conn:            conn,
		stream:          stream,
		openAttemptMade: NewSyncvarTimePtr(),
	}
	c.Log("registering")
	s.activeController.Set(&c)
	s.lastControllerConnect.UpdateToMaxWith(time.Now())
	go c.doReads()
	go c.doWrites()
	go c.doCloses()
}

func (c *controller) IsRegistered() bool {
	return c.server.activeController.Get() == c
}

func (c *controller) Deregister() {
	c.server.activeController.Update(func(cc *controller) *controller {
		if c == cc {
			c.Log("deregistering")
			return nil
		}
		return cc
	})
}

func (c *controller) IfActive(task func()) {
	c.server.activeController.WithValue(func(cc *controller) {
		if c == cc {
			task()
		}
	})
}

func (s *server) Open() (string, error) {
	c := s.activeController.Get()
	if c == nil {
		return "", errors.New("no active controller")
	}
	c.Log("received open request")
	now := time.Now()
	s.openRequestReceived.UpdateToMaxWith(now)
	var resp string
	err := s.doorOpened.WaitTimeout(func(last *time.Time) bool {
		if last == nil {
			return false
		}
		if last.Before(now.Add(-Timing_DoorOpenDuration)) {
			return false
		}
		if last.Before(now) {
			resp = "door already opened"
			return true
		}
		resp = "door opened"
		return true
	}, 5*time.Second)
	if err != nil {
		return "", fmt.Errorf("waiting for controller: %w", err)
	}
	return resp, nil
}

func (s *server) isHealthy() (bool, string) {

	lastConnect := s.lastControllerConnect.Get()
	if lastConnect == nil {
		return false, fmt.Sprintf("bad, no connect yet\n")
	}

	lastCheckin := s.lastControllerCheckin.Get()
	if lastCheckin == nil {
		return false, fmt.Sprintf("bad, no check-in yet\n")
	}

	sinceConnect := time.Now().Sub(*lastConnect).Round(time.Second)
	sinceCheckin := time.Now().Sub(*lastCheckin).Round(time.Second)

	msg := fmt.Sprintf("last check-in %s ago, last connect %s ago", sinceCheckin, sinceConnect)
	healthy := lastCheckin.After(time.Now().Add(-60 * time.Second))

	var status string
	if healthy {
		status = "ok"
	} else {
		status = "bad"
	}

	return healthy, fmt.Sprintf("%s, %s\n", status, msg)
}

func (c *controller) doReads() {
	defer c.Deregister()
	defer c.Log("terminating read loop")
	for {
		if !c.IsRegistered() {
			return
		}
		c.conn.SetReadDeadline(time.Now().Add(Timing_IODeadline))
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
			c.Log("received client ok")
			c.IfActive(func() {
				// Check to make sure we have been connected for at least some time
				// before we start recording the health pings. This way, if the
				// client repeatedly only gets in one or two pings before breaking
				// and reconnecting again, this will show as consistently being
				// offline.
				//
				// If on the other hand the client only occasionally has to
				// reconnect, say less than once per minute, it will show as
				// consistently being online.
				lastConnect := c.server.lastControllerConnect.Get()
				if lastConnect == nil {
					c.Log("ignore client ok due to no connect info !?")
					return
				}
				if lastConnect.After(time.Now().Add(-30 * time.Second)) {
					c.Log("ignore client ok due to recent reconnect")
					return
				}
				c.server.lastControllerCheckin.UpdateToMaxWith(time.Now())
			})
		case "opened":
			c.Log("door was opened")
			c.IfActive(func() {
				c.server.doorOpened.UpdateToMaxWith(time.Now())
			})
		case "closed":
			c.Log("door was closed")
		default:
			c.Log("unexpected line received: %s", line)
		}
	}
}

func (c *controller) shouldOpen(lastRequest *time.Time, lastAttempt *time.Time, lastSuccess *time.Time) bool {
	// We have not been asked to open the door
	if lastRequest == nil {
		return false
	}
	// The request was from too long ago, something must have gone
	// wrong, ignore it to avoid unexpected opens
	if lastRequest.Before(time.Now().Add(-Timing_RequestStaleness)) {
		return false
	}
	// We have already submitted an attempt for this request
	if lastAttempt != nil && lastRequest.Before(*lastAttempt) {
		return false
	}
	// The door is still open from last time
	if lastSuccess != nil && lastRequest.Before(lastSuccess.Add(Timing_DoorOpenDuration+Timing_DoorOpenCooldown)) {
		return false
	}
	return true
}

func (c *controller) doWrites() {
	defer c.Deregister()
	defer c.Log("terminating write loop")
	for {
		var shouldOpen bool
		c.server.openRequestReceived.WaitTimeout(func(lastRequest *time.Time) bool {
			lastAttempt := c.openAttemptMade.Get()
			lastSuccess := c.server.doorOpened.Get()
			shouldOpen = c.shouldOpen(lastRequest, lastAttempt, lastSuccess)
			return shouldOpen
		}, Timing_HeartbeatInterval)
		if !c.IsRegistered() {
			return
		}
		c.conn.SetWriteDeadline(time.Now().Add(Timing_IODeadline))
		if shouldOpen {
			c.Log("writing open command")
			_, err := c.conn.Write([]byte("open sesame\n"))
			if err != nil {
				log.Println("error writing line:", err)
				return
			}
			c.openAttemptMade.UpdateToMaxWith(time.Now())
		}
		c.conn.SetWriteDeadline(time.Now().Add(Timing_IODeadline))
		c.Log("writing server ok")
		_, err := c.conn.Write([]byte("server ok\n"))
		if err != nil {
			log.Println("error writing line:", err)
			return
		}
	}
}

func (c *controller) doCloses() {
	c.server.activeController.Wait(func(cc *controller) bool {
		return c != cc
	})
	c.Log("closing socket")
	c.conn.Close()
}

func mainE() error {
	err := godotenv.Load()
	if err != nil {
		return err
	}
	cfg := config{}
	err = env.Parse(&cfg)
	if err != nil {
		return err
	}
	s := NewServer()
	r := http.NewServeMux()
	r.HandleFunc("GET /health", func(w http.ResponseWriter, req *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write([]byte("ok\n"))
	})
	r.HandleFunc("PUT /api/v0/controller/register", func(w http.ResponseWriter, req *http.Request) {
		if req.Header.Get("authorization") != fmt.Sprintf("MeLaan %s", cfg.ControllerPassword) {
			w.WriteHeader(http.StatusUnauthorized)
			w.Write([]byte("unauthorized"))
			return
		}
		if req.Header.Get("upgrade") != "MeLaan" {
			w.Header().Add("upgrade", "MeLaan")
			w.WriteHeader(http.StatusUpgradeRequired)
			return
		}
		h, ok := w.(http.Hijacker)
		if !ok {
			log.Println("http.ResponseWriter does not implement http.Hijacker")
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		conn, stream, err := h.Hijack()
		if err != nil {
			log.Println("unable to hijack http.ResponseWriter:", err)
			return
		}
		_, err = conn.Write([]byte("HTTP/1.1 101 Switching Protocols\r\nUpgrade: MeLaan\r\n\r\n"))
		if err != nil {
			return
		}
		s.RegisterController(conn, stream)
	})
	r.HandleFunc("GET /api/v0/remote/status", func(w http.ResponseWriter, req *http.Request) {
		if req.Header.Get("authorization") != fmt.Sprintf("MeLaan %s", cfg.RemotePassword) && req.Header.Get("authorization") != fmt.Sprintf("MeLaan %s", cfg.RemotePasswordReadOnly) {
			w.WriteHeader(http.StatusUnauthorized)
			w.Write([]byte("unauthorized"))
			return
		}
		ok, status := s.isHealthy()
		if ok {
			w.WriteHeader(http.StatusOK)
		} else {
			w.WriteHeader(http.StatusBadGateway)
		}
		w.Write([]byte(status))
	})
	r.HandleFunc("POST /api/v0/remote/open", func(w http.ResponseWriter, req *http.Request) {
		if req.Header.Get("authorization") != fmt.Sprintf("MeLaan %s", cfg.RemotePassword) {
			w.WriteHeader(http.StatusUnauthorized)
			w.Write([]byte("unauthorized"))
			return
		}
		resp, err := s.Open()
		if err != nil {
			w.WriteHeader(http.StatusBadGateway)
			w.Write([]byte(err.Error()))
			return
		}
		w.WriteHeader(http.StatusCreated)
		w.Write([]byte(resp + "\n"))
	})
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
