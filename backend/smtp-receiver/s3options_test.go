package main

import (
	"testing"

	"github.com/aws/aws-sdk-go-v2/service/s3"
)

// The bug this exists to stop coming back: UsePathStyle was hardcoded true, which is
// correct for MinIO and rejected by AWS for any bucket created after 30 September 2020.
// Against a real bucket every raw .eml upload fails, the receiver answers 451, and
// senders retry for days without ever succeeding — a failure that reads like a network
// problem from both ends and mentions S3 nowhere.
//
// It survived block B's review, block B's live test and a 10,000-message load test,
// because all of those ran against MinIO. Nothing offline could have caught it, so this
// test is the offline thing.
func TestS3OptionsPicksAddressingStyleFromTheEndpoint(t *testing.T) {
	cases := []struct {
		name          string
		endpoint      string
		wantPathStyle bool
		wantEndpoint  bool // whether BaseEndpoint should be set at all
	}{
		{
			name:          "empty endpoint means real AWS",
			endpoint:      "",
			wantPathStyle: false,
			wantEndpoint:  false,
		},
		{
			name:          "MinIO over http",
			endpoint:      "http://localhost:9000",
			wantPathStyle: true,
			wantEndpoint:  true,
		},
		{
			name:          "MinIO by container name, which is what compose uses",
			endpoint:      "http://minio:9000",
			wantPathStyle: true,
			wantEndpoint:  true,
		},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			var o s3.Options
			s3Options(config{s3Endpoint: c.endpoint})(&o)

			if o.UsePathStyle != c.wantPathStyle {
				t.Errorf("UsePathStyle = %v, want %v", o.UsePathStyle, c.wantPathStyle)
			}
			if (o.BaseEndpoint != nil) != c.wantEndpoint {
				t.Errorf("BaseEndpoint set = %v, want %v", o.BaseEndpoint != nil, c.wantEndpoint)
			}
			if c.wantEndpoint && *o.BaseEndpoint != c.endpoint {
				t.Errorf("BaseEndpoint = %q, want %q", *o.BaseEndpoint, c.endpoint)
			}
		})
	}
}

// The two settings must never disagree. Path-style against AWS is the outage above;
// an endpoint set without path style would send MinIO a bucket.host name it cannot
// resolve. Asserted as one property so a future edit cannot change only one of them.
func TestS3OptionsNeverSetsPathStyleWithoutAnEndpoint(t *testing.T) {
	for _, endpoint := range []string{"", "http://minio:9000", "https://storage.example.internal"} {
		var o s3.Options
		s3Options(config{s3Endpoint: endpoint})(&o)
		if o.UsePathStyle && o.BaseEndpoint == nil {
			t.Errorf("endpoint %q: path style with no endpoint", endpoint)
		}
		if !o.UsePathStyle && o.BaseEndpoint != nil {
			t.Errorf("endpoint %q: custom endpoint without path style", endpoint)
		}
	}
}

// KAFKA_REPLICATION defaults to 1 and must stay there while HLD §14 deploys one broker.
// CreateTopic returns an error for a replication factor it cannot satisfy and main()
// calls log.Fatalf on it, so a well-meaning "3 is more production-ready" edit does not
// degrade — the receiver simply never starts, and no mail is accepted at all.
func TestKafkaReplicationDefaultsToOneBroker(t *testing.T) {
	t.Setenv("KAFKA_REPLICATION", "")
	if got := loadConfig().kafkaReplication; got != 1 {
		t.Errorf("kafkaReplication = %d, want 1 — §14 deploys a single Redpanda node", got)
	}
}
