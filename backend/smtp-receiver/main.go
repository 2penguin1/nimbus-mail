// Command smtp-receiver is Nimbus's front door: the server that other mail servers on
// the internet connect to.
//
// It does three things and deliberately nothing else (docs/HLD.md §9.1):
//
//  1. At RCPT TO, decide whether we accept this recipient — the LAST moment a message
//     can be refused, because after DATA the sender hangs up and is gone.
//  2. Stream the message straight into S3 as it arrives. Never hold it all in memory.
//  3. Publish a "mail.received" event to Kafka, then close the connection.
//
// All the thinking — MIME parsing, dedup, routing, delivery — happens later in the
// Python worker. This process stays dumb and fast so it can hold thousands of slow
// connections at once.
package main

import (
	"context"
	"errors"
	"log"
	"net"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/feature/s3/manager"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/emersion/go-smtp"
	"github.com/redis/go-redis/v9"
	"github.com/twmb/franz-go/pkg/kadm"
	"github.com/twmb/franz-go/pkg/kerr"
	"github.com/twmb/franz-go/pkg/kgo"
	"golang.org/x/net/netutil"
)

type config struct {
	smtpAddr         string
	smtpDomain       string
	redisAddr        string
	s3Endpoint       string
	s3Bucket         string
	s3AccessKey      string
	s3SecretKey      string
	kafkaSeeds       []string
	kafkaTopic       string
	kafkaPartitions  int32
	kafkaReplication int16
	maxBytes         int64
	maxConns         int
}

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func mustInt(s string) int64 {
	v, err := strconv.ParseInt(s, 10, 64)
	if err != nil {
		log.Fatalf("expected a number, got %q", s)
	}
	return v
}

func loadConfig() config {
	// Defaults match infra/docker-compose.yml. Redis is on 6380 because this machine
	// runs a native redis-server on 6379 — see CLAUDE.md.
	maxMB := mustInt(env("MAX_MESSAGE_MB", "40"))
	return config{
		smtpAddr:         env("SMTP_ADDR", ":2525"),
		smtpDomain:       env("SMTP_DOMAIN", "nimbus.local"),
		redisAddr:        env("REDIS_ADDR", "localhost:6380"),
		s3Endpoint:       env("S3_ENDPOINT", "http://localhost:9000"),
		s3Bucket:         env("S3_RAW_BUCKET", "nimbus-raw"),
		s3AccessKey:      env("S3_ACCESS_KEY", "nimbus"),
		s3SecretKey:      env("S3_SECRET_KEY", "nimbus-dev-secret"),
		kafkaSeeds:       strings.Split(env("KAFKA_SEEDS", "localhost:19092"), ","),
		kafkaTopic:       env("KAFKA_TOPIC", "mail.received"),
		kafkaPartitions:  int32(mustInt(env("KAFKA_PARTITIONS", "4"))),
		kafkaReplication: int16(mustInt(env("KAFKA_REPLICATION", "1"))),
		maxBytes:         maxMB * 1024 * 1024,
		maxConns:         int(mustInt(env("MAX_CONNECTIONS", "100"))),
	}
}

// s3Options decides between real S3 and a MinIO-shaped endpoint. S3_ENDPOINT alone
// decides it: empty means AWS, anything else means a custom endpoint.
//
// This used to be unconditional `UsePathStyle = true`, which is right for MinIO and
// WRONG for AWS. MinIO serves buckets as host/bucket/key; S3 buckets created after
// 30 September 2020 support only bucket.host/key and reject the path form. Against a
// real bucket every raw .eml upload would have failed, the receiver would have answered
// 451 (§9.1a), and senders would have retried for days without ever succeeding — a
// failure that looks like a network problem from both ends.
//
// One variable, not two, and the same variable the Python side reads. A separate
// S3_FORCE_PATH_STYLE flag would let the endpoint and the addressing style disagree,
// which is a combination nobody wants and somebody would eventually set.
func s3Options(cfg config) func(*s3.Options) {
	return func(o *s3.Options) {
		if cfg.s3Endpoint == "" {
			// Real AWS. Leave BaseEndpoint nil so the SDK resolves the regional
			// endpoint itself, and leave UsePathStyle false, which is what S3 wants.
			return
		}
		o.BaseEndpoint = aws.String(cfg.s3Endpoint)
		o.UsePathStyle = true
	}
}

