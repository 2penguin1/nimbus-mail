# Nimbus — working context

Read this first, every session. It says what we are building, how we write it down,
and how the code should look.

## What this is

A multi-tenant business email platform built around a deduplicating storage engine.
It receives mail, stores each unique attachment once, and delivers it to many mailboxes
without copying bytes.

Five docs, five jobs. Keep them separate — do not let one grow into another.

| Doc | For | Contains |
|---|---|---|
| `docs/CONCEPTS.md` | Background knowledge | What a domain, MX record, `RCPT TO`, Redis, API key and JWT actually are. **Read first.** |
| `docs/OVERVIEW.md` | Understanding | Plain English, why each choice was made |
| `docs/ARCHITECTURE.md` | Seeing it | 20 diagrams — every component, flow, and decision |
| `docs/DATABASE.md` | The schema, explained | What each table is for, keys, indexes, delete traps |
| `docs/HLD.md` | Building it | Exact schema, endpoints, steps. Source of truth. |

Read `docs/HLD.md` before writing code. When a design changes, update **every one that
it touches** — the overview says why, the architecture redraws it, the HLD states it
exactly. Diagrams are ASCII inside fenced code blocks, kept under ~90 characters wide
so they survive the PDF render.

## How to think about this project

Work at a senior level. That does not mean writing more code or bigger abstractions —
it usually means writing less. It means these habits, every time:

1. **Trace the whole flow before touching anything.** Read the path end to end: who
   calls it, what state it depends on, what happens after. The two worst bugs found in
   the HLD so far — the impossible `550` bounce and the Kafka replay corrupting
   refcounts — were invisible in any single line. They only appeared by following the
   timing across components.
2. **Ask the three questions of every design.**
   - What happens the **second** time this runs?
   - What happens if it **crashes halfway**?
   - What happens at **100x** the volume?
   Most real bugs in a system like this are one of those three.
3. **Fix the root cause, not the symptom.** Before editing a shared function, find
   every caller. One guard in the shared place beats a guard in each caller, and
   patching only the reported path leaves the siblings broken.
4. **Check the design against itself.** When the schema and a flow disagree, one of
   them is wrong. Say so. That is how the missing `forwarding_rule` table and the
   never-incremented `chunk.refcount` were caught.
5. **Name the trade-off out loud.** Every real decision costs something. A design doc
   that lists only benefits is marketing. State what we gave up and when we would
   revisit it.
6. **Separate "this is wrong" from "I would do it differently."** Only the first is
   worth raising. Preference dressed as correctness wastes everyone's time.
7. **Never invent an API.** If unsure of a library's real signature, look it up with
   `context7` before writing against it. Confidently wrong code costs more than asking.
8. **Say what is unverified.** "I did not test this path" is a complete and acceptable
   sentence. Quiet assumptions are what break at 3am.

**The most senior move available is deleting something.** Question whether a piece
needs to exist before deciding how to build it.

## Where things are

```
docs/                design docs, each with a matching .pdf
tools/               md2pdf.py — renders any .md in docs/ to PDF
backend/
  .env               local settings, git-ignored. Copy from .env.example
  pyproject.toml     dependencies, package build, pytest config
  alembic.ini        migration config (URL comes from nimbus/config.py, not from here)
  migrations/        Alembic — versions/ holds the schema history
  src/nimbus/        THE PACKAGE. Installed, so every import is `nimbus.*`
    config.py        pydantic-settings, reads ../.env, no defaults      shared
    models.py        the schema as SQLAlchemy models, every table       shared
    db.py            async engine, session factory, Redis connection    shared
    storage.py       one boto3 client — worker writes chunks, API reads shared
    gc.py            block G. The three-phase sweep. `python -m nimbus.gc`
                     Root, not worker/: it is a script, not a consumer
    search_index.py  builds the tsvector + writes message_index.        shared
                     Root, not worker/: REGCONFIG must match on both sides
    api/
      main.py        app, lifespan, include_router. NO endpoints live here.
      security.py    Argon2 passwords, API keys, JWT, the auth dependencies
      addresses.py   the valid-address cache the Go receiver reads
      webhooks.py    outbound calls to a reseller, with the SSRF guard
      visibility.py  which mailboxes a reader may see. EVERY read filters on this
      ranges.py      HTTP byte-range maths. Pure, no I/O, heavily unit-tested
      cursor.py      keyset cursor encode/decode. Shared by /messages and /search
      search_query.py the query DSL. Pure, never raises, no I/O
      routers/       the HTTP surface, one module per resource
        health.py    GET /health
        auth.py      POST /v1/auth/login
        orders.py    POST /v1/orders, GET /v1/orders/{id}
        messages.py  list, read, patch, delete, streaming attachment download
        threads.py   GET /v1/threads/{id}
        search.py    GET /v1/search
        quota.py     GET /v1/quota — the ONE read endpoint that must not
                     filter on visibility.readable_mailboxes(). See its docstring
    worker/
      main.py        aiokafka consumer, manual offset commit
      pipeline.py    the one transaction: guard, scope, dedup, rows
      dedup.py       chunk + SHA-256. Pure, no I/O — the testable core
      mime.py        the only file touching the stdlib `email` API
      routing.py     block D: the chain, and the only place refcount goes up
  tests/
    unit/            no database, no network. Run them anywhere.
    integration/     marked `integration`, need the live stack
  scripts/           one-off operator tools (create_reseller.py,
                     apply_raw_retention.py)
  smtp-receiver/     Go SMTP receiver (block B)             DONE
                     No snooze/ worker: block H has none, deliberately
frontend/            React + TypeScript webmail UI          DONE (block I)
  src/api.ts         one request() — injects Bearer, maps 401 to logout
  src/hooks.ts       useAsync + usePaged. The ENTIRE data layer, ~50 lines
  src/types.ts       hand-written, because no endpoint declares a response_model
  src/components/BodyView.tsx  sender HTML in a sandboxed iframe. THE XSS boundary
  src/styles/tokens.css        3 layers, measured WCAG ratios in the comments
infra/               docker-compose, AWS deploy
```

