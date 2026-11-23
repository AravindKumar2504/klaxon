package main

// Kafka, as src/klaxon/patient/services/kafka_io.py uses it: a producer that waits for the broker's acknowledgement,
// and every consumer group's lag. The client is franz-go, a Kafka client written in Go (no C library).
//
// Lag is how far a group's committed place in the log is behind the end. Two groups read provider-events, each with
// its own place: "ledger" books the money, "payment-confirmer" marks our payments succeeded. It is measured from
// outside the consumers, from the committed offsets, so a consumer stuck on one message, or stopped, still shows it.

import (
	"context"
	"errors"
	"fmt"
	"hash/crc32"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/twmb/franz-go/pkg/kadm"
	"github.com/twmb/franz-go/pkg/kerr"
	"github.com/twmb/franz-go/pkg/kgo"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
)

const (
	eventsTopic = "provider-events"
	ackTimeout  = 4 * time.Second // the longest the receiver waits for Kafka before answering 503
	lagTimeout  = 2 * time.Second // per group, on the metrics' collection: with Kafka down, give up fast
)

// consumerGroups: every group that reads provider-events.
var consumerGroups = []string{"ledger", "payment-confirmer"}

// publishError means Kafka did not confirm the write: the caller must not acknowledge the event.
type publishError string

func (e publishError) Error() string { return string(e) }

type kafkaPublisher struct{ client *kgo.Client }

// newKafkaPublisher makes the producer. It connects when it is first used.
func newKafkaPublisher(bootstrap, clientID string, l *logs) (*kafkaPublisher, error) {
	client, err := kgo.NewClient(
		kgo.SeedBrokers(seeds(bootstrap)...),
		kgo.ClientID(clientID),
		// Every in-sync copy has the write before the broker says so (acks=all). The producer is idempotent, as
		// franz-go's always is with acks=all: the broker drops duplicates caused by the producer's own retries.
		kgo.RequiredAcks(kgo.AllISRAcks()),
		kgo.ProducerLinger(0), // send at once: someone is waiting for the answer
		kgo.ProduceRequestTimeout(2*time.Second),
		// Give up within 3 s, so the receiver can answer 503 before the provider's own 5 s timeout.
		kgo.RecordDeliveryTimeout(3*time.Second),
		kgo.RecordPartitioner(librdkafkaPartitioner()),
		kgo.WithLogger(kafkaLogger{l}),
	)
	if err != nil {
		return nil, err
	}
	return &kafkaPublisher{client}, nil
}

// seeds are the brokers KAFKA_BOOTSTRAP names, comma-separated.
func seeds(bootstrap string) []string {
	var out []string
	for _, item := range strings.Split(bootstrap, ",") {
		if item = strings.TrimSpace(item); item != "" {
			out = append(out, item)
		}
	}
	return out
}

// librdkafkaPartitioner picks the partition the Python receiver's Kafka library (librdkafka) picks for a key: the
// key's CRC-32, modulo the number of partitions. So a payment's events go where they went before.
func librdkafkaPartitioner() kgo.Partitioner {
	return kgo.BasicConsistentPartitioner(func(string) func(*kgo.Record, int) int {
		return func(r *kgo.Record, n int) int { return partitionFor(r.Key, n) }
	})
}

func partitionFor(key []byte, partitions int) int {
	return int(crc32.ChecksumIEEE(key) % uint32(partitions))
}

// newRecord is the message: the exact signed bytes, keyed by payment, with its headers in order.
func newRecord(topic, key string, value []byte, headers []kafkaHeader) *kgo.Record {
	record := &kgo.Record{Topic: topic, Key: []byte(key), Value: value}
	for _, h := range headers {
		record.Headers = append(record.Headers, kgo.RecordHeader{Key: h.name, Value: []byte(h.value)})
	}
	return record
}

// Publish writes one message and waits for the broker's acknowledgement: it returns the partition and offset.
func (p *kafkaPublisher) Publish(topic, key string, value []byte, headers []kafkaHeader) (int32, int64, error) {
	record := newRecord(topic, key, value, headers)
	ctx, cancel := context.WithTimeout(context.Background(), ackTimeout)
	defer cancel()
	done := make(chan error, 1)
	p.client.Produce(ctx, record, func(_ *kgo.Record, err error) { done <- err })
	select {
	case err := <-done:
		if err != nil {
			return 0, 0, publishError(err.Error())
		}
		return record.Partition, record.Offset, nil
	case <-ctx.Done():
		return 0, 0, publishError("no acknowledgement from Kafka in time")
	}
}

// Healthy: a broker answers a metadata request within 2 s.
func (p *kafkaPublisher) Healthy() bool {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	return p.client.Ping(ctx) == nil
}

func (p *kafkaPublisher) Close() { p.client.Close() }

