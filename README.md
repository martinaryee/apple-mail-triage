# Mail Agent

Triages new Apple Mail messages every 5 minutes using a local LLM. Actionable
emails are color-flagged in Mail and, optionally, queued as checkboxes in a
markdown file in your Obsidian vault. Everything runs on-device — no email
content, metadata, or message subjects ever leave your Mac.

For a tour of the code as it stands today, see [ARCHITECTURE.md](ARCHITECTURE.md).

---

## What it does

The agent wakes up every 5 minutes, checks whether Mail is the frontmost app
(and waits up to 30 minutes if so), then:

1. Reads new messages directly from Apple Mail's SQLite Envelope Index and
   `.emlx` files on disk — no AppleScript, no Mail event-loop involvement.
2. Drops obvious noise (newsletters, auto-replies, mailing lists, junk) with a
   fast header heuristic — no LLM needed.
3. Sends the survivors to a local Ollama model for classification.
4. Color-flags every processed message in Apple Mail (orange/red = actionable,
   grey = not actionable).
5. Optionally appends actionable emails as `- [ ]` checkboxes to `Mail
   Triage.md` in your Obsidian vault — set `enable_triage_queue = false` in
   config to skip this step and use flagging only.

Only one run is allowed at a time (lock file). Everything is recorded in
`state.db` and never re-classified.

---

## How it works

```
launchd (every 5 min)
        |
        v
  agent_with_mail_app_status_check.py
  (lsappinfo frontmost check — no AppleScript)
        |
        v
  agent.py
        |
        +---> fetcher.py ──► ~/Library/Mail/V*/MailData/Envelope Index (SQLite)
        |                    ~/Library/Mail/V*/<UUID>/**/*.emlx
        |                    (direct disk reads, ~2 s for 100 messages)
        |
        v
  prefilter.py   (headers + junk flag, no LLM)
        |
        v
  classify.py    (Ollama gemma4:e4b, think=false)
        |
   actionable?
      yes |                        no |
          v                           v
  triage_queue.py               state.py (mark seen)
  (append to                   (never re-classify)
   Mail Triage.md)
        |
        +---------------------------+
                    |
                    v
          set_flags.js (JXA) ──► Apple Mail
          (color-flag each message; 1 s between flags
           so Mail's event loop can breathe)

  stats.py  ──►  ~/.mail-agent/logs/runs.ndjson
```

After every run, each processed message is color-flagged in Apple Mail:

| Color | Flag index | Meaning |
|-------|-----------|---------|
| 🔴 Red | 0 | Actionable — high urgency |
| 🟠 Orange | 1 | Actionable — medium urgency |
| 🟡 Yellow | 2 | Actionable — low urgency |
| ⚫ Grey | 6 | Seen, no action needed |

Flag indices were determined empirically and differ from Apple's documentation.

---

## Requirements

