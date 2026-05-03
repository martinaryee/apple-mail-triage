# Architecture

A tour of the code as it stands today. For why each design decision was
made, see [HANDOFF.md](HANDOFF.md). For the original specification, see
[PLAN-1.md](PLAN-1.md).

## One-paragraph summary

`agent.py` is the orchestrator. Every 5 minutes (via launchd) it asks Apple
Mail for the oldest unseen messages since a per-run watermark, drops obvious
noise with header heuristics, sends survivors to a local Ollama model, and
appends actionable items to a markdown file in an Obsidian vault. State
(every classified message-id) lives in SQLite as a cache; the queue file is
the user-visible source of actionable items. Per-run telemetry goes to
NDJSON for later analysis.

## File layout

```
mail-agent/
├── agent.py                       # Orchestrator (CLI: --dry-run, --reset, --since)
├── prefilter.py                   # Heuristic drops (headers, junk flag)
├── classify.py                    # Ollama HTTP client + JSON-output parser
├── triage_queue.py                # Append-and-dedupe markdown writer
├── state.py                       # SQLite cache of (message_id → outcome)
├── stats.py                       # RunStats accumulator → runs.ndjson
├── jxa/
│   └── fetch_new_messages.js      # JXA mail fetcher (osascript -l JavaScript)
├── prompts/
│   └── classify_system.md         # LLM system prompt — edit to retune
├── tests/                         # 63 pytest tests covering all components
├── config.toml.example            # Starter config
├── com.user.mailagent.plist       # launchd job (StartInterval 300s)
├── install.sh / uninstall.sh      # Setup scripts
├── README.md                      # User-facing docs
├── ARCHITECTURE.md                # (this file)
├── HANDOFF.md                     # War stories + open work for next agent
└── PLAN-1.md                      # Original design plan
```

User data lives outside the repo:

```
~/.mail-agent/
├── config.toml                    # User config (copied from config.toml.example)
├── state.db                       # SQLite cache of processed message-ids
└── logs/
    ├── agent.log                  # Human-readable rolling log
    ├── agent.out.log              # launchd stdout
    ├── agent.err.log              # launchd stderr
    └── runs.ndjson                # One JSON record per run (telemetry)
```

## Pipeline per run

```
load config (~/.mail-agent/config.toml)
   ↓
compute_since(state.db, start_date)        # MAX(date_received) across processed; falls back to start_date
   ↓
osascript jxa/fetch_new_messages.js        # 1 round trip per account (bulk dateReceived) + 1 per emitted msg (msg.properties())
   ↓
parse NDJSON                               # split on '\n' only — splitlines() breaks on U+2028/2029
   ↓
sort oldest-first; cap at max_messages_per_run (cap+1 to detect cap_hit)
   ↓
for each message:
   skip if state.is_processed(message-id)
   prefilter.filter_message(...)           # junk_flag → auto_submitted → precedence_bulk → list_unsubscribe → no_reply_sender
   if dropped: state.mark_processed(..., actionable=False); record stat; next
   classify.classify(...)                  # Ollama /api/chat with think=false, no format=json
   if actionable: triage_queue.append_block(queue_path, entry)
   state.mark_processed(message-id, account, date_received, actionable)
   ↓
stats.write(runs.ndjson)                   # one NDJSON record per run
```

## Key contracts

### `prefilter.filter_message(msg) -> (bool, str|None)`

Pure function. Returns `(True, None)` to keep, or `(False, drop_reason)`
where `drop_reason` is one of:

- `junk_flag` — Apple Mail flagged it
- `auto_submitted` — `Auto-Submitted: auto-*` header
- `precedence_bulk` — `Precedence: bulk|list|junk` header
- `list_unsubscribe` — any `List-Unsubscribe:` header
- `no_reply_sender` — sender local-part matches `noreply | no-reply | mailer-daemon | notifications | bounces` etc.

Order matters: rules are checked in this order and the first match wins, so
the returned reason is deterministic when multiple rules would fire.
Headers are parsed once per call into a lower-cased dict (first-occurrence
wins for repeated headers).

### `classify.classify(msg, model, ...) -> dict`

POSTs to `http://localhost:11434/api/chat`. Returns a dict with
`actionable, title, reason, urgency, llm_ms, prompt_tokens, eval_tokens, error`.
Never raises — all HTTP, parse, and validation failures return a default
`actionable: False, error: <str>` dict.

Critical Ollama options sent in the request body:

