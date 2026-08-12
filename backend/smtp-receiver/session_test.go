package main

import (
	"bytes"
	"io"
	"strings"
	"testing"
	"time"
)

// splitAddress is where the fiddly cases live: casing, whitespace, angle brackets,
// and the fact that the local part may legally contain '@' while the domain cannot.
// Getting it wrong means either rejecting real mail or accepting mail for nobody.
func TestSplitAddress(t *testing.T) {
	cases := []struct {
		in         string
		normalised string
		domain     string
		ok         bool
	}{
		{"sujal@acme.com", "sujal@acme.com", "acme.com", true},
		{"SUJAL@ACME.COM", "sujal@acme.com", "acme.com", true},
		{"  sujal@acme.com  ", "sujal@acme.com", "acme.com", true},
		{"<sujal@acme.com>", "sujal@acme.com", "acme.com", true},
		{"<SUJAL@Acme.Com>", "sujal@acme.com", "acme.com", true},

		// The last '@' separates. A quoted local part may contain one.
		{`"weird@name"@acme.com`, `"weird@name"@acme.com`, "acme.com", true},

		// A trailing root dot is legal in a fully-qualified name. Refusing it would
		// mean rejecting mail we should accept.
		{"sujal@acme.com.", "sujal@acme.com", "acme.com", true},
		{"<SUJAL@ACME.COM.>", "sujal@acme.com", "acme.com", true},

		// Malformed — must be refused, not guessed at.
		{"nodomain", "nodomain", "", false},
		{"@acme.com", "@acme.com", "", false},
		{"sujal@", "sujal@", "", false},
		{"", "", "", false},
	}

	for _, c := range cases {
		normalised, domain, ok := splitAddress(c.in)
		if normalised != c.normalised || domain != c.domain || ok != c.ok {
			t.Errorf("splitAddress(%q) = (%q, %q, %v), want (%q, %q, %v)",
				c.in, normalised, domain, ok, c.normalised, c.domain, c.ok)
		}
	}
}

// countingReader must report the true size while passing every byte through
// unchanged. If it altered the stream we would silently corrupt stored mail.
func TestCountingReader(t *testing.T) {
	payload := strings.Repeat("Subject: hello\r\n", 5000) // ~80 KB
	c := &countingReader{r: strings.NewReader(payload)}

	got, err := io.ReadAll(c)
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	if !bytes.Equal(got, []byte(payload)) {
		t.Fatal("countingReader changed the bytes passing through it")
	}
	if c.n != int64(len(payload)) {
		t.Errorf("counted %d bytes, want %d", c.n, len(payload))
	}
}

// Reading in small pieces, the way an upload does, must still total correctly.
func TestCountingReaderChunked(t *testing.T) {
	c := &countingReader{r: strings.NewReader(strings.Repeat("x", 1000))}
	buf := make([]byte, 7) // deliberately not a divisor of 1000
	for {
		if _, err := c.Read(buf); err != nil {
			break
		}
	}
	if c.n != 1000 {
		t.Errorf("counted %d, want 1000", c.n)
	}
}

func TestRawKeyIsUniqueAndDatePrefixed(t *testing.T) {
	at := time.Date(2026, 8, 9, 15, 4, 5, 0, time.UTC)

	first, err := rawKey(at)
	if err != nil {
		t.Fatalf("rawKey: %v", err)
	}
	if !strings.HasPrefix(first, "raw/2026/08/09/") {
		t.Errorf("key %q is missing the date prefix", first)
	}
	if !strings.HasSuffix(first, ".eml") {
		t.Errorf("key %q should end in .eml", first)
	}

	// Two messages in the same second must not collide, or one silently overwrites
	// the other in S3 and that mail is lost.
	seen := map[string]bool{first: true}
	for i := 0; i < 1000; i++ {
		k, err := rawKey(at)
		if err != nil {
			t.Fatalf("rawKey: %v", err)
		}
		if seen[k] {
			t.Fatalf("duplicate key generated: %s", k)
		}
		seen[k] = true
	}
}