**Go dependencies** (block B), each with its reason — see the "new dependency needs a
line" rule below:

| Package | Why |
|---|---|
| `emersion/go-smtp` | The SMTP protocol state machine. HLD §15 forbids hand-writing it. |
| `aws-sdk-go-v2` + `feature/s3/manager` | Streaming multipart upload. Its documented ceiling is `PartSize x (Concurrency+1)`, which is exactly the flat-memory guarantee §13 needs. |
| `redis/go-redis` | The `RCPT TO` address lookup |
| `twmb/franz-go` + `kadm` | Kafka producer and topic creation. Redpanda's own docs use franz-go. |
| `golang.org/x/net/netutil` | `LimitListener` — the connection cap on :2525. Replaces a hand-written accept loop with a semaphore. Go team's own repo, already an indirect dependency. |

**Python tooling is `uv`, not pip.** `uv run <cmd>` executes inside the project venv —
no activation step. `uv sync` installs from `uv.lock`, which pins exact versions so
this machine and AWS get byte-identical packages. Do not add `requirements.txt` back;
dependencies belong in `backend/pyproject.toml`.

**Database access is the SQLAlchemy 2.0 async ORM, everywhere** — including the storage
engine in blocks C, D and G. `models.py` is the single description of the schema, which
is also what makes `--autogenerate` work. Postgres-specific writes use the dialect and
stay in the ORM:
`pg_insert(Blob).values(...).on_conflict_do_nothing(index_elements=[...]).returning(...)`.
Raw `text()` is an **escape hatch, not a style** — allowed only where Postgres has no
clean Core form (tsvector/GIN search is the expected one), and every use carries a
comment saying why. There is currently **zero** raw SQL in `api/`.

Prefer `ON CONFLICT ... RETURNING` over catching `IntegrityError` for expected
collisions. An exception aborts the whole transaction and ties the code to how
SQLAlchemy happens to wrap asyncpg's error types.

Build order is `docs/HLD.md` §15. Built so far: blocks **A**, **B**, **C**, **D**, **E**,
**F**, **G** and **H**. In progress: **I** (React UI). Next: **J** (load test), **K** (deploy).

**Layout rules.** `src/nimbus/` is an installed package, so there is exactly one way to
import anything: `from nimbus.models import Blob`. No `sys.path` juggling, no relative
imports between services. `src/` layout specifically means a test can only import the
INSTALLED code, so a packaging mistake fails in the test run instead of in production.

- **Endpoints go in a router, never in `main.py`.** Each router owns its own prefix and
  OpenAPI tag, so adding an endpoint edits one file. `main.py` is assembly only: create
  the app, run the lifespan, `include_router`. HLD §10.2 lists 14 endpoints and blocks
  E–G add the rest — one file holding all of them is a file nobody reads.
- A module that serves no request is not a router. `webhooks.py` makes outbound calls,
  `addresses.py` maintains a cache — both sit beside the routers, not inside them.
- Shared by every service → `nimbus/` root (`config`, `models`, `db`)
- Owned by one service → its subpackage (`nimbus/api/`, `nimbus/worker/`)
- Tests never live beside source. `tests/unit/` runs anywhere; `tests/integration/`
  carries `pytestmark = pytest.mark.integration` and needs the stack.
- **The Go receiver keeps `_test.go` files next to its source.** That is Go's
  convention, not an oversight — "industry standard" is per language, not global.

**Running it**

```
docker compose -f infra/docker-compose.yml up -d     # Postgres, Redis, MinIO, Redpanda
cp backend/.env.example backend/.env                 # ONCE — nothing runs without it
cd backend && uv sync                                # installs nimbus + dev deps
uv run alembic upgrade head                          # apply the schema

uv run python scripts/create_reseller.py "Acme"      # prints an API key, once
uv run uvicorn nimbus.api.main:app --reload          # http://localhost:8000/docs
uv run python -m nimbus.worker.main                  # the dedup worker

uv run pytest                                        # everything
uv run pytest -m "not integration"                   # offline only, no stack needed
uv run pytest tests/unit -q                          # same thing, by path
uv run pytest -q -s tests/integration                # -s to see the dedup numbers
```