- `"think": False` — disables Gemma 3n's thinking-mode reasoning tokens
  (those go to `message.thinking`, not `message.content`, and waste 5–8s
  per call). See HANDOFF.md.
- `"keep_alive": "30m"` — keeps the model resident across the 5-minute
  agent cadence so we don't pay the ~4s reload penalty when other models
  evict ours.
- `"options.num_ctx": 4096` — caps the KV cache. Default is 131072 for
  Gemma 3n, which bloats memory for no benefit.
- `"options.num_predict": 200` — bounds output length.
- **No** `format: "json"` — that mode adds ~1-2s of opaque grammar overhead
  per call and we get equivalent reliability by asking for JSON in the
  prompt and parsing the first balanced `{...}` from the output via
  `_extract_json_object()`.

### `triage_queue.append_block(path, entry)` and `existing_message_ids(path)`

Append-only writer for `Mail Triage.md`. Each block embeds the RFC822
Message-Id in an HTML comment marker (`<!-- mid:<id> urgency:<u> -->`) so
the agent can dedupe against the file as a safety belt before writing.
Uses `fcntl.LOCK_EX` for cross-process safety.

If a message arrives without a Message-Id header, the agent skips it
entirely (logged as an error) — see HANDOFF.md "RFC missing Message-Id"
for why.

### `state.State` (SQLite cache)

```python
processed(message_id PK, account, date_received, processed_at, actionable)
```

`is_processed(mid)` is a single indexed lookup. `high_water(account)` is
`MAX(date_received) WHERE account=?`. The cache is **not** the source of
truth for what's actionable — the queue file is. The cache exists so we
don't re-LLM messages we've already seen.

The two stores reset independently and that is intentional — see README's
"Reset / starting over" section. An empty queue file with a populated cache
is the normal steady state when the user has worked through the queue.

### `stats.RunStats`

Accumulates per-run telemetry, then `write(path)` appends one NDJSON record
to `runs.ndjson`. Schema is in PLAN-1.md ("Run statistics log") and is
versioned (`schema_version: 1`). Latency percentiles use
`statistics.quantiles` when there are ≥4 samples; smaller batches fall
back to `max(samples)` for p50/p95/max.

### `jxa/fetch_new_messages.js`

Invoked via `osascript -l JavaScript fetch_new_messages.js --since <ISO> --max <N> --truncate-bytes <N>`.
Emits NDJSON to stdout, one JSON object per message:

```json
{ "id", "account", "mailbox", "subject", "sender", "replyTo",
  "messageId", "dateReceived", "junk", "read", "headers", "content" }
```

Performance pattern (the heart of why the agent is fast on a 132k-message
Gmail mailbox):

1. Per account: bulk-fetch *all* `dateReceived` via the collection
   specifier (`inbox.messages.dateReceived()`) — ~2s for 132k entries.
2. Linear scan in JS to find the boundary index where dates cross
   `sinceDate`.
3. For each message we'll emit: call `messages.at(idx).properties()` —
   one round trip returns all 22 properties in ~7s. Calling individual
   properties (`subject()`, `sender()`, `content()`, ...) one at a time
   is ~6s *per property* on this Apple Mail build, so `properties()` is
   the sole reason this isn't unusable.

### `agent.py` orchestration details

- `compute_since`: returns `MAX(date_received)` across all processed rows
  (not per-account MIN). The min-per-account approach is correct for
  per-account catch-up but stalls progress when one account is slow —
  see HANDOFF.md "Per-account watermark" for the full discussion.
- `fetch_messages`: shells out to `osascript`, parses stdout via
  `split("\n")` rather than `splitlines()` (the latter splits on U+2028
  / U+2029, which are valid in JSON strings but get emitted unescaped by
  some email content).
- Handles `--dry-run` (no writes to state, queue, or runs.ndjson) and
  `--reset` (deletes both `state.db` and the queue file with confirmation).

## Tests

```bash
cd /Users/martin/projects/mail-agent
uv run pytest tests/ -q
```

63 tests across 5 files. Live tests in `test_classify.py` skip themselves
if Ollama isn't reachable at `localhost:11434`. Tests use `tmp_path` for
isolation; no global state.

## Logging conventions

- `agent.log` is rolling INFO-level and human-readable. Use this when
  watching live behavior with `tail -f`.
- `runs.ndjson` is structured per-run telemetry. Use this for trend
  analysis (load into pandas / duckdb).
- launchd writes its own stdout/stderr to `agent.out.log` and
  `agent.err.log` — usually empty, but check them if launchd itself
  failed to start the job.
