package main

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/twmb/franz-go/pkg/kadm"
	"github.com/twmb/franz-go/pkg/kerr"
	"github.com/twmb/franz-go/pkg/kgo"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
)

func TestAPaymentsEventsGoToThePartitionLibrdkafkaPicks(t *testing.T) {
	// Python's zlib.crc32(key) % n, the same CRC-32 librdkafka's default partitioner uses. The first three pairs were
	// recorded by the Python receiver on the stack (evidence/week6, "webhook queued" lines: key, partition).
	cases := []struct {
		key        string
		partitions int
		want       int
	}{
		{"pay_000247", 3, 1}, {"pay_000259", 3, 1}, {"pay_000279", 3, 0},
		{"pay_000001", 3, 0}, {"pay_000003", 3, 1}, {"evt_000001", 3, 1},
		{"pay_000001", 7, 3}, {"pay_000279", 7, 5}, {"pay_123456", 7, 5},
	}
	partitioner := librdkafkaPartitioner().ForTopic(eventsTopic)
	for _, c := range cases {
		record := &kgo.Record{Key: []byte(c.key)}
		if got := partitioner.Partition(record, c.partitions); got != c.want {
			t.Errorf("%s of %d: %d, want %d", c.key, c.partitions, got, c.want)
		}
		if !partitioner.RequiresConsistency(record) {
			t.Error("a key may move to another partition") // never, even while its partition is down
		}
	}
}

func TestTheRecordIsTheSignedBytesKeyedByPaymentWithItsHeadersInOrder(t *testing.T) {
	value := []byte(`{"id":"evt_000001"}`)
	headers := []kafkaHeader{{"content-type", "application/json"}, {"received-at", "now"}, {"traceparent", "00-a-b-01"}}
	record := newRecord(eventsTopic, "pay_000001", value, headers)
	if record.Topic != eventsTopic || string(record.Key) != "pay_000001" || &record.Value[0] != &value[0] {
		t.Errorf("record %s %s, the value copied", record.Topic, record.Key) // the exact bytes, never re-encoded
	}
	if len(record.Headers) != 3 {
		t.Fatalf("headers %v", record.Headers)
	}
	for i, h := range headers {
		if record.Headers[i].Key != h.name || string(record.Headers[i].Value) != h.value {
			t.Errorf("header %d: %v", i, record.Headers[i])
		}
	}
}

func TestWithKafkaDownTheWriteTimesOutWithinThreeSecondsAndHealthSaysSo(t *testing.T) {
	// With no broker, the client gives up on the record after its 3 s delivery timeout, before the receiver's own 4 s
	// wait ends: the provider gets its 503 before its own 5 s timeout. (About 3 s, on a real client.)
	c := newCapture(t)
	publisher, err := newKafkaPublisher("127.0.0.1:1", service, c.logs) // nothing listens on port 1
	if err != nil {
		t.Fatal(err)
	}
	defer publisher.Close()
	started := time.Now()
	_, _, err = publisher.Publish(eventsTopic, "pay_000001", []byte("{}"), nil)
	waited := time.Since(started)
	if _, isPublishError := err.(publishError); !isPublishError {
		t.Fatalf("got %v, want a publishError", err)
	}
	if !strings.HasPrefix(err.Error(), kgo.ErrRecordTimeout.Error()) || waited >= ackTimeout {
		t.Errorf("after %v: %v", waited, err) // not the delivery timeout: the 4 s backstop, or something else
	}
	if publisher.Healthy() {
		t.Error("healthy with no broker")
	}
	if !strings.Contains(c.stdout.String(), `"msg": "kafka client error"`) {
		t.Errorf("no kafka client error line: %s", c.stdout.String())
	}
}

func TestTheBootstrapSettingNamesOneOrMoreBrokers(t *testing.T) {
	if got := seeds("kafka:9092"); len(got) != 1 || got[0] != "kafka:9092" {
		t.Errorf("%v", got)
	}
	if got := seeds(" a:1, b:2 ,"); len(got) != 2 || got[0] != "a:1" || got[1] != "b:2" {
		t.Errorf("%v", got)
	}
}

// fakeAdmin answers the three offset requests from fixed numbers.
type fakeAdmin struct {
	start, end   map[int32]int64
	committed    map[string]map[int32]int64 // by group
	fail         map[string]error           // by group
	missingTopic bool
	asked        []string
}

func listedOffsets(topic string, offsets map[int32]int64) kadm.ListedOffsets {
	out := kadm.ListedOffsets{topic: {}}
	for partition, offset := range offsets {
		out[topic][partition] = kadm.ListedOffset{Topic: topic, Partition: partition, Offset: offset}
	}
	return out
}

func (f *fakeAdmin) ListStartOffsets(_ context.Context, topics ...string) (kadm.ListedOffsets, error) {
	return listedOffsets(topics[0], f.start), nil
}

func (f *fakeAdmin) ListEndOffsets(_ context.Context, topics ...string) (kadm.ListedOffsets, error) {
	if f.missingTopic {
		return kadm.ListedOffsets{topics[0]: {-1: {Topic: topics[0], Partition: -1, Err: kerr.UnknownTopicOrPartition}}}, nil
	}
	return listedOffsets(topics[0], f.end), nil
}