**Python dependencies added for block C**, each with its reason:

| Package | Why |
|---|---|
| `aiokafka[snappy]` | The worker's Kafka consumer. **The `[snappy]` extra is not optional** — franz-go compresses batches with snappy by default, so without the codec the consumer starts fine and dies on the first real message. |
| `boto3` | S3 and MinIO with one client (HLD §14). Blocking, so calls go through stdlib `asyncio.to_thread` rather than adding `aioboto3`. |

**Settings live in `backend/.env`, never in code.** `nimbus/config.py` is a
`pydantic-settings` model with **no working defaults** — a missing value or a
`JWT_SECRET` under 32 bytes raises on import instead of booting with a signing key that
is sitting in the repo. `.env.example` is the committed template and must never hold a
real secret; `.env` is git-ignored. `.env` stores a driver-less `postgresql://` URL and
`config.py` derives `+asyncpg` for the app and `+psycopg` for Alembic, so the two
cannot drift apart.

**Changing the schema** — never edit an applied migration, always add a new one:

```
cd backend
uv run alembic revision -m "add spam_score to message"   # creates an empty file
#   ... write upgrade() and downgrade() by hand with op.execute("...") ...
uv run alembic upgrade head        # apply
uv run alembic downgrade -1        # undo the last one
uv run alembic current             # which migration is this database at
uv run alembic upgrade head --sql  # print the SQL instead of running it
```

Two things to know:

- `--autogenerate` **works**, because `backend/src/nimbus/models.py` describes every table.
  It still cannot see two things: **triggers** and **extensions**. The refcount trigger
  on `mailbox_message` is the only place `blob.refcount` and `mailbox.used_bytes` go
  down, so never regenerate the initial migration from the models — it would drop the
  trigger silently and GC would stop freeing anything. Models follow the schema; they
  do not define it. Write trigger/extension changes by hand with `op.execute()`.
- **Drift check — run after touching `models.py`:**

  ```
  uv run alembic revision --autogenerate -m "drift check"   # must contain only `pass`
  rm backend/migrations/versions/*drift_check.py
  ```

  Anything other than `pass` means a model and the real schema disagree. A model that
  lies is worse than no model.
- Alembic runs on the **sync psycopg** driver; the app runs on **asyncpg**. asyncpg
  wraps statements in prepared statements, which Postgres refuses when the string
  holds more than one command — that would force one `op.execute()` per statement
  forever. Migrations are one-shot admin work, so sync is right there.

**Ports on this machine.** A native PostgreSQL owns 5432 and a native redis-server owns
6379, so compose maps ours one above: **Postgres on 5433, Redis on 6380**. Only the
host side moved — inside the Docker network containers still use `postgres:5432` and
`redis:6379`. MinIO (9000/9001) and Redpanda (19092/9644) had no conflict.

Schema changes go through Alembic (above) and apply to a live database. `down -v`
wipes every volume and is only for starting completely fresh — it deletes all data.

## Documentation rules

The docs are the deliverable, not an afterthought. Someone should be able to read
`docs/` and understand the whole system without reading code.

1. **Every design decision lands in `docs/` as Markdown.** Not in a chat reply, not in
   a code comment. If we decide something during a session, write it down that session.
2. **Every `.md` in `docs/` has a matching `.pdf`.** Regenerate after any edit:
   `python tools/md2pdf.py docs/HLD.md`
3. **Bump the version and date** at the top of a doc when it changes materially.
4. **Simple English.** Short sentences. Plain words. No jargon for its own sake.
   Real technical terms (`SMTP`, `tsvector`, `refcount`) stay — but any term a reader
   might not know goes in the glossary.
5. **Say why, not just what.** A design doc that lists decisions without reasons is
   useless six months later. Every non-obvious choice states the trade-off it made.
6. **Tables over paragraphs.** Comparisons, per-component breakdowns, metrics → table.
7. **ASCII diagrams inline**, so they survive in plain text and diff cleanly.

## Code rules

The point of this project is that a stranger can read it and follow it. Optimise for
that, not for cleverness.

1. **Simple and explainable.** If you cannot explain a piece of code in one sentence,
   it is too clever. Rewrite it.
2. **Every addition has a stated reason.** A new dependency, a new abstraction, a new
   layer — say in one line why it exists. No speculative "we might need this later"
   code. No interface with one implementation. No config for a value that never changes.
3. **Do not hand-write what a library does.** MIME parsing, the SMTP state machine,
   JWT signing, hashing. The value here is the storage engine, routing, and search.
4. **Comments explain why, not what.** The code already says what.
5. **Every non-trivial piece leaves one runnable check** — a small `test_*.py` or Go
   test that fails if the logic breaks. Not a full suite. The smallest thing that catches
   a regression.
6. **Deliberate shortcuts get a `# ponytail:` comment** naming the ceiling and the
   upgrade path, e.g. `# ponytail: fixed 4MB chunks, content-defined if dedup ratio disappoints`.
