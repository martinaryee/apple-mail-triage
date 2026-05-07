# Architecture

A tour of the code as it stands today. For the user-facing setup guide, see
[README.md](README.md).

## One-paragraph summary

`agent.py` is the orchestrator. Every 5 minutes (via launchd) it reads the
oldest unseen messages since a per-run watermark directly from Apple Mail's
SQLite Envelope Index and `.emlx` files on disk, drops obvious noise with
header heuristics, sends survivors to a local Ollama model, and appends
actionable items to a markdown file in an Obsidian vault. State (every
classified message-id) lives in SQLite; the queue file is the user-visible
source of actionable items. Per-run telemetry goes to NDJSON.

## File layout

```
mail-agent/
├── agent.py                        # Orchestrator (CLI: --dry-run, --reset, --since)
├── agent_with_mail_app_status_check.py  # launchd entry point; polls lsappinfo before running
├── fetcher.py                      # Disk-based message reader (Envelope Index + .emlx)
├── prefilter.py                    # Heuristic drops (headers, junk flag)
├── classify.py                     # Ollama HTTP client + JSON-output parser
├── triage_queue.py                 # Append-and-dedupe markdown writer
├── state.py                        # SQLite cache of (message_id → outcome)
├── stats.py                        # RunStats accumulator → runs.ndjson
├── bench.py                        # A/B benchmark: disk fetch vs legacy JXA fetch
├── dump_candidates.py              # Dump prefilter survivors to ~/.mail-agent/candidates.json
├── run_classifier.py               # Replay candidates.json through the classifier
├── jxa/
│   ├── set_flags.js                # JXA: color-flag messages in Mail after classification
│   ├── list_accounts.js            # JXA: map account UUIDs to friendly names (one-shot)
│   └── fetch_new_messages.js       # Legacy JXA fetcher — kept for bench.py --run-jxa only
├── prompts/
│   └── classify_system.md          # LLM system prompt — edit to retune without code changes
├── tests/                          # 95 pytest tests covering all components
├── config.toml.example             # Starter config
├── com.user.mailagent.plist        # launchd job (StartInterval 300s)
└── install.sh / uninstall.sh       # Setup scripts
```

User data lives outside the repo:

```
~/.mail-agent/
├── config.toml                     # User config (copied from config.toml.example)
├── state.db                        # SQLite cache of processed message-ids
├── candidates.json                 # Last dump_candidates.py output (gitignored)
└── logs/
    ├── agent.log                   # Human-readable rolling log
    ├── agent.out.log               # launchd stdout
    ├── agent.err.log               # launchd stderr
    └── runs.ndjson                 # One JSON record per run (telemetry)
```

## Pipeline per run

```
launchd fires agent_with_mail_app_status_check.py
   ↓
lsappinfo front → check if Mail is frontmost (no AppleScript)
   if active: poll every poll_delay_seconds up to max_wait_minutes
   ↓
agent.py main()
   ↓
load config (~/.mail-agent/config.toml)
   ↓
compute_since(state.db, start_date)     # MAX(date_received) across processed; falls back to start_date
   ↓
fetcher.stream_messages_since(...)      # SQLite Envelope Index + .emlx disk reads; ~2s/100 msgs
   ↓
sort oldest-first; cap at max_messages_per_run+1 (the +1 detects backlog without fetching all)
   ↓
for each message:
   skip if state.is_processed(message_id)
   prefilter.filter_message(...)        # junk_flag → auto_submitted → precedence_bulk → list_unsubscribe → no_reply_sender
   if dropped: state.mark_processed(..., actionable=False); record stat; next
   classify.classify(...)               # Ollama /api/chat with think=false, no format=json
   if actionable: triage_queue.append_block(queue_path, entry)
   state.mark_processed(message_id, account, date_received, actionable)
   ↓
set_flags(assignments) via jxa/set_flags.js    # single JXA call; 1 s sleep between messages
   ↓
stats.write(runs.ndjson)               # one NDJSON record per run
```

## Key contracts

### `fetcher.py`

Reads messages directly from Apple Mail's on-disk storage — no AppleScript,
no Mail event-loop involvement. Two data sources:

**Envelope Index** (`~/Library/Mail/V*/MailData/Envelope Index`): Apple Mail's
SQLite database. Queried read-only via the `immutable` URI parameter. Contains
all metadata (timestamps, mailbox membership, sender, subject) indexed for fast
lookup. Timestamps are plain Unix epoch seconds (not Core Data timestamps).

**`.emlx` files**: Each message is stored as `<id>.emlx` or
`<id>.partial.emlx` at a path like:
```
V*/<UUID>/<mailbox>.mbox/<sub-UUID>/Data/<a>/<b>/<c>/Messages/<id>.emlx
```
Format: `<byte_count>\n<RFC 822 message><plist footer>`. The plist footer
contains flags (read, junk) as a bitmask.

Key helpers:
- `_iso_to_unix` / `_unix_to_iso` — Unix epoch conversion for Envelope Index SQL
- `_iso_to_core_data` / `_core_data_to_iso` — Core Data epoch conversion for .emlx plist footer
- `_emlx_path` — discovers the correct `.emlx` path across all known Apple Mail
  directory layouts (0–3 bucket levels, optional sub-UUID directory, nested
  mailbox paths like `[Gmail]/All Mail` → `[Gmail].mbox/All Mail.mbox/`)