// ensureTopic creates the topic if it is not already there.
//
// We do not rely on Kafka's auto-create. It is off by default on Redpanda and is
// routinely disabled on production clusters, and when it is off the failure arrives
// as UNKNOWN_TOPIC_OR_PARTITION on the first real message — that is, on live mail
// rather than at startup. Creating it here fails loudly before we accept anything.
//
// Partitions set the ceiling on how many workers can consume in parallel; raising it
// later is easy, lowering it is not.
//
// KAFKA_REPLICATION must be <= the number of brokers, and it stays 1 everywhere we run,
// because HLD §14 deploys ONE Redpanda node. An earlier version of this comment said it
// "should be 3 in production", which is true of a three-broker cluster and a foot-gun
// here: CreateTopic returns an error for a replication factor it cannot satisfy,
// main() log.Fatalf's on it, and the receiver never starts. Raise this only alongside
// the broker count.
func ensureTopic(ctx context.Context, kc *kgo.Client, cfg config) error {
	adm := kadm.NewClient(kc)

	// CreateTopic folds the per-topic error into its SECOND return value — its doc
	// says it "returns the kerr.ErrorForCode(response.ErrorCode)". So the
	// already-exists case arrives as err, not as resp.Err, and checking resp.Err is
	// unreachable. Getting this wrong made the receiver refuse to start on its
	// second run, which is every run after the first.
	_, err := adm.CreateTopic(ctx, cfg.kafkaPartitions, cfg.kafkaReplication, nil, cfg.kafkaTopic)
	if err != nil {
		// Compared by error CODE. kerr.Error has no Is method, so errors.Is would
		// fall back to pointer identity and is not something to rely on.
		var kerrErr *kerr.Error
		if !errors.As(err, &kerrErr) || kerrErr.Code != kerr.TopicAlreadyExists.Code {
			return err
		}
	}
	log.Printf("topic %q ready (%d partitions, replication %d)",
		cfg.kafkaTopic, cfg.kafkaPartitions, cfg.kafkaReplication)
	return nil
}

// Deps is everything a session needs. Built once, shared by every connection.
type Deps struct {
	Addresses *Addresses
	Store     *Store
	Events    *Events
	MaxBytes  int64
}