7. **Consistent naming with the HLD.** If the doc says `blob`, the code says `blob`.

## Skills and plugins — use them, do not improvise

All of these are already installed. Reaching for the right one is not optional; hand-rolling
what a skill already does is the same mistake as hand-rolling what a library already does.

**They compose.** `ponytail` decides *how small* the solution should be. `code-architect`
decides *what shape* it takes. `context7` supplies *the real API*. Using one does not
excuse skipping the others.

### Before designing

| When | Use |
|---|---|
| Designing any block before coding it | `feature-dev:code-architect` |
| Planning multi-step work | `claude-mem:make-plan`, then `claude-mem:do` |
| Writing a formal spec for one component | `create-specification` |
| Auditing architecture once code exists | `claude-mem:pathfinder` |
| Checking whether we solved this before | `claude-mem:mem-search` |

### Before writing a line against a library

| When | Use |
|---|---|
| **Any** FastAPI, boto3, Kafka client, Go SMTP, Redis, Postgres API | **`context7`** — fetch the current docs. Do this even when you think you know. Inventing a method signature is the single most common way this goes wrong. |

### While coding

| When | Use |
|---|---|
| Every coding task | `ponytail` (on by default — climb the ladder, stop at the first rung that holds) |
| Finding every caller before changing shared code | `serena` MCP — `find_symbol`, `find_referencing_symbols` |
| Understanding code structure fast | `claude-mem:smart-explore` (tree-sitter), `feature-dev:code-explorer` |
| React UI, components, styling | `ui-ux-pro-max:ui-styling`, `frontend-design` |
| The savings dashboard charts | `dataviz` |
| Running the app to see it work | `run` |

### After coding

| When | Use |
|---|---|
| Correctness review | `/code-review`, `feature-dev:code-reviewer` |
| Over-engineering review | `ponytail-review` — what can be deleted |
| Applying quality cleanups | `/simplify` |
| Listing every shortcut we deferred | `ponytail-debt` — harvests the `# ponytail:` comments |
| Browser tests | `playwright-tester` |
| Before the AWS deploy | `/security-review` |

### Docs and context

| When | Use |
|---|---|
| Writing or restructuring a doc | `documentation-writer` (Diátaxis) |
| Explaining a piece to Sujal in plain English | `claude-mem:what-the` |
| Keeping this file honest | `claude-md-management:revise-claude-md` |

## Answering style

Plain English, straight to the point. Lead with the answer. Bullets and tables over
prose. Say the number. If something is not verified, say so. No preamble, no filler,
no closing offers.

**Always explain what and why — this is not optional.** Sujal is building this to
understand it, not just to have it work. Before running a command or building a
component, say what it does, why this approach and not another, and what would break
without it.

- **Keep the jargon, define it inline.** Not "spins up a container" alone, but
  "starts a **container** — one program running in its own sealed box, with its own
  filesystem and network".
- **Assume no infrastructure background.** Docker, Compose, volumes, brokers, ports,
  networks all need explaining the first time they come up in a session.
- **Never answer with only a result.** "Done, 4 containers running" is a failure. Say
  what each one is for.
- A wrong command he understands is more useful than a right one he does not.

## Task review (after every task — do this without being asked)

This is being built fast, in about two days. So the check-in is **per task, not per day**,
and it stays short. If the review costs more than the task did, it is wrong.

After finishing a task, before starting the next, say in **under 10 lines**:

1. **What was built** — file by file, one line each, plain English.
2. **Why** — the reason each piece exists, not a description of it.
3. **What is missing or shortcut** — including every `# ponytail:` comment added.
4. **Anything that contradicts `docs/HLD.md`** — say it now, not later.
5. **One question, only if a real fork appeared.** Use `AskUserQuestion`. If there is an
   obvious default, take it, name it in one line, and keep moving. Do not stall on
   something that can be changed later.

Then update the Status section at the bottom of this file. That is the running record.

**Batch the docs.** Do not touch `docs/HLD.md` or regenerate the PDF after every task —
only when a design actually changed, and once per work session. The PDF is a deliverable,
not a build artifact.

If Sujal wants a deeper walkthrough of one piece, `claude-mem:what-the` gives the
who/what/where/why/when in plain English.

## Independent review after every block

Every build block (A–K) gets reviewed by a **separate agent** before it counts as done.
Not the agent that wrote the code — a fresh one, told to be skeptical of it. Self-review
misses what self-review always misses.

Spawn it with the `Agent` tool, `general-purpose`, and a prompt that:

0. **Makes it call `Skill(...)` as its FIRST action** — `ponytail:ponytail-review` for an
   over-engineering pass, `code-review` and `security-review` for a correctness pass.
   Naming a skill in the prompt does not load it: block F's first review listed `ponytail`
   and `context7` and did neither, and a second pass with the skills actually invoked
   found a 500 on `?q=%00`, a wrong expansion constant, and a swallowed retryable error.
   If two agents run at once, only ONE may edit — the other reports, or they collide.
