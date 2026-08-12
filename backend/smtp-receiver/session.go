package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/feature/s3/manager"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/emersion/go-smtp"
	"github.com/redis/go-redis/v9"
	"github.com/twmb/franz-go/pkg/kgo"
)

// ---------------------------------------------------------------------------
// Recipient checking
// ---------------------------------------------------------------------------

const (
	keyValidAddresses  = "valid_addresses"
	keyCatchAllDomains = "catch_all_domains"
)

// Addresses answers "do we accept mail for this address?" from Redis.
//
// Postgres knows the same answer, but this runs once per recipient of every message
// while the sending server waits on the line, across thousands of open connections.
// A database query there would exhaust the connection pool and make senders time out.
// The API rebuilds these sets from Postgres on every startup, so Redis is a cache and
// Postgres stays the source of truth (docs/HLD.md §9.6).
type Addresses struct {
	rdb *redis.Client
}

// splitAddress normalises an address and returns its domain.
// Pure function, no I/O — this is where the fiddly cases live, so it is tested.
func splitAddress(addr string) (normalised, domain string, ok bool) {
	normalised = strings.ToLower(strings.TrimSpace(addr))
	// go-smtp strips the angle brackets before it calls us, so these are belt and
	// braces — they keep this function correct as a standalone utility.
	normalised = strings.TrimPrefix(normalised, "<")
	normalised = strings.TrimSuffix(normalised, ">")
	// "sujal@acme.com." is a legal fully-qualified name with the root dot written
	// out. Without this it matches nothing in Redis and we would refuse real mail.
	normalised = strings.TrimSuffix(normalised, ".")

	// LastIndex, not Index: the local part is allowed to contain '@' when quoted,
	// but the domain never is. The last one is always the separator.
	at := strings.LastIndex(normalised, "@")
	if at <= 0 || at == len(normalised)-1 {
		return normalised, "", false
	}
	return normalised, normalised[at+1:], true
}

func (a *Addresses) Accepts(ctx context.Context, addr string) (bool, error) {
	normalised, domain, ok := splitAddress(addr)
	if !ok {
		return false, nil // malformed: not our problem, just refuse it
	}

	isMailbox, err := a.rdb.SIsMember(ctx, keyValidAddresses, normalised).Result()
	if err != nil {
		return false, err
	}
	if isMailbox {
		return true, nil
	}

	// Not a known mailbox or alias. The domain may still catch everything.
	return a.rdb.SIsMember(ctx, keyCatchAllDomains, domain).Result()
}

// ---------------------------------------------------------------------------
// Storing the raw message
// ---------------------------------------------------------------------------

type Store struct {
	uploader *manager.Uploader
	bucket   string
}

// Put streams r into S3. r is read as the upload progresses, so memory stays bounded
// by the uploader's part size no matter how large the message is.
func (s *Store) Put(ctx context.Context, key string, r io.Reader) error {
	_, err := s.uploader.Upload(ctx, &s3.PutObjectInput{
		Bucket:      aws.String(s.bucket),
		Key:         aws.String(key),
		Body:        r,
		ContentType: aws.String("message/rfc822"),
	})
	return err
}

// rawKey builds a unique object key, date-prefixed so the bucket stays browsable and
// lifecycle rules can target old mail.
func rawKey(now time.Time) (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	return fmt.Sprintf("raw/%s/%s.eml", now.UTC().Format("2006/01/02"), hex.EncodeToString(b[:])), nil
}

// countingReader reports how many bytes flowed through it, without keeping any of
// them. Buffering the message just to measure it would defeat the whole design.
type countingReader struct {
	r io.Reader
	n int64
}

func (c *countingReader) Read(p []byte) (int, error) {
	n, err := c.r.Read(p)
	c.n += int64(n)
	return n, err
}

// ---------------------------------------------------------------------------
// Publishing the event
// ---------------------------------------------------------------------------

type Events struct {
	client *kgo.Client
	topic  string
}

// Event is the handoff to the Python worker. Deliberately tiny — it says where the
// message is, not what is in it. s3_key doubles as the worker's idempotency key
// (processed_event.event_key), which is what makes a Kafka replay harmless.
type Event struct {
	S3Key      string    `json:"s3_key"`
	From       string    `json:"from"`
	Recipients []string  `json:"recipients"`
	SizeBytes  int64     `json:"size_bytes"`
	ReceivedAt time.Time `json:"received_at"`
	RemoteAddr string    `json:"remote_addr"`
}

func (e *Events) Publish(ctx context.Context, ev Event) error {
	body, err := json.Marshal(ev)
	if err != nil {
		return err
	}
	// Key by s3_key so a replayed event lands on the same partition, keeping
	// per-message ordering meaningful.
	return e.client.ProduceSync(ctx, &kgo.Record{
		Topic: e.topic,
		Key:   []byte(ev.S3Key),
		Value: body,
	}).FirstErr()
}

// ---------------------------------------------------------------------------
// The SMTP conversation
// ---------------------------------------------------------------------------

type Backend struct {
	deps *Deps
}

func (b *Backend) NewSession(c *smtp.Conn) (smtp.Session, error) {
	remote := ""
	if conn := c.Conn(); conn != nil {
		remote = conn.RemoteAddr().String()
	}
	return &Session{deps: b.deps, remote: remote}, nil
}

