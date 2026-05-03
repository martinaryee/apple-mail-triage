# Mail-to-Todo Agent — High-Level Plan

## Context

Build a fully-local agent that watches Apple Mail for new incoming messages, uses
a local LLM (Ollama) to identify which ones imply an action item, and queues
those candidates as checkboxes in a single markdown file inside an Obsidian vault
for the user to review and promote. **No data ever leaves the machine.**

Reality-checked constraints from live probing on this Mac (macOS 26.3):

- Mail's UI "Categories" feature (Primary/Updates/Promotions/Transactions) is
  **not exposed via AppleScript** — `Mail.sdef` has no `category` property, and
  `properties of message` confirms it isn't on individual messages either.
  → We replace the "Primary" prefilter with a cheap header-based heuristic
  (List-Unsubscribe, no-reply senders, Mail's junk flag).
- AppleScript *does* give us everything we need per message: `subject`,
  `sender`, `content`, `all headers`, `date received`, `id` (numeric), `read
  status`, `junk mail status`, `reply to`, `mailbox`. Verified live against the
  user's inbox.
- Ollama is installed at `/opt/homebrew/bin/ollama` with `gemma4:26b` and
  `gemma4:31b` already pulled. Plan adds a smaller fast model (`gemma4:4b`) for
  classification.
- Two Obsidian vaults are open: Personal (`~/Dropbox (Personal)/Obsidian - Personal`)
  and Work (`~/Partners HealthCare Dropbox/Martin Aryee/Obsidian - Work`).
  Vault path is a config setting; default to Personal.

## Architecture

```
launchd (every 5 min)
        │
        ▼
  agent.py  ──► fetch_new_messages.applescript ──► Apple Mail
        │           (returns NDJSON of new msgs)
        ▼
   prefilter (headers + junk flag)
        │
        ▼
   for each survivor:
     classify via Ollama (gemma4:4b, format=json)
        │
        ▼
   if actionable → append `- [ ]` row to Mail Triage.md in Obsidian
        │
        ▼
   record message id in SQLite cursor (so we never re-process)
```

### Components

1. **launchd job** (`com.user.mailagent.plist`)
   - `StartInterval: 300` (5 min). Runs `agent.py`.
   - Logs stdout/stderr to `~/.mail-agent/logs/agent.{out,err}.log`.
   - `RunAtLoad: true`.

2. **Mail fetcher** — JXA (`osascript -l JavaScript`) embedded in `agent.py`
   - Query: messages in every account's Inbox where
     `dateReceived > <last_run_iso>` AND `id` not already in cursor table.
   - Emit one JSON object per message to stdout (NDJSON):
     `{id, account, subject, sender, replyTo, dateReceived, junk, read, headers, content}`.
   - Truncate `content` to ~4 KB (more than enough for an LLM signal; prevents
     edge cases with huge HTML bodies).

3. **Prefilter** (pure Python, no LLM)
   - Drop if: `junk == true`, header has `List-Unsubscribe:`,
     sender domain matches `noreply@|no-reply@|mailer-daemon@|notifications@`,
     `Auto-Submitted: auto-` is present, or `Precedence: bulk|list|junk`.
   - All other messages proceed to the classifier.

4. **Classifier** (`classify.py`)
   - POSTs to `http://localhost:11434/api/chat` with model `gemma4:4b`,
     `format: "json"` (Ollama-native structured output), `stream: false`.
   - System prompt: "You triage emails. Decide if this email implies an action
     I personally need to take (reply, decision, RSVP, deadline, errand,
     follow-up). FYI/newsletter/auto-confirmation = not actionable."
   - User content: subject, sender, snippet (first ~2 KB of plain-text content).
   - Schema:
     ```json
     {
       "actionable": true,
       "title": "Reply to Nicole about cooking-club trial on May 7",
       "reason": "asks me to confirm attendance",
       "urgency": "medium"
     }
     ```
   - Reject and skip on JSON parse failure (logged).

5. **Queue writer** — `<VAULT>/Inbox/Mail Triage.md` holds the user-visible
   actionable items only. Created on first write if missing; an absent or
   empty file is the normal steady state when the user has finished triaging.
   - Append one block per actionable message. Each block embeds the RFC822
     `Message-Id` in an HTML comment so the agent can de-dup against the file
     on startup as a safety belt against double-writes.
     ```
     - [ ] {title}  <!-- mid:{Message-Id} urgency:{u} -->
       - From: {sender} | {dateReceived}
       - Why: {reason}
       - [Open in Mail](message://%3C{Message-Id}%3E)
       - Account: {account}
     ```
   - The `message://` link, clicked from Obsidian, opens the original message
     in Apple Mail.

6. **State store** — SQLite at `~/.mail-agent/state.db`. Records the agent's
   memory of *every* message it has classified, actionable or not, so the LLM
   doesn't re-classify the same email on every poll.
   - `processed(message_id TEXT PK, account TEXT, date_received TEXT,
     processed_at TEXT, actionable INT)`.
   - High-water timestamp per account is computed at runtime as
     `max(date_received)` from this table, used as the lower bound for the
     next AppleScript fetch (combined with `processed_ids` dedupe to handle
     out-of-order arrivals).
   - Idempotent: running `agent.py` twice in a row with no new mail is a
     no-op.

   **Reset semantics (manual, explicit).** The queue file and the SQLite
   cache are independent stores. To redo the triage from scratch the user
   deletes **both** `Mail Triage.md` *and* `~/.mail-agent/state.db`. The
   agent does **not** auto-couple them, because an empty/missing queue file
   is the normal state when the user has finished triaging and would
   otherwise trigger a spurious full reprocess on the next poll. The README
   documents the reset as a deliberate two-step (a `--reset` flag on
   `agent.py` is a convenience that does the same two deletions).

### Configuration (`~/.mail-agent/config.toml`)

```toml
# Lower bound — agent never looks at messages received before this date.
# Used (a) on first run when no cache exists, and (b) any time the queue file
# is missing and the agent does a full reprocess.
start_date = "2026-05-03"

vault_path = "/Users/martin/Dropbox (Personal)/Obsidian - Personal"
queue_file = "Inbox/Mail Triage.md"
ollama_model = "gemma4:4b"
poll_interval_minutes = 5

# Backpressure cap. Each run classifies at most this many messages; any
# overflow stays unprocessed and is picked up by the next 5-min run. Protects
# against laptop-closed-for-days catch-up runs and mailing-list digest bursts
# that would otherwise stall the LLM for tens of minutes. Messages are
# processed oldest-first so the queue stays in chronological order.
max_messages_per_run = 50

content_truncate_bytes = 4096
log_level = "INFO"
```

## Files to create

| Path | Purpose |
| --- | --- |
| `~/projects/mail-agent/agent.py` | Orchestrator: fetch → prefilter → classify → write → record |
| `~/projects/mail-agent/jxa/fetch_new_messages.js` | JXA script invoked via osascript |
| `~/projects/mail-agent/classify.py` | Ollama JSON-mode call + parsing |
| `~/projects/mail-agent/queue.py` | Append-and-deduplicate writer for the markdown queue |
| `~/projects/mail-agent/state.py` | SQLite cursor and processed-id table |
| `~/projects/mail-agent/com.user.mailagent.plist` | launchd job (loaded with `launchctl bootstrap gui/$UID`) |
| `~/projects/mail-agent/install.sh` | Pulls `gemma4:4b`, creates dirs, loads launchd job, prompts for Mail TCC permission |
| `~/projects/mail-agent/uninstall.sh` | Unloads launchd, leaves data |
| `~/projects/mail-agent/README.md` | Setup, troubleshooting, privacy note |
| `~/projects/mail-agent/pyproject.toml` | uv/pip project; pure stdlib + `requests` and `tomli` if needed |

## Run statistics log (`~/.mail-agent/logs/runs.ndjson`)

Append one JSON object per run, NDJSON format. Designed to be loaded into
pandas/duckdb later for tuning `max_messages_per_run`, comparing models, and
spotting regressions. Empty/zero values are written explicitly so the schema
is uniform.

```json
{
  "schema_version": 1,
  "run_id": "2026-05-03T15:30:00Z-7f3a",
  "started_at": "2026-05-03T15:30:00.123Z",
  "ended_at":   "2026-05-03T15:30:18.901Z",
  "duration_ms": 18778,
  "model": "gemma4:4b",
  "dry_run": false,
  "cap_hit": false,
  "max_messages_per_run": 50,
  "counts": {
    "fetched_from_mail": 14,
    "deduped_already_seen": 3,
    "heuristic_dropped":   6,
    "llm_classified":      5,
    "actionable":          2,
    "errors":              0,
    "backlog_remaining":   0
  },
  "heuristic_dropouts": {
    "list_unsubscribe": 4,
    "no_reply_sender": 1,
    "junk_flag": 1,
    "auto_submitted": 0,
    "precedence_bulk": 0
  },
  "llm_ms": {
    "mean": 1820, "p50": 1700, "p95": 3100, "max": 3400
  },
  "tokens": {
    "prompt_total": 8420, "eval_total": 740
  },
  "classification": {
    "urgency": { "low": 0, "med": 1, "high": 1 }
  },
  "per_account": {
    "martin.aryee@gmail.com": { "fetched": 9, "classified": 4, "actionable": 2 },
    "DFCI":                   { "fetched": 5, "classified": 1, "actionable": 0 }
  },
  "errors": []
}
```

Key fields and why they matter:

- **`cap_hit` + `backlog_remaining`**: direct signal for whether
  `max_messages_per_run` is too tight. If `cap_hit` is repeatedly true, raise
  the cap or shorten poll interval.
- **`llm_ms.p95`** (not just mean): tail latency dominates the time budget
  on large HTML bodies. Use this — not the mean — when sizing the cap.
- **`tokens.prompt_total` / `eval_total`**: returned for free by Ollama;
  enables tokens/sec computation and apples-to-apples model comparison if
  the user later experiments with a different model.
- **`heuristic_dropouts`**: tells you which prefilters do real work; lets
  you prune dead ones or tune patterns.
- **`per_account`**: spot-check whether one account dominates load or
  produces all the false positives.
- **`errors`**: per-run array of `{stage, message_id?, error}` so healthy
  runs are `[]` and incidents are easy to grep.

Per-message timings are intentionally **not** in this file (would explode
size). A separate opt-in `--profile` flag writes detailed per-message
records to `runs-detailed.ndjson` when active.

Companion file `~/.mail-agent/logs/agent.log` is the human-readable rolling
log capturing launchd stdout/stderr, structured-log-style INFO/WARN/ERROR
lines. Stats live in NDJSON; narrative lives in the .log file.

## Permissions / one-time setup notes

- First AppleScript call to Mail will trigger a TCC prompt: "Terminal /
  mail-agent wants to control Mail." User must approve in System Settings →
  Privacy & Security → Automation. The install script should print the exact
  steps and verify with a probe call.
- Full Disk Access is **not** required because we use AppleScript, not direct
  reads of `~/Library/Mail`.
- Network egress: agent only ever talks to `localhost:11434`. README states
  this and recommends the user verify with Little Snitch / `lsof`.

## Implementation breakdown (Sonnet subagents)

Implementation runs as a series of `Agent` calls with `subagent_type:
"general-purpose"` and `model: "sonnet"`, each with a self-contained brief
and a tight I/O contract. The orchestrator (`agent.py`) is built last and
wires the pieces together; that step stays in the main session so the
top-level model owns integration.

### Wave 1 — six independent components, run in parallel

Each task gets its own Sonnet subagent. Briefs are tight: file path, exact
function signatures / CLI contract, sample input + expected output, and a
self-verification step the agent must run before returning.

| # | Subagent task | Output file | Contract |
|---|---|---|---|
| 1 | **JXA mail fetcher** | `jxa/fetch_new_messages.js` | CLI: `osascript -l JavaScript fetch_new_messages.js --since <ISO> [--max N]`. Emits NDJSON, one message per line: `{id, account, subject, sender, replyTo, dateReceived, junk, read, headers, content}`. Truncates `content` to N bytes. Verifies by running against the user's live Mail and confirming valid JSON. |
| 2 | **Heuristic prefilter** | `prefilter.py` | Pure function `filter_message(msg: dict) -> tuple[bool, Optional[str]]` returning `(keep, drop_reason)` where reason ∈ {`list_unsubscribe`, `no_reply_sender`, `junk_flag`, `auto_submitted`, `precedence_bulk`, None}. Includes pytest cases for each branch. |
| 3 | **Ollama classifier** | `classify.py` | Function `classify(msg: dict, model: str) -> dict`. POSTs to `localhost:11434/api/chat` with `format: "json"`, returns `{actionable, title, reason, urgency, llm_ms, prompt_tokens, eval_tokens}`. Handles JSON parse failure by returning `{actionable: False, error: "..."}`. Verifies by running against a known-actionable and known-not-actionable sample message. |
| 4 | **Queue writer** | `queue.py` | Functions: `existing_message_ids(path) -> set[str]` (reads `<!-- mid:... -->` markers); `append_block(path, entry: dict)` (creates file with header if missing). Pure file I/O. Pytest covers create-from-empty, dedup, and concurrent-append safety (file lock). |
| 5 | **State store** | `state.py` | SQLite wrapper. Functions: `mark_processed(message_id, account, date_received, actionable)`, `is_processed(message_id) -> bool`, `high_water(account) -> Optional[str]`. Schema migrations live here. Pytest covers schema creation, idempotent insert, and high-water across multiple accounts. |
| 6 | **Run-stats logger** | `stats.py` | Class `RunStats` with methods to record fetched/dropped/classified/errors and `.write(path)` that emits the NDJSON record described in the "Run statistics log" section. Records `llm_ms` percentiles correctly (uses `statistics.quantiles`). Pytest covers an empty run, a normal run, and a cap-hit run. |

These six are launched in a single message with six parallel `Agent` calls.
Each subagent's brief includes the relevant section of this plan file and
the contract row above; they do not need to read each other.

### Wave 2 — orchestrator (in main session, not a subagent)

`agent.py` is the integration point and stays in the foreground so the main
model owns the wiring. Sequence per run:

1. Load config → resolve model, vault path, cap, start_date.
2. Initialize `state.py`. For each account, compute `since = max(high_water,
   start_date)`.
3. Shell out to `jxa/fetch_new_messages.js --since <since> --max <cap>`,
   parse NDJSON.
4. For each message: skip if `state.is_processed`; else run prefilter; if
   kept, run `classify`; record outcome in `state` and (if actionable) in
   `queue`.
5. Aggregate counts and timings into `RunStats`; flush to `runs.ndjson`.
6. Honor `--dry-run` (no writes to state or queue) and `--reset` (deletes
   queue file and `state.db` after confirmation prompt).

### Wave 3 — packaging and docs (parallel subagents)

| # | Subagent task | Output |
|---|---|---|
| 7 | **launchd packaging** | `com.user.mailagent.plist`, `install.sh`, `uninstall.sh`. Install script also `ollama pull gemma4:4b` if missing, creates `~/.mail-agent/{logs,}`, prints the TCC-prompt instructions. |
| 8 | **README** | `README.md` covering setup, the manual two-step reset (delete `Mail Triage.md` AND `state.db`), TCC permissions, troubleshooting (Mail not responding, Ollama down), privacy guarantee (only network call is `localhost:11434`), and how to interpret `runs.ndjson`. |

### Why this carving

- **Each Wave 1 component has a single concrete artifact** (one Python module
  or one JXA script) and a contract that doesn't depend on other Wave 1
  components — so subagents don't fight over interfaces.
- **The integration step is in the main session**, not a subagent. Wiring
  is where surprises happen (signature drift, error-handling assumptions),
  and the top-level model already has the full plan in context.
- **No subagent needs to understand the LLM prompt** except #3 (classifier).
  No subagent needs to know about launchd except #7.

## Verification (end-to-end)

1. **Smoke test the fetcher**:
   `osascript -l JavaScript jxa/fetch_new_messages.js --since "1 hour ago"`
   → expect NDJSON of recent messages.
2. **Smoke test Ollama**:
   `curl -s localhost:11434/api/chat -d '{"model":"gemma4:4b","format":"json",...}'`
   → expect `{actionable: ...}` JSON.
3. **Dry run**:
   `python agent.py --dry-run --since "24 hours ago"` → prints what *would* be
   written to the queue without touching the SQLite cursor or Obsidian file.
4. **First real run**:
   `python agent.py` once manually → check
   `Obsidian - Personal/Inbox/Mail Triage.md` and click a `message://` link to
   confirm it opens the right message in Mail.
5. **Schedule test**: load the plist with
   `launchctl bootstrap gui/$UID com.user.mailagent.plist`, send yourself a
   test email asking a question, wait 5 min, confirm it appears in the queue
   file with reasonable title and urgency.
6. **Idempotence test**: run `agent.py` twice in a row with no new mail →
   second run does nothing, no duplicate queue entries.

## Design decision: no agent framework (LangChain etc.) for v1

Considered LangChain / LlamaIndex; rejecting for v1.

- **Pros of a framework**: standardized prompt templates, easy LLM swapping
  via abstractions, ready-made retry/parsing helpers, future extensibility to
  multi-step agents.
- **Cons that matter here**: this workload is *one* LLM call per email with
  Ollama's native `format: "json"` — none of the framework features
  (chains, RAG, ReAct tool loops, memory, callbacks) apply. LangChain in
  particular has had significant API churn between versions, adds a heavy
  transitive dep tree, and obscures simple `requests.post` calls behind
  abstractions that are harder to debug locally.
- **Decision**: direct `requests` to `localhost:11434` (~30 lines in
  `classify.py`). Revisit if v2 grows multi-step behavior — e.g. "look up the
  sender in Contacts, check Calendar for conflicts, draft a reply, ask the
  user to confirm." That's where chains and tool-using agents earn their
  keep, and switching at that point is a contained refactor.

## Out of scope for v1 (mention in README)

- Drafting reply text (just enqueue the action; replying is manual).
- Per-account routing (work mail → Work vault, personal → Personal vault).
  v1 puts everything in the configured vault. Easy follow-up.
- Two-way sync (ticking the box in Obsidian doesn't archive the mail).
- Snooze / due dates / recurrence.
- HTML-to-text fidelity (we use Mail's `content` property which is already
  plaintext-ish; complex HTML may give noisy snippets — acceptable for a
  classifier signal).