1. **Makes it read `CLAUDE.md` and the relevant `docs/` sections first.** It has to know
   what the code was supposed to do before judging what it does.
2. Orders the priorities: **correctness bugs → anything that loses or wrongly accepts
   data → security → over-engineering (`ponytail`) → doc/code mismatches.**
3. Demands **verification before claiming** — check the real library behaviour with
   `context7` or by reading the source. A confident wrong finding costs more than a
   missed one.
4. Lets it **fix only clear-cut, low-risk bugs**, and requires `go build`, `go vet`,
   `go test` (or the Python equivalent) to pass after every edit. Anything needing a
   judgment call gets reported, not fixed.
5. **Forbids it editing `.md` files.** It reports doc mismatches; the main agent folds
   them into the block's doc update. Two writers on one file is a merge conflict
   waiting to happen.
6. Asks for output as a ranked list: `file:line`, what is wrong, the concrete failure
   scenario, the fix, and FIXED or REPORTED.

Then: read its findings critically — it can be wrong too — apply what survives, and
record anything material in `docs/`.

## When anything new is added to the project

Adding something is not finished until it is written down. Nothing new gets built
without the paperwork catching up in the same session.

**Whenever a new component, table, endpoint, dependency, worker, folder, or rule appears:**

| Update | With what |
|---|---|
| `docs/HLD.md` | The exact truth — new columns, new endpoints, new steps. Bump the version and date. |
| `docs/ARCHITECTURE.md` | Redraw the diagram it changes, or add a new one. Do not leave a stale diagram. |
| `docs/OVERVIEW.md` | Only if the *concept* changed. A new column does not belong here; a new subsystem does. |
| This file | New folder → "Where things are". New rule → the rules. Always → "Status". |
| PDFs | `python tools/md2pdf.py docs/<file>.md` for whatever changed. |

**A new dependency needs one extra line** stating why it exists and what it replaced
doing by hand. If that line is hard to write, we probably do not need the dependency.

**A stale diagram is worse than no diagram** — it is believed. If code and a diagram
disagree, fix the diagram in the same session, not later.

Design detail belongs in `docs/`, not here. This file stays short enough to be read.

## Status

- **Full scope, no features cut.** Target is ~2 days. `docs/HLD.md` §15 is now blocks
  A–K in dependency order, not days.
- `docs/HLD.md` at v0.4 — schema, flows, API surface, auth, build order all defined
- **Block B DONE and verified live.** Go SMTP receiver on :2525. Rejects unknown
  recipients (`550`), accepts known ones, streams to S3, creates its Kafka topic,
  publishes events. Measured: **212 MB of concurrent mail through 84 MB RSS**;
  sequential memory flattens at 40 MB regardless of message count. Reviewed by an
  independent agent — 5 real bugs found and fixed, see below.
- **Block A DONE and verified live.** Stack runs, schema applied (15 tables, 1 trigger,
  14 indexes), provisioning creates a domain + mailboxes in one transaction,
  idempotent retry replays instead of double-creating, login works, addresses land in
  Redis. The refcount trigger was proven on all three delete paths (3→2→1→0), and
  `ON DELETE RESTRICT` correctly refused to delete a reseller that still owned blobs.
- **API rewritten onto the SQLAlchemy 2.0 async ORM + `.env`.** Was raw asyncpg with
  hardcoded credentials. Now: `models.py` covers all 15 tables, **zero** raw SQL left in
  `api/`, settings validated on startup with no working defaults. Verified by
  `--autogenerate` producing an empty migration — proof the models and the live schema
  agree — plus all four check suites passing against the live stack.
- **Block C DONE and verified live.** The dedup engine. Consumes `mail.received`,
  splits MIME, chunks at 4 MB, SHA-256s, dedups per reseller, maintains
  `chunk.refcount`. Measured: two deliveries of one 10 MB attachment → 1 blob,
  3 chunks, 3 S3 objects, **50% saved**. Reviewed by an independent agent — 12 findings,
  6 fixed. New: `message_thread_lookup_idx` migration for the threading lookup.
- Next: **block D** — routing chain and fan-out delivery. It writes the
  `mailbox_message` rows and is the only thing that may move `blob.refcount`.

- **Block D DONE and verified live.** Routing chain and fan-out. `worker/routing.py`:
  alias → mailbox → catch-all → drop, returning a SET so two addresses reaching one
  mailbox deliver once. Only place `blob.refcount` and `mailbox.used_bytes` go up.
  Verified: 4 recipients → 3 mailboxes → refcount 3 → delete one row → 2.
  Reviewed by an independent agent — 9 findings, 5 fixed.
- **Block E DONE and verified live.** The read path: 6 endpoints, keyset pagination,
  shared-mailbox visibility, byte-range attachment streaming with `ETag`. Reviewed by
  TWO agents (an architect on the design, an SDE3 on the code) — 9 + 6 findings, 8 fixed.
  Migration `d7c28d117e84` adds `message.body_text` / `body_html`.
