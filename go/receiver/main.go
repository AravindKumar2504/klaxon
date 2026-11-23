// The webhook receiver of Klaxon's payments system, in Go (week 8, item 2). It does what
// src/klaxon/patient/services/receiver_app.py does, and answers the same: that Python receiver stays in the repo as
// the reference, and testdata/shared_cases.json holds the cases both must answer alike.
//
// It checks the provider's signature, writes the event to Kafka, and answers 200 only once Kafka has acknowledged
// the write (503 if Kafka is down, so the provider sends it again later). It also measures every consumer group's lag.
//
//	POST /webhooks/provider
//	GET  /healthz
//
// Settings: KAFKA_BOOTSTRAP (kafka:9092), KAFKA_TOPIC (provider-events), WEBHOOK_SECRET_VERSION (v1; v1,v2 during a
// rotation), WEBHOOK_SECRET_V1, WEBHOOK_SECRET_V2, SIGNATURE_TOLERANCE_SECONDS (300), PORT (8000), LOG_LEVEL (INFO),
// and OpenTelemetry's own OTEL_* variables.
//
// Run: /receiver           serve, until SIGTERM or Ctrl-C
//
//	/receiver -health   the container's health check: exit 0 when GET /healthz answers 200
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/twmb/franz-go/pkg/kadm"
	"github.com/twmb/franz-go/pkg/kgo"
)

const service = "webhook-receiver"

func main() {
	health := flag.Bool("health", false, "check GET /healthz on this container's port; exit 0 when it answers 200")
	flag.Parse()
	if *health {
		os.Exit(checkHealth())
	}
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "%s: %v\n", service, err)
		os.Exit(1)
	}
}

// checkHealth asks this container's own receiver for GET /healthz, as Compose and Kubernetes check it. There is no
// shell or Python in the image to do it.
func checkHealth() int {
	port, err := settingNumber(os.LookupEnv, "PORT", 8000)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	client := &http.Client{Timeout: 2 * time.Second, Transport: &http.Transport{}} // no proxy, whatever is set
	answer, err := client.Get("http://127.0.0.1:" + strconv.Itoa(port) + healthRoute)
	if err != nil {
		fmt.Fprintln(os.Stderr, "unhealthy:", err)
		return 1
	}
	answer.Body.Close()
	if answer.StatusCode != http.StatusOK {
		fmt.Fprintln(os.Stderr, "unhealthy:", answer.Status)
		return 1
	}
	return 0
}

func run() error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, os.Interrupt)
	defer stop()
	telemetry, err := setupTelemetry(ctx, service)
	if err != nil {
		return err
	}
	level, isSet := os.LookupEnv("LOG_LEVEL")
	logs, err := newLogs(service, level, isSet, telemetry.logs, os.Stdout)
	if err != nil {
		return err
	}
	reportSDKErrors(logs)
	tel, err := newInstruments(telemetry)
	if err != nil {
		return err
	}

	bootstrap, err := settingText(os.LookupEnv, "KAFKA_BOOTSTRAP", "kafka:9092")
	if err != nil {
		return err
	}
	publisher, err := newKafkaPublisher(bootstrap, service, logs)
	if err != nil {
		return err
	}
	defer publisher.Close()
	admin, err := kgo.NewClient(kgo.SeedBrokers(seeds(bootstrap)...))
	if err != nil {
		return err
	}
	defer admin.Close()
	if err := observeConsumerLag(telemetry.metrics, kadm.NewClient(admin), logs); err != nil {
		return err
	}

	secrets, err := secretsFromEnv(os.Getenv)
	if err != nil {
		return err
	}
	topic, err := settingText(os.LookupEnv, "KAFKA_TOPIC", eventsTopic)
	if err != nil {
		return err
	}
	tolerance, err := settingNumber(os.LookupEnv, "SIGNATURE_TOLERANCE_SECONDS", defaultToleranceSeconds)
	if err != nil {
		return err
	}
	port, err := settingNumber(os.LookupEnv, "PORT", 8000)
	if err != nil {
		return err
	}
	receiver := &Receiver{publisher, secrets, topic, int64(tolerance), time.Now, tel, logs}

	listener, err := net.Listen("tcp", "0.0.0.0:"+strconv.Itoa(port))
	if err != nil {
		return err
	}
	server := &http.Server{Handler: receiver, ReadHeaderTimeout: 10 * time.Second, IdleTimeout: 5 * time.Second,
		MaxHeaderBytes: 16 << 10}
	served := make(chan error, 1)
	go func() { served <- server.Serve(listener) }()
	logs.log(context.Background(), logger, levelInfo, "listening", field{"port", port})

	select {
	case err := <-served:
		return err
	case <-ctx.Done(): // SIGTERM: finish the requests in flight, then send the telemetry still buffered
	}
	logs.log(context.Background(), logger, levelInfo, "shutting down")
	finish, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	if err := server.Shutdown(finish); err != nil && !errors.Is(err, context.DeadlineExceeded) {
		return err
	}
	return telemetry.shutdown(finish)
}