func main() {
	cfg := loadConfig()
	log.SetPrefix("smtp-receiver ")
	log.SetFlags(log.LstdFlags | log.Lmsgprefix)

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	// ---- Redis: answers "do we accept this recipient?" in well under a millisecond.
	rdb := redis.NewClient(&redis.Options{Addr: cfg.redisAddr})
	if err := rdb.Ping(ctx).Err(); err != nil {
		log.Fatalf("cannot reach Redis at %s: %v", cfg.redisAddr, err)
	}
	defer rdb.Close()

	// ---- S3, or MinIO. Which one is decided by S3_ENDPOINT alone — see s3Options.
	s3Client := s3.NewFromConfig(aws.Config{
		Region:      env("AWS_REGION", "us-east-1"),
		Credentials: credentials.NewStaticCredentialsProvider(cfg.s3AccessKey, cfg.s3SecretKey, ""),
	}, s3Options(cfg))

	// PartSize x (Concurrency+1) is the SDK's documented memory ceiling per upload.
	// 5 MB is S3's minimum part size, and concurrency 1 keeps each connection cheap —
	// we would rather hold many connections than upload any single one quickly.
	// Result: ~10 MB per in-flight message, whether it is 1 MB or 40 MB.
	uploader := manager.NewUploader(s3Client, func(u *manager.Uploader) {
		u.PartSize = 5 * 1024 * 1024
		u.Concurrency = 1
	})

	// ---- Kafka. The event is tiny; the message itself already went to S3.
	kc, err := kgo.NewClient(
		kgo.SeedBrokers(cfg.kafkaSeeds...),
		kgo.DefaultProduceTopic(cfg.kafkaTopic),
		// Wait for all replicas. Losing an event means losing mail we already
		// told the sender we had accepted.
		kgo.RequiredAcks(kgo.AllISRAcks()),
		kgo.ProducerLinger(0), // one message per email; batching would only add delay
	)
	if err != nil {
		log.Fatalf("cannot create Kafka client: %v", err)
	}
	defer kc.Close()
	if err := kc.Ping(ctx); err != nil {
		log.Fatalf("cannot reach Kafka at %s: %v", cfg.kafkaSeeds, err)
	}
	if err := ensureTopic(ctx, kc, cfg); err != nil {
		log.Fatalf("cannot ensure topic %s: %v", cfg.kafkaTopic, err)
	}

	deps := &Deps{
		Addresses: &Addresses{rdb: rdb},
		Store:     &Store{uploader: uploader, bucket: cfg.s3Bucket},
		Events:    &Events{client: kc, topic: cfg.kafkaTopic},
		MaxBytes:  cfg.maxBytes,
	}

	srv := smtp.NewServer(&Backend{deps: deps})
	srv.Addr = cfg.smtpAddr
	srv.Domain = cfg.smtpDomain
	// Generous but finite. A stalled peer must not hold a connection forever.
	srv.ReadTimeout = 5 * time.Minute
	srv.WriteTimeout = 5 * time.Minute
	// 40 MB, not 25. A 25 MB attachment is base64-encoded on the wire, which inflates
	// binary by about a third, so a "25 MB attachment" arrives as ~34 MB of message.
	// A literal 25 MB cap would reject exactly the case docs/OVERVIEW.md advertises.
	srv.MaxMessageBytes = cfg.maxBytes
	srv.MaxRecipients = 100
	// No AUTH: we are an inbound MX. Sending servers do not have accounts with us.
	// Not implementing AuthMechanisms means AUTH is never advertised.

	// A hard ceiling on open connections, because there is no other one.
	//
	// A connection that is actively streaming holds PartSize x (Concurrency+1) = 10 MB
	// of upload buffer — measured at 14 MB. An idle one holds almost nothing, but it
	// can sit there for the full 5-minute ReadTimeout. Without a cap, an attacker opens
	// sockets until we run out of file descriptors, and no real mail gets through.
	//
	// 100 x 14 MB = 1.4 GB if every slot streams at once. HLD §9.1a records that this
	// needs more memory than §14's shared t3.small gives it — set MAX_CONNECTIONS=50
	// there until the receiver has its own instance.
	//
	// Trade-off: LimitListener makes connection 201 WAIT in the TCP backlog rather
	// than answering "421 too busy". A rejecting limiter would be politer, but real
	// sending servers retry for days either way, and this is five lines instead of a
	// hand-written accept loop.
	ln, err := net.Listen("tcp", cfg.smtpAddr)
	if err != nil {
		log.Fatalf("cannot listen on %s: %v", cfg.smtpAddr, err)
	}
	ln = netutil.LimitListener(ln, cfg.maxConns)

	go func() {
		log.Printf("listening on %s (domain %s, max %d MB, max %d connections)",
			cfg.smtpAddr, cfg.smtpDomain, cfg.maxBytes/1024/1024, cfg.maxConns)
		// A clean Shutdown makes this return nil, so a non-nil error here means the
		// listener died. Exiting 0 on that would tell the supervisor we finished
		// successfully and leave a container that is up, healthy-looking, and
		// accepting no mail at all.
		if err := srv.Serve(ln); err != nil {
			log.Fatalf("listener stopped: %v", err)
		}
	}()

	<-ctx.Done()
	log.Println("shutting down, letting in-flight messages finish")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Printf("shutdown: %v", err)
	}
}