- **Block F DONE and verified live.** Search. `GET /v1/search` with a 4-operator DSL
  (`from:`, `has:attachment`, `before:`, `after:`) plus free text. Index rows written at
  delivery inside a savepoint, removed by a cascade. Migration `4f1a9c2b8e07`.
  **89 tests pass.** Measured: **5.3 ms** on a 100k-message mailbox (73–76 ms worst case,
  sort spilling to disk); the old `GIN (tsv)` index read another tenant's 50,000 rows and
  took 16.5 ms.
- **Block F was reviewed TWICE, and the second pass is why.** The first used agents whose
  prompt *named* `ponytail` and `context7`; the second made them call `Skill(...)` before
  reading any code. Naming a skill does not load it — the first pass was an ordinary
  review wearing the label, and the second found three things it had missed:
  1. **`GET /v1/search?q=%00abc` was a 500.** `str.split()` does not treat `\x00` as
     whitespace, so a NUL survived into `websearch_to_tsquery` and asyncpg raised — from
     the one function whose stated contract is that it never raises. The same byte, the
     same failure, that `mime._str` already fixed once on the write side. Now stripped in
     `parse()`, the single point every operator value and the free text pass through.
     `messages.py` had the same hole on `blob_hash`; a format check answers 404.
  2. **The tsvector expansion factor was wrong.** 2.1x is what plain words give; a URL
     emits THREE lexemes (`url`, `host`, `url_path`) and measures **3.64x**. The cap
     still holds at 128 KB/band, but the real headroom is 87% of the ceiling, not the
     "comfortably half" the comment claimed. This arithmetic has now been wrong twice —
     `test_the_two_bands_together_stay_under_the_tsvector_ceiling` exists to make the
     third time fail offline.
  3. **`_is_transient` swallowed failures carrying no SQLSTATE.** `InvalidCachedStatementError`
     fires right after a migration, has no sqlstate, and its connection is healthy — so
     the transaction committed and the message was permanently unsearchable, for
     something one retry fixes. Class `53` (disk full) was missing too.
- **Block G DONE and verified live.** Quota + garbage collection. `nimbus/gc.py` — a
  three-phase sweep with `--dry-run`, run as a script not a daemon. `GET /v1/quota`.
  Migration `9c3e5a1d7b42`. **91 tests pass.** Verified: a sweep leaves a shared
  attachment byte-identical for the mailbox still holding it, a doubly-referenced chunk
  reaches 0, no refcount goes negative, `used_bytes` reconciles to zero.
- **Block G re-reviewed by two skills-loaded agents (editor + reviewer).** No path was
  found that deletes referenced bytes — every destructive statement sits behind a
  `NO ACTION` foreign key, so a wrong refcount degrades to a leak or a loud abort, never
  to data loss. **Do not "tidy" `message_attachment → blob` or `blob_chunk → chunk`.**
  Four real defects fixed, all of them in the *reclaim* direction:

| Was | Now |
|---|---|
| `--dry-run` reported **`0 blobs, 0 chunks` in every real garbage state** — it rolled back after each phase, so phase 2 never saw the rows phase 1 would have released | The dry run performs the deletes and rolls the whole sweep back at the end. Locks are held for the sweep, which is why the real sweep still commits per batch |
| boto3 had **no timeouts at all** — ~600 s worst case *inside* phase 3's transaction, holding `FOR UPDATE` on up to 500 chunk rows, so one S3 hiccup stalls mail delivery for ten minutes | 5 s connect, 30 s read, 3 standard retries in `storage.py` |
| Grace-period cutoff compared Postgres's `now()` against the *app host's* clock | `func.now() - grace`, so one machine's clock decides |
| A failed S3 delete logged an ERROR and exited 0 | Still commits (see below), but exits non-zero |

**Two rules block G's review establishes:**

1. **A failed S3 delete must COMMIT, not roll back.** Rolling back looks tidier and is the
   corrupt choice: every chunk row survives at `refcount = 0` while the keys that *did*
   delete are already gone, so the next delivery of those bytes finds the row, skips the
   upload, and raises the refcount on a chunk pointing at nothing. Committing costs money;
   rolling back costs correctness. A review agent recommended the rollback — it was wrong.
2. **`--dry-run` is the only path an operator runs before trusting GC, so it needs a test.**
   It had none, which is how the always-zero bug survived the block's original review.
   `test_gc.py` now asserts the preview equals the real sweep.

**Tooling limitation worth knowing:** this project is **not a git repository**, so
`/code-review` and `/security-review` cannot scope a diff — `security-review` refuses
outright, and `code-review` silently falls back to scoping by file mtime, which sent it
to review the React UI when it was asked about GC. `git init` would unlock both.

**Four things block G found that were wrong before it existed:**

1. **The grace period measured the wrong clock.** `blob_gc_idx` was on `created_at` —
   when the bytes were STORED — so a blob written a year ago whose last reference vanished
   a second ago was collectable on the very next sweep, while a blob written an hour ago
   was held for 23 more hours for nothing. Added `blob.refcount_zeroed_at`, maintained by
   the trigger and cleared by `deliver()`. Invariant: NULL exactly when `refcount > 0`.