- macOS 14 or later, with Apple Mail configured with at least one account
- Apple Silicon recommended — Ollama inference is significantly faster on M-series chips
- [Homebrew](https://brew.sh) installed, with `uv` and `ollama` available:
  ```bash
  brew install uv
  brew install ollama
  ```
- An Obsidian vault, or any directory you want a markdown file dropped into

---

## Install

```bash
cd /Users/martin/projects/mail-agent
./install.sh
```

The install script runs in two steps:

1. **First run**: creates `~/.mail-agent/` and its subdirectories, pulls
   `gemma4:e4b` via Ollama if it isn't already on disk (~10 GB), and writes a
   starter config to `~/.mail-agent/config.toml`.

2. **Edit the config**: open `~/.mail-agent/config.toml` and set at least
   `vault_path` and `start_date` (see [Configuration](#configuration) below).

3. **Load the launchd job**: run `./install.sh` a second time (or follow the
   prompt from the first run). This registers `com.user.mailagent` with
   launchd so the agent runs every 5 minutes automatically.

### Full Disk Access

`fetcher.py` reads `~/Library/Mail/` directly. The binary that launchd invokes
(`uv`) must have **Full Disk Access**:

System Settings → Privacy & Security → Full Disk Access → add the `uv` binary
(typically `/opt/homebrew/bin/uv`).

When running the agent manually from the terminal, FDA is inherited from the
terminal app (Terminal.app or iTerm2), so granting FDA to the terminal is
sufficient for development.

### TCC Automation permission

Flag-setting (`set_flags.js`) uses JXA to talk to Mail. The first time launchd
fires the agent, macOS may show:

> _"osascript" wants access to control "Mail"._

**Click Allow.** If you miss the dialog, or if the agent later logs
`NotAuthorizedError`, fix it manually:

System Settings → Privacy & Security → Automation → find **osascript** →
enable the toggle next to **Mail**.

You can trigger the prompt on demand by running the agent once in dry-run mode
(no flags are set, but the JXA call for account listing fires):

```bash
cd /Users/martin/projects/mail-agent
uv run python agent.py --dry-run
```

---

## Configuration

Config file: `~/.mail-agent/config.toml`. A fully-commented example is at
`config.toml.example`.

| Key | Default | What it does |
|-----|---------|--------------|
| `start_date` | `"2026-05-03"` | Hard floor — the agent never looks at mail older than this date. **Tune this first if you want to backfill older mail.** |
| `enable_triage_queue` | `true` | Write actionable items to a markdown queue file. Set to `false` for flagging-only mode — no Obsidian required. |
| `vault_path` | — | Absolute path to your Obsidian vault root (or any directory). **Required when `enable_triage_queue = true`.** |
| `queue_file` | `"Inbox/Mail Triage.md"` | Path *within* the vault for the review queue. Created on first write. Ignored when queue is disabled. |
| `ollama_model` | `"gemma4:e4b"` | Model used for classification. Must be pulled locally (`ollama pull <model>`). |
| `poll_interval_minutes` | `5` | Informational — reflects the `StartInterval` in the plist. Change in the plist, not just here. |
| `max_messages_per_run` | `50` | Cap on messages classified per run. Excess is carried to the next run. |
| `content_truncate_bytes` | `4096` | Message body trimmed to this length before sending to the LLM. |
| `log_level` | `"INFO"` | `DEBUG`, `INFO`, `WARN`, or `ERROR`. |
| `mail_app_status_check.poll_delay_seconds` | `30` | Seconds between frontmost-app checks when Mail is active. |
| `mail_app_status_check.max_wait_minutes` | `30` | Maximum minutes to wait before forcing a run anyway. |

---

## Prompt refinement

The classifier's system prompt lives at
[`prompts/classify_system.md`](prompts/classify_system.md). It is loaded
fresh on every run — edit and save, and the next run picks it up with no
code change.

### Iterating without re-fetching from Mail

To avoid paying the fetch cost on every prompt iteration, first dump a fixed
dataset of today's classifier-bound messages, then replay it as many times as
you like.

**Step 1 — Dump candidates** (run once per dataset):

```bash
uv run python dump_candidates.py
# or for a specific window:
uv run python dump_candidates.py --since "2026-05-04T00:00:00"
```

Fetches messages, applies prefilter, saves survivors to `~/.mail-agent/candidates.json`.

**Step 2 — Classify and review** (fast, no Mail access):

```bash
uv run python run_classifier.py
```

Output for each message:

```
[1/5] alice@example.com | Can you review the budget?
  ACTIONABLE [high] — Review budget proposal by Friday
  Why: Sender explicitly asks for a decision before the deadline
  1823ms  (245 prompt + 48 eval tokens)

[2/5] announcements@dfci.harvard.edu | Town Hall — May 15
  not actionable
  Why: Broadcast announcement, no personal action required
  1541ms  (198 prompt + 31 eval tokens)

────────────────────────────────────────────────
Actionable: 1/5
Total time: 8.4s  (1682ms avg)
```

**Step 3 — Edit and repeat:**

```
edit prompts/classify_system.md
uv run python run_classifier.py
```

---

## Daily use

### Flag colors in Apple Mail

Regardless of queue settings, every processed message is color-flagged:

| Color | Meaning |
|-------|---------|
| 🔴 Red | Actionable — high urgency |
| 🟠 Orange | Actionable — medium urgency |
| 🟡 Yellow | Actionable — low urgency |
| ⚫ Grey | Seen, no action needed |

### Where action items appear (queue enabled)

When `enable_triage_queue = true`, each actionable message is also appended to:

```
<vault_path>/Inbox/Mail Triage.md
```

Each entry looks like:

```markdown
- [ ] Reply to Nicole about cooking-club trial on May 7  <!-- mid:<id> urgency:medium -->
  - From: nicole@example.com | 2026-05-03 09:14
  - Why: asks me to confirm attendance
  - [Open in Mail](message://%3C...%3E)
  - Account: martin.aryee@gmail.com
```

- **Tick the checkbox** (`- [x]`) to mark done. The agent never modifies completed items.
- **Click "Open in Mail"** to jump directly to the original message.
- The file is yours — edit freely. The agent only appends; it never rewrites existing lines.
- An empty file is normal when you're caught up. It is not an error.

---

## Running manually / testing

```bash
cd /Users/martin/projects/mail-agent

# Dry-run with verbose output from a specific time (no writes)
uv run python agent.py --dry-run --verbose --since "2026-05-04T17:00:00"
```

**Flags:**

| Flag | Description |
|------|-------------|
| `--dry-run` | Classify messages but write nothing — no queue updates, no state.db entries, no runs.ndjson record. |
| `--verbose` / `-v` | Print one status line per message to stderr as it is processed. |
| `--since <ISO8601>` | Override the start of the fetch window. |

**Verbose output looks like:**

```
Fetching messages since 2026-05-04T17:00:00 …
[1] DROP list_unsubscribe        newsletter@example.com | Weekly digest
[2] DROP no_reply_sender         noreply@github.com | PR merged
[3] CLASSIFYING...               alice@example.com | Can you review this?
[3] ACTIONABLE [high]            alice@example.com | Can you review this?
[4] SKIP already-seen            bob@example.com | Re: meeting notes
Done: 4 message(s) processed
```

### Benchmark fetch vs classify

```bash
# Benchmark disk-based fetcher (default)
uv run python bench.py --since "2026-05-01T00:00:00" --max 100

# Also benchmark legacy JXA fetcher for comparison (~2 min)
uv run python bench.py --since "2026-05-01T00:00:00" --max 100 --run-jxa
```

---

## Reset / starting over

To redo triage from scratch, delete both the queue file (if using one) and
state:

```bash
rm "$VAULT_PATH/Inbox/Mail Triage.md"   # skip if enable_triage_queue = false
rm ~/.mail-agent/state.db
```

Or use the convenience flag (prompts for confirmation, skips queue file if
queue is disabled):

```bash
uv run python agent.py --reset
```

---

## Troubleshooting

### "It hasn't run"

```bash
# Check launchd job status
launchctl print gui/$UID/com.user.mailagent

# Watch the live log
tail -f ~/.mail-agent/logs/agent.log
```

### "Permission denied" reading mail / fetcher errors

The `uv` binary needs **Full Disk Access** for launchd runs. See [Full Disk
Access](#full-disk-access) above. For manual runs, the terminal app needs FDA.

### "Permission denied" setting flags / Automation errors

`set_flags.js` uses JXA to talk to Mail. Grant **osascript → Mail** in System
Settings → Privacy & Security → Automation.

### "Ollama errors" / classifier not responding

```bash
curl -s localhost:11434/api/tags   # should return model list JSON
ollama serve &                     # if it fails, start Ollama
```

### "Hitting the cap"

```bash
tail -50 ~/.mail-agent/logs/runs.ndjson | jq -r 'select(.cap_hit) | .run_id'
```

If `cap_hit` appears repeatedly, raise `max_messages_per_run` in config.

### "False positives in the queue"

Delete those lines from `Mail Triage.md`. The agent will not re-add them —
`state.db` has those message IDs recorded as seen.

---

## Reading runs.ndjson

Each run appends one JSON record to `~/.mail-agent/logs/runs.ndjson`. Example:

```json
{
  "schema_version": 2,
  "run_id": "2026-05-03T15:30:00Z-7f3a",
  "started_at": "2026-05-03T15:30:00.123Z",
  "ended_at": "2026-05-03T15:30:18.901Z",
  "duration_ms": 18778,
  "model": "gemma4:e4b",
  "cap_hit": false,
  "max_messages_per_run": 50,
  "counts": {
    "fetched_from_mail": 14,
    "deduped_already_seen": 3,
    "heuristic_dropped": 6,
    "llm_classified": 5,
    "actionable": 2,
    "errors": 0,
    "backlog_remaining": 0
  },
  "llm_ms": { "mean": 1820, "p50": 1700, "p95": 3100, "max": 3400 },
  "tokens": { "prompt_total": 8420, "eval_total": 740 },
  "errors": []
}
```

Useful one-liners:

```bash
# Latest run summary
tail -1 ~/.mail-agent/logs/runs.ndjson | jq

# Average LLM latency over last 100 runs
tail -100 ~/.mail-agent/logs/runs.ndjson | jq '.llm_ms.mean' | awk '{s+=$1;n++}END{print s/n"ms"}'

# Were we capped recently?
tail -50 ~/.mail-agent/logs/runs.ndjson | jq -r 'select(.cap_hit) | .run_id'

# How many messages were actionable today?
grep "$(date -u +%Y-%m-%d)" ~/.mail-agent/logs/runs.ndjson | jq '.counts.actionable' | awk '{s+=$1}END{print s}'
```

For heavier analysis:

```python
import pandas as pd
df = pd.read_json("~/.mail-agent/logs/runs.ndjson", lines=True)
```

---

## Privacy guarantee

- The only network call the agent ever makes is to `localhost:11434` (Ollama).
- You can verify this while a run is in flight:
  ```bash
  lsof -i -P | grep agent.py
  ```
- No telemetry. No cloud APIs. No data leaves your machine.

---

## Uninstall

```bash
cd /Users/martin/projects/mail-agent
./uninstall.sh
```

This unloads the launchd job and removes the plist. To fully wipe everything:

```bash
rm "/path/to/your/vault/Inbox/Mail Triage.md"
rm -rf ~/.mail-agent
```

---

## Out of scope (v1)

- Drafting reply text — the agent enqueues the action item; composing the reply is manual.
- Per-account routing — all accounts feed a single configured vault.
- Two-way sync — checking off an item in Obsidian does not archive or flag the original email.
- Snooze, due dates, or recurrence.
- Flag-setting for Gmail messages stored in `[Gmail]/All Mail` — JXA can't resolve nested mailbox names, so Gmail messages are classified correctly but don't receive color flags.