- `_parse_mailbox_url` — URL-decodes and parses `ews://`, `imap://`,
  `mailbox://` URLs from the Envelope Index into `(uuid, mailbox_name)`
- `stream_messages_since` — main entry point; yields `(msg_dict, None)` or
  `(None, error_str)` for each result

Messages are fetched from all mailboxes except known noise folders (spam,
trash, deleted, drafts, outbox, sent). This avoids false negatives from
Gmail, where inbox membership is tracked in a separate `labels` table rather
than via mailbox URL.

### `prefilter.filter_message(msg) -> (bool, str|None)`

Pure function. Returns `(True, None)` to keep, or `(False, drop_reason)` where
`drop_reason` is one of:

- `junk_flag` — Apple Mail flagged it
- `auto_submitted` — `Auto-Submitted: auto-*` header
- `precedence_bulk` — `Precedence: bulk|list|junk` header
- `list_unsubscribe` — any `List-Unsubscribe:` header
- `no_reply_sender` — sender local-part matches no-reply/noreply/mailer-daemon/etc.

Rules are checked in this order; first match wins.

### `classify.classify(msg, model, ...) -> dict`

POSTs to `http://localhost:11434/api/chat`. Returns a dict with
`actionable, title, reason, urgency, llm_ms, prompt_tokens, eval_tokens, error`.
Never raises — all HTTP, parse, and validation failures return a default
`actionable: False, error: <str>` dict.

Critical Ollama options sent in the request body:

- `"think": False` — disables Gemma 3n's thinking-mode reasoning tokens, which
  go to `message.thinking` (not `message.content`) and waste 5–8 s per call.
- `"keep_alive": "30m"` — keeps the model resident across the 5-minute cadence.
- `"options.num_ctx": 4096` — caps KV cache (default 131072 for Gemma 3n bloats
  memory without benefit).
- `"options.num_predict": 200` — bounds output length.
- **No** `format: "json"` — that mode adds ~1–2 s of grammar overhead; we get
  equivalent reliability by parsing the first balanced `{...}` from the output
  via `_extract_json_object()`.

### `triage_queue.append_block(path, entry)` and `existing_message_ids(path)`

Append-only writer for `Mail Triage.md`. Only called when
`enable_triage_queue = true` in config (default). Each block embeds the RFC 822
Message-Id in an HTML comment (`<!-- mid:<id> urgency:<u> -->`), so the agent
can dedupe against the file before writing. Uses `fcntl.LOCK_EX` for
cross-process safety.

When the queue is disabled, `vault_path` is not required and `append_block` is
never called. Classification still runs (urgency is needed to pick the flag
color); actionable items are only surfaced via Apple Mail color flags.

### `state.State` (SQLite cache)

```
processed(message_id PK, account, date_received, processed_at, actionable)
```

`is_processed(mid)` is a single indexed lookup. `compute_since` returns
`MAX(date_received)` across all accounts — not per-account MIN, which would
stall progress when one account lags behind another.

The cache and the queue file reset independently. An empty queue file with a
populated cache is the normal steady state when the user is caught up. See
README "Reset / starting over".

### `stats.RunStats` → `runs.ndjson`

Accumulates per-run telemetry, then `write(path)` appends one NDJSON record.
`schema_version: 2`. Latency percentiles use `statistics.quantiles` when there
are ≥4 samples; smaller batches fall back to `max(samples)`.

### `jxa/set_flags.js`

The only JXA call on the hot path. Called once per batch (not per message) with
a JSON input file of `{account, mailbox, id, flagIndex}` assignments. Sleeps
1 s between each flag-set via `$.NSThread.sleepForTimeInterval(1.0)` to let
Mail's event loop breathe between operations.

Flag index values (empirically confirmed): 0=red, 1=orange, 2=yellow, 3=green,
4=blue, 5=purple, 6=grey, -1=no flag.

**Known limitation**: Gmail messages are stored in `[Gmail]/All Mail`. JXA's
`mailboxes.byName()` can't resolve nested mailbox names, so Gmail messages are
classified correctly but don't receive color flags.

### `jxa/list_accounts.js` (one-shot)

Called once per agent run (~0.5 s) to map account UUIDs to the friendly names
that `set_flags.js` needs for `mail.accounts.byName()`. Returns a JSON array
of `{id, name}`. Not on the per-message hot path.

### `jxa/fetch_new_messages.js` (legacy, bench only)

The original JXA-based message fetcher, replaced by `fetcher.py`. Kept so
`bench.py --run-jxa` can A/B compare the two approaches. Not used in
production runs. Performance context: ~138 s for 100 messages vs ~2.3 s for
the disk-based path.

## Tests

```bash
cd /Users/martin/projects/mail-agent
uv run pytest tests/ -q
```

95 tests across 6 files. Live tests in `test_classify.py` skip themselves if
Ollama isn't reachable at `localhost:11434`. Tests use `tmp_path` for
isolation; no global state.

## Logging conventions

- `agent.log` — rolling INFO-level, human-readable. Use `tail -f` while
  watching live behavior.
- `runs.ndjson` — structured per-run telemetry. Load into pandas or DuckDB for
  trend analysis.
- `agent.out.log` / `agent.err.log` — launchd stdout/stderr. Usually empty;
  check if launchd itself fails to start the job.