2. **The grace period was never what made the upload race safe — the FK is.** The
   key-share lock either blocks GC (which then re-checks `refcount = 0` and matches
   nothing) or blocks the writer (which fails loudly and Kafka retries). That holds at a
   grace period of zero. The 24 hours are an operator recovery window; `ARCHITECTURE.md`
   diagram 14 argued the opposite and its own worked example disproved it.
3. **`DELETE ... WHERE NOT EXISTS` does not re-check after unblocking.** EvalPlanQual
   re-checks against the ROW, and a row that was only key-share locked has no new version,
   so the subquery keeps its stale snapshot and the delete removes a message that just
   gained a copy. Phase 1 is `SELECT ... FOR UPDATE SKIP LOCKED` then a SEPARATE `DELETE`.
   `DATABASE.md` §9 trap 4.
4. **The raw `.eml` archive was 6.5x the store GC reclaims** — measured 2004 MB vs 310 MB
   — and nothing ever deleted it. Decision: a 7-day lifecycle rule
   (`scripts/apply_raw_retention.py`), applied and verified. HLD §11.2.

**Three rules block G establishes:**

1. **Chunk refcounts move by `count(*)`, never by 1.** One blob can list the same chunk
   twice (8 MB of zeros); block C counted it twice, so GC must too, or that chunk is
   stranded at 1 for ever with no error anywhere.
2. **The S3 delete goes INSIDE the transaction, before COMMIT.** Commit-then-delete leaves
   a window where the worker re-uploads the bytes and the delete lands after it. Content
   addressing does not save it — the re-upload is byte-identical.
3. **Nothing may add a `mailbox_message` row to an already-committed `message`.** Phase 1
   is safe partly because `deliver()` is the only writer and always runs in the message's
   own transaction. The database does not enforce this. "Restore from trash", "undelete"
   or "copy to another mailbox" all break it — re-read rule 3 above before adding one.
- **Block I DONE.** React 19 + TypeScript + Vite, 5 runtime dependencies, 6 screens,
  26 files. No Tailwind, no shadcn, no Radix, no state library, no chart library, no test
  framework, no HTML sanitiser — each rejected with a stated reason. Reviewed by a code
  agent (13 findings, 9 fixed) and a design agent (Rams audit, 3 changes applied).
  **Verified by attack, not by reasoning:** crafted hostile mail through the live
  receiver, and the browser blocked every vector — inline script, meta refresh, remote
  img, CSS @import, background-image, svg image, video poster, nested iframe, and the
  sender's attempt to inject its own permissive CSP. Zero bytes reached the attacker.
- **Two frontend rules worth keeping:** never `dangerouslySetInnerHTML` for `body_html`
  (the iframe sandbox is a browser-enforced boundary; a sanitiser is a denylist that has
  to be right every time), and never add `allow-scripts` or `allow-same-origin` to that
  iframe — together they let the frame remove its own sandbox.
- **Block H DONE and verified live.** Snooze — and it is none of what HLD §9.5 specified.
  No Redis sorted set, no Go worker, no poll loop, no lock, no leader election. Snoozed IS
  the predicate `snooze_until > now()`, evaluated at read time, so nothing fires and
  accuracy is exact instead of "within 1s". Migration `c81f4e6a29d3` **drops**
  `is_snoozed` — two columns for one state can disagree. `PATCH` gains `snooze_until`
  (timezone required: a naive one would be read in the server's zone and fire 5.5 hours
  early here, so it is a 422); the list gains `?snoozed=true`. **91 tests pass.**
  Verified: a message snoozed for 3 seconds came back by itself with nothing running.
- **Fixed alongside H:** `GET /v1/messages/{id}` used `.first()` with no `ORDER BY`, so a
  reader holding two copies got an arbitrary copy's `is_read`/`folder` — a coin flip. It
  now takes `?mailbox_id=` and 409s on ambiguity, like PATCH and DELETE. Found by the
  block I architect while reading the API, not by any backend review.

**Three things block F fixed that were latent in the schema from day one:**

1. `message_index`'s GIN index was on `tsv` alone, so the tenant filter could not use it.
   Now `GIN (mailbox_id, tsv)` — `mailbox_id` must be INSIDE the index or every search
   scans a posting list spanning every tenant. Needs the `btree_gin` extension.
2. Nothing deleted an index row when a copy was deleted. `DELETE /v1/messages/{id}`
   removes a `mailbox_message` row, and the FKs pointed at `message` and `mailbox` — so
   deleted mail would have stayed findable forever. Now a composite FK onto
   `mailbox_message` with `ON DELETE CASCADE`. `DATABASE.md` §9 trap 3 has the diagram.
3. `ARCHITECTURE.md` diagram 16's SQL had three bugs drawn into it: a join on
   `message_id` alone (a cartesian product for anyone reading two copies of one message),
   the mailbox filter on the wrong table, and a date filter on the sender-supplied
   `sent_at` instead of `received_at`. All corrected.

**Two rules block F establishes that G must not break:**