func (f *fakeAdmin) FetchOffsets(_ context.Context, group string) (kadm.OffsetResponses, error) {
	f.asked = append(f.asked, group)
	if err := f.fail[group]; err != nil {
		return nil, err
	}
	out := kadm.OffsetResponses{eventsTopic: {}}
	for partition, at := range f.committed[group] {
		out[eventsTopic][partition] = kadm.OffsetResponse{Offset: kadm.Offset{Topic: eventsTopic, Partition: partition, At: at}}
	}
	return out, nil
}

func TestLagIsCountedFromTheCommitOrFromTheStartOfTheLog(t *testing.T) {
	admin := &fakeAdmin{
		start: map[int32]int64{0: 0, 1: 5, 2: 0},
		end:   map[int32]int64{0: 10, 1: 12, 2: 7},
		committed: map[string]map[int32]int64{
			"ledger":            {0: 10, 2: 3}, // partition 1 never committed: from its start, 5
			"payment-confirmer": {0: 4, 1: -1, 2: 7},
		},
	}
	c := newCapture(t)
	got := lagValues(context.Background(), admin, consumerGroups, c.logs)
	want := []lagValue{
		{"ledger", 0, 0}, {"ledger", 1, 7}, {"ledger", 2, 4},
		{"payment-confirmer", 0, 6}, {"payment-confirmer", 1, 7}, {"payment-confirmer", 2, 0},
	}
	if len(got) != len(want) {
		t.Fatalf("got %v", got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("%d: got %v, want %v", i, got[i], want[i])
		}
	}
}

func TestLagMeasuringStopsAtTheFirstGroupItCannotMeasure(t *testing.T) {
	// Kafka is down: one timeout is enough, rather than one per group.
	c := newCapture(t)
	admin := &fakeAdmin{fail: map[string]error{"ledger": errors.New("kafka unreachable")}}
	if got := lagValues(context.Background(), admin, consumerGroups, c.logs); len(got) != 0 || len(admin.asked) != 1 {
		t.Errorf("values %v, asked %v", got, admin.asked)
	}
	line := c.lines()[0]
	if !strings.Contains(line, `"logger": "klaxon.kafka", "msg": "could not measure consumer lag", "group": "ledger", "error": "kafka unreachable"`) {
		t.Errorf("%s", line)
	}
	// The second group fails: the first one's values are kept.
	admin = &fakeAdmin{start: map[int32]int64{0: 0}, end: map[int32]int64{0: 3},
		fail: map[string]error{"payment-confirmer": errors.New("kafka unreachable")}}
	if got := lagValues(context.Background(), admin, consumerGroups, c.logs); len(got) != 1 || got[0] != (lagValue{"ledger", 0, 3}) {
		t.Errorf("values %v", got)
	}
}

func TestATopicThatDoesNotExistCannotBeMeasured(t *testing.T) {
	admin := &fakeAdmin{missingTopic: true}
	if _, err := consumerLag(context.Background(), admin, "ledger", eventsTopic); err == nil ||
		err.Error() != "topic provider-events not found" {
		t.Errorf("%v", err)
	}
	empty := &fakeAdmin{end: map[int32]int64{}}
	if _, err := consumerLag(context.Background(), empty, "ledger", eventsTopic); err == nil {
		t.Error("a topic with no partitions was measured")
	}
}

func TestTheLagGaugeIsReportedByConsumerAndPartition(t *testing.T) {
	c := newCapture(t)
	admin := &fakeAdmin{start: map[int32]int64{0: 0, 1: 0}, end: map[int32]int64{0: 4, 1: 0},
		committed: map[string]map[int32]int64{"ledger": {0: 0, 1: 0}, "payment-confirmer": {0: 4, 1: 0}}}
	if err := observeConsumerLag(c.p.metrics, admin, c.logs); err != nil {
		t.Fatal(err)
	}
	found := map[string]int64{}
	for _, scope := range c.metrics(t).ScopeMetrics {
		for _, m := range scope.Metrics {
			gauge, ok := m.Data.(metricdata.Gauge[int64])
			if !ok || m.Name != "klaxon.kafka.consumer.lag" {
				continue
			}
			if scope.Scope.Name != "klaxon.kafka" || m.Unit != "{message}" {
				t.Errorf("scope %s, unit %s", scope.Scope.Name, m.Unit)
			}
			for _, p := range gauge.DataPoints {
				group, _ := p.Attributes.Value("consumer")
				partition, _ := p.Attributes.Value("partition")
				if partition.Type() != attribute.STRING {
					t.Errorf("partition is a %v, not a string", partition.Type())
				}
				found[group.AsString()+"/"+partition.Emit()] = p.Value
			}
		}
	}
	want := map[string]int64{"ledger/0": 4, "ledger/1": 0, "payment-confirmer/0": 0, "payment-confirmer/1": 0}
	if len(found) != len(want) {
		t.Fatalf("found %v", found)
	}
	for k, v := range want {
		if found[k] != v {
			t.Errorf("%s: %d, want %d", k, found[k], v)
		}
	}
}