// kafkaLogger passes franz-go's warnings and errors on as "kafka client error" lines (a broker it cannot reach, say),
// as the Python receiver logs its Kafka library's errors.
type kafkaLogger struct{ logs *logs }

func (kafkaLogger) Level() kgo.LogLevel { return kgo.LogLevelWarn }

func (k kafkaLogger) Log(_ kgo.LogLevel, msg string, keyvals ...any) {
	text := msg
	for i := 0; i+1 < len(keyvals); i += 2 {
		text += fmt.Sprintf(" %v=%v", keyvals[i], keyvals[i+1])
	}
	k.logs.log(context.Background(), "klaxon.kafka", levelWarning, "kafka client error", field{"error", text})
}

// offsetReader is what measuring lag needs from Kafka's admin API. kadm.Client has these methods; tests use a fake.
type offsetReader interface {
	ListStartOffsets(ctx context.Context, topics ...string) (kadm.ListedOffsets, error)
	ListEndOffsets(ctx context.Context, topics ...string) (kadm.ListedOffsets, error)
	FetchOffsets(ctx context.Context, group string) (kadm.OffsetResponses, error)
}

// consumerLag is, per partition, how many messages of the topic the group has not committed yet. A partition the
// group never committed counts from the start of the log: everything there is lag.
func consumerLag(ctx context.Context, admin offsetReader, group, topic string) (map[int32]int64, error) {
	committed, err := admin.FetchOffsets(ctx, group)
	if err != nil {
		return nil, err
	}
	listed, err := admin.ListEndOffsets(ctx, topic)
	end, err := byPartition(topic, listed, err)
	if err != nil {
		return nil, err
	}
	listed, err = admin.ListStartOffsets(ctx, topic)
	start, err := byPartition(topic, listed, err)
	if err != nil {
		return nil, err
	}
	lag := make(map[int32]int64, len(end))
	for partition, last := range end {
		place := start[partition]
		if commit, ok := committed.Lookup(topic, partition); ok && commit.At >= 0 {
			place = commit.At
		}
		lag[partition] = last - place
	}
	return lag, nil
}

// byPartition is one topic's listed offsets, per partition. A topic that does not exist is an error.
func byPartition(topic string, listed kadm.ListedOffsets, err error) (map[int32]int64, error) {
	if err != nil {
		return nil, err
	}
	out := map[int32]int64{}
	for partition, offset := range listed[topic] {
		if errors.Is(offset.Err, kerr.UnknownTopicOrPartition) {
			return nil, fmt.Errorf("topic %s not found", topic)
		}
		if offset.Err != nil {
			return nil, offset.Err
		}
		out[partition] = offset.Offset
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("topic %s not found", topic)
	}
	return out, nil
}

// lagValue is one group's lag on one partition.
type lagValue struct {
	group     string
	partition int32
	behind    int64
}

// lagValues measures each group's lag. It stops at the first group it cannot measure: that means Kafka itself is
// unreachable, and waiting for every other group would hold up the service's other metrics during that outage.
func lagValues(ctx context.Context, admin offsetReader, groups []string, l *logs) []lagValue {
	var out []lagValue
	for _, group := range groups {
		measured, cancel := context.WithTimeout(ctx, lagTimeout)
		lag, err := consumerLag(measured, admin, group, eventsTopic)
		cancel()
		if err != nil { // no value this time, rather than a wrong one
			l.log(context.Background(), "klaxon.kafka", levelWarning, "could not measure consumer lag",
				field{"group", group}, field{"error", err.Error()})
			return out
		}
		partitions := make([]int32, 0, len(lag))
		for partition := range lag {
			partitions = append(partitions, partition)
		}
		slices.Sort(partitions)
		for _, partition := range partitions {
			out = append(out, lagValue{group, partition, lag[partition]})
		}
	}
	return out
}

// observeConsumerLag reports every group's lag as the gauge klaxon.kafka.consumer.lag, by consumer and partition,
// measured each time the metrics are collected (every 10 s on the stack).
func observeConsumerLag(meters metric.MeterProvider, admin offsetReader, l *logs) error {
	_, err := meters.Meter("klaxon.kafka").Int64ObservableGauge("klaxon.kafka.consumer.lag",
		metric.WithUnit("{message}"),
		metric.WithDescription("Messages in provider-events that a consumer group has not committed yet, per partition."),
		metric.WithInt64Callback(func(ctx context.Context, o metric.Int64Observer) error {
			for _, v := range lagValues(ctx, admin, consumerGroups, l) {
				o.Observe(v.behind, metric.WithAttributes(attribute.String("consumer", v.group),
					attribute.String("partition", strconv.Itoa(int(v.partition)))))
			}
			return nil
		}))
	return err
}