type Session struct {
	deps   *Deps
	remote string

	// go-smtp always advertises CHUNKING, so a sender may use BDAT instead of DATA.
	// On that path the library runs Data in its own goroutine while the connection
	// goroutine keeps reading commands — an RSET there calls Reset() concurrently
	// with Data reading the envelope. The mutex is what makes that not a data race.
	mu         sync.Mutex
	from       string
	recipients []string
}

func (s *Session) Mail(from string, _ *smtp.MailOptions) error {
	normalised, _, _ := splitAddress(from)
	s.mu.Lock()
	defer s.mu.Unlock()
	s.from = normalised
	return nil
}

// Rcpt is the decision point of the entire system.
//
// This is the last moment a message can be refused. Once DATA is answered with 250 the
// sender hangs up, and any later "no" has nobody to reach. See docs/ARCHITECTURE.md
// diagram 8.
func (s *Session) Rcpt(to string, _ *smtp.RcptOptions) error {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	accepted, err := s.deps.Addresses.Accepts(ctx, to)
	if err != nil {
		// Redis is unreachable. Answer 451 (temporary), never 550.
		//
		// 550 means "this mailbox does not exist" — permanent. The sending server
		// gives up and generates a bounce, and that mail is gone for good. Our
		// infrastructure being briefly down must never destroy someone's email.
		// 451 tells the sender to try again in a few minutes, which it will.
		log.Printf("rcpt %s: address lookup failed: %v", to, err)
		return &smtp.SMTPError{
			Code:         451,
			EnhancedCode: smtp.EnhancedCode{4, 3, 0},
			Message:      "Temporary failure, please retry",
		}
	}
	if !accepted {
		return &smtp.SMTPError{
			Code:         550,
			EnhancedCode: smtp.EnhancedCode{5, 1, 1},
			Message:      "No such user here",
		}
	}

	normalised, _, _ := splitAddress(to)
	s.mu.Lock()
	defer s.mu.Unlock()
	// A sender may legally repeat the same RCPT TO. Recording it twice would make the
	// worker insert two mailbox_message rows and add 2 to blob.refcount for one real
	// delivery — a refcount that never returns to zero, so GC can never free the blob.
	if !slices.Contains(s.recipients, normalised) {
		s.recipients = append(s.recipients, normalised)
	}
	return nil
}

func (s *Session) Data(r io.Reader) error {
	// Snapshot the envelope once. go-smtp already refuses DATA and BDAT without a
	// MAIL FROM and at least one RCPT TO, so there is nothing to re-check here — but
	// on the BDAT path Reset() can fire on the connection goroutine while this one
	// runs, so the values are read under the lock and used as locals afterwards.
	//
	// from is deliberately allowed to be empty: "MAIL FROM:<>" is the null sender that
	// every bounce and delivery-status notification uses. Refusing it would mean we
	// never receive a single bounce.
	s.mu.Lock()
	from := s.from
	recipients := slices.Clone(s.recipients)
	s.mu.Unlock()

	// Generous: covers a 40 MB message crawling in over a slow link, plus the upload.
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
	defer cancel()

	receivedAt := time.Now().UTC()
	key, err := rawKey(receivedAt)
	if err != nil {
		log.Printf("data: cannot generate key: %v", err)
		return tempFailure()
	}

	// The reader goes straight to S3. We never call io.ReadAll — that is the whole
	// point. Memory stays flat whether this message is 2 KB or 40 MB.
	counted := &countingReader{r: r}
	if err := s.deps.Store.Put(ctx, key, counted); err != nil {
		// Over MaxMessageBytes is the one failure here that is genuinely permanent:
		// the message will be exactly as big on every retry. go-smtp signals it by
		// making the reader return ErrDataTooLarge, which the upload surfaces wrapped.
		// Answering 451 would make the sender requeue a message that can never fit,
		// retrying for days before giving up. 552 tells it now.
		if errors.Is(err, smtp.ErrDataTooLarge) {
			log.Printf("data: %s rejected, over %d bytes", key, s.deps.MaxBytes)
			return smtp.ErrDataTooLarge
		}
		log.Printf("data: upload %s failed: %v", key, err)
		return tempFailure()
	}

	// Store first, publish second, and the order matters.
	//
	// Publish-then-store would let the worker receive an event pointing at an object
	// that does not exist. Store-then-publish can only leave an unreferenced object
	// in S3 if the publish fails — cheap, sweepable, and nobody loses mail. When in
	// doubt, fail towards keeping the message.
	if err := s.deps.Events.Publish(ctx, Event{
		S3Key:      key,
		From:       from,
		Recipients: recipients,
		SizeBytes:  counted.n,
		ReceivedAt: receivedAt,
		RemoteAddr: s.remote,
	}); err != nil {
		log.Printf("data: publish %s failed: %v", key, err)
		return tempFailure()
	}

	log.Printf("accepted %s from=%s rcpt=%d size=%d", key, from, len(recipients), counted.n)
	return nil
}

// tempFailure tells the sender to retry later. Anything that is our fault gets a 4xx,
// so the message stays alive in the sender's queue instead of bouncing.
func tempFailure() error {
	return &smtp.SMTPError{
		Code:         451,
		EnhancedCode: smtp.EnhancedCode{4, 3, 0},
		Message:      "Temporary failure, please retry",
	}
}

// Reset is called when a sender starts a new message on the same connection.
// Forgetting to clear these would leak one message's recipients into the next.
func (s *Session) Reset() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.from = ""
	s.recipients = nil
}

func (s *Session) Logout() error { return nil }