1. **`routing.deliver()` returns the SET of mailboxes it wrote, not a count.** That set is
   the shared replay guard: refcounts move by it, and the search index is written from it.
   A replay returns it empty and both stay still.
2. **A search-index failure must never cost the message.** The write sits in a SAVEPOINT.
   Transient failures (SQLSTATE `40`/`08`/`57` — deadlock, connection, timeout) are
   re-raised so the worker retries the event; everything else is logged at ERROR and the
   mail is kept unindexed. Verified: the first live run failed on a type mismatch and the
   log read `delivered ... to 2 mailbox(es), indexed 0`.

**Two things the next blocks must not get wrong:**

1. `blob.refcount` counts `mailbox_message` rows, so it moves by the number of rows
   **actually written** — never by the recipient count. Two addresses can resolve to one
   mailbox, and a replay finds rows already there.
2. **GC (block G) is a two-phase sweep and cannot be anything else.** Deleting a
   `refcount = 0` blob directly raises a foreign-key violation, because
   `message_attachment` still references it and nothing deletes `message` rows. Phase 1
   deletes orphan messages, phase 2 the blobs, phase 3 the chunks. HLD §9.7 has the
   proof. Getting this wrong means the dedup engine never reclaims a single byte.

**What blocks F and G inherit from E — three rules, all learned the hard way:**

1. **Filter on `visibility.readable_mailboxes()`, never on the JWT's mailbox id.**
   `ARCHITECTURE.md` diagram 16 said `WHERE mm.mailbox_id = $1` and would have made
   every shared mailbox's mail permanently unsearchable — invisible to members, and
   invisible to the mailbox itself, which has `password_hash = NULL` and cannot be
   logged into. Fixed in the diagram; do not reintroduce it.
2. **A message id does not identify a row.** The key is `(mailbox_id, message_id)`, and
   one email can reach one reader twice. Any endpoint that writes must name the copy.
   HLD §10.3.
3. **Quota is per-mailbox-row, not per-readable-set.** `used_bytes`/`quota_bytes` live on
   the specific mailbox a delivery landed on. G should report the caller's own row, not
   a sum over everything they can read — unless that becomes an explicit product
   decision written into §11.

**Two features are inert — the code handles them, nothing can create them:**
`shared_mailbox_member` (no member-management endpoint) and `forwarding_rule` (no
endpoint, and no outbound SMTP client). Both are described honestly in HLD §9.2. Do not
let a doc imply either works end to end.

**Known ceiling from block C:** worker memory peaks at **187 MB per 25 MB attachment**
(measured, `tracemalloc`), holding 25 MB during the database and S3 work. The peak is
the stdlib MIME parse, which has no streaming decode. About two workers per 2 GiB box.

**The five review findings are now settled** — four fixed, one deliberately deferred:

| # | Issue | Decision |
|---|---|---|
| 1 | No connection limit on :2525 | **Fixed.** `netutil.LimitListener`, 100 (`MAX_CONNECTIONS`). Worst case 100 x 14 MB = 1.4 GB — which does not fit §14's shared `t3.small`. Raised in HLD §9.1a as a deployment decision for block K, not papered over. |
| 2 | No inbound TLS / STARTTLS | **Still deferred.** The missing piece is a certificate for a hostname that does not exist yet, not code. Revisit with block K. HLD §14. |
| 3 | Cannot add mailboxes to a domain we already own | **Fixed.** Reuse via `ON CONFLICT DO NOTHING` + owner-filtered `SELECT`. Other tenant → vague `409`. Idempotency race → `409`, not `500`. HLD §9.6, diagram 19. |
| 4 | JWT outlives a deleted mailbox by 24h | **Fixed.** One PK lookup in `current_mailbox`. Trade-off taken: the token is no longer verifiable offline. HLD §10.1. |
| 5 | `_call_webhook` has no URL restriction | **Fixed.** Public HTTPS only, every resolved address checked with `is_global`. DNS-rebinding gap noted as `# ponytail:`. |

**All five verified.** `tests/integration/test_provisioning.py` — 7 checks against the live stack,
including that a failed order leaves no half-created mailbox. JWT revocation proven
directly: same token accepted, mailbox deleted, same token 401 while still being
cryptographically valid. #1 and #5 have offline checks (`go build`/`go test`,
`tests/unit/test_webhook_url.py`).

**Found while verifying, fixed:** `JWT_SECRET`'s dev default was 26 bytes — below the
32-byte HS256 floor (RFC 7518 §3.2), which PyJWT warns about on every call. A guessable
signing key forges a token for any mailbox in any tenant. `config.py` now refuses to
start below 32 bytes; HLD §14 lists the secrets a deploy must set.
- Critical path is A → B → C → D. Everything else hangs off D.
- **Outstanding on Sujal:** file the AWS port 25 unblock request. Takes AWS several days;
  nothing else can make it faster. Only needed if the deployed system must receive real mail.
- Open decisions still unsettled: `docs/HLD.md` §16
- **Loose end:** `docs/HLD.md` §7 lists a `mail.processed` Kafka topic that nothing
  publishes or consumes. Either give it a purpose or delete it.
