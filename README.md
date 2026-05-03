# Mail-to-Todo Agent

Triages new Apple Mail messages every 5 minutes using a local LLM, and queues
actionable ones as checkboxes in a markdown file inside your Obsidian vault.
Everything runs on-device — no email content, metadata, or message subjects
ever leave your Mac.

For the original design rationale, see [PLAN-1.md](PLAN-1.md). For a tour of
the code as it stands today (post-implementation), see
[ARCHITECTURE.md](ARCHITECTURE.md). For the handoff notes, open work, and the
war stories from getting this performant on Apple Mail, see
[HANDOFF.md](HANDOFF.md).

---

## What it does

The agent wakes up every 5 minutes, fetches messages that arrived since the
last run, drops obvious noise (newsletters, auto-replies, mailing lists) with
a fast header heuristic, and sends the survivors to a local Ollama model for
classification. Emails that imply a personal action — a reply, a decision, an
RSVP, a deadline — are appended as `- [ ]` checkboxes to `Mail Triage.md` in
your Obsidian vault. Everything else is silently recorded as seen and never
shown again.

---

## How it works

```
launchd (every 5 min)
        |
        v
  agent.py
        |
        +---> osascript (JXA) ---> Apple Mail
        |       returns NDJSON of new messages
        |
        v
  prefilter.py   (headers + junk flag, no LLM)
        |
        v
  classify.py    (Ollama gemma4:e4b, think=false)
        |
   actionable?
      yes |                no |
          v                   v
  triage_queue.py         state.py (mark seen)
  (append to             (never re-classify)
   Mail Triage.md)
        |
        v
  stats.py  -->  ~/.mail-agent/logs/runs.ndjson
```

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

### TCC Automation permission

The first time launchd fires the agent, macOS shows a dialog:

> _"osascript" wants access to control "Mail"._

**Click Allow.** If you miss the dialog, or if the agent later logs
`NotAuthorizedError`, fix it manually:

System Settings → Privacy & Security → Automation → find **osascript** →
enable the toggle next to **Mail**.

You can also trigger the prompt on demand by running the agent manually once:

```bash
cd /Users/martin/projects/mail-agent
uv run agent.py --dry-run
```

---

## Configuration

Config file: `~/.mail-agent/config.toml`. A fully-commented example is at
`/Users/martin/projects/mail-agent/config.toml.example`.

| Key | Default | What it does |
|-----|---------|--------------|
| `start_date` | `"2026-05-03"` | Hard floor — the agent never looks at mail older than this date. Set it to roughly when you started using the agent. **Tune this first if you want to backfill older mail.** |
| `vault_path` | Personal Obsidian path | Absolute path to the root of your Obsidian vault (or any directory). **Must exist.** |
| `queue_file` | `"Inbox/Mail Triage.md"` | Path *within* the vault for the review queue. Created on first write. |
| `ollama_model` | `"gemma4:e4b"` | Model used for classification. Must be pulled locally (`ollama pull <model>`). The agent disables the model's "thinking" mode (see [HANDOFF.md](HANDOFF.md)) — if you swap in a model whose JSON output depends on a reasoning preamble, expect quality regressions. |
| `poll_interval_minutes` | `5` | Informational — reflects the `StartInterval` in the plist. Change in the plist, not just here. |
| `max_messages_per_run` | `50` | Cap on how many messages are classified in a single run. Excess is carried to the next run. **Raise this if you see frequent `cap_hit` in `runs.ndjson`; lower it if runs are slow.** |
| `content_truncate_bytes` | `4096` | Message body is trimmed to this length before sending to the LLM. 4 KB is enough signal; larger values slow inference. |
| `log_level` | `"INFO"` | `DEBUG`, `INFO`, `WARN`, or `ERROR`. |

---

## Tuning the prompt

The classifier's system prompt lives at
[`prompts/classify_system.md`](prompts/classify_system.md). Edit and save —
the next agent run picks it up; no code change needed. Keep it short; the
prompt file is appended to the system message verbatim.

If you want to A/B prompts later, dated copies in `prompts/archive/` and a
symlink swap is the simplest path.

---

## Daily use

### Where action items appear

```
<vault_path>/Inbox/Mail Triage.md
```

Each actionable message gets a block like:

```markdown
- [ ] Reply to Nicole about cooking-club trial on May 7  <!-- mid:<id> urgency:medium -->
  - From: nicole@example.com | 2026-05-03 09:14
  - Why: asks me to confirm attendance
  - [Open in Mail](message://%3C...%3E)
  - Account: martin.aryee@gmail.com
```

- **Tick the checkbox** (`- [x]`) to mark an item done. The agent never
  modifies completed items.
- **Click "Open in Mail"** to jump directly to the original message in Apple
  Mail.
- The file is yours — edit freely, reorder items, copy rows to another task
  list, delete false positives. The agent only appends; it never rewrites
  existing lines.
- An empty file is the normal state when you're caught up. It is not an error.

---

## Reset / starting over

The queue file and `state.db` are independent. Deleting only the queue file
does **not** cause the agent to reprocess old mail — that's intentional,
because an empty queue is the normal steady state.

To redo the triage from scratch, delete **both**:

```bash
rm "$VAULT_PATH/Inbox/Mail Triage.md"
rm ~/.mail-agent/state.db
```

Or use the convenience flag, which prompts for confirmation before deleting:

```bash
cd /Users/martin/projects/mail-agent
uv run agent.py --reset
```

After a reset, the agent starts fresh from `start_date` on its next run.

---

## Troubleshooting

### "It hasn't run"

Check the launchd job status:

```bash
launchctl print gui/$UID/com.user.mailagent
```

Watch the live log:

```bash
tail -f ~/.mail-agent/logs/agent.log
```

### "Permission denied" / Mail access errors

This is a TCC issue. Go to System Settings → Privacy & Security → Automation.
If osascript does not appear under Mail, run the agent once manually to trigger
the prompt:

```bash
cd /Users/martin/projects/mail-agent
uv run agent.py --dry-run
```

### "Ollama errors" / classifier not responding

Verify Ollama is up:

```bash
curl -s localhost:11434/api/tags
```

This should return JSON listing your local models. If it fails, start Ollama:

```bash
ollama serve &
# or open the Ollama app
```

### "Hitting the cap"

Check recent runs:

```bash
tail -50 ~/.mail-agent/logs/runs.ndjson | jq -r 'select(.cap_hit) | .run_id'
```

If `cap_hit` appears repeatedly, raise `max_messages_per_run` in
`~/.mail-agent/config.toml`, or shorten `poll_interval_minutes` in the plist
and reload it.

### "False positives in the queue"

Delete those lines from `Mail Triage.md`. The agent will not re-add them —
`state.db` already has those message IDs recorded as seen.

---

## Reading runs.ndjson

Each run appends one JSON record to `~/.mail-agent/logs/runs.ndjson`. Example:

```json
{
  "schema_version": 1,
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

For heavier analysis, load the file into pandas:

```python
import pandas as pd
df = pd.read_json("~/.mail-agent/logs/runs.ndjson", lines=True)
```

Or DuckDB:

```sql
SELECT * FROM read_ndjson_auto('~/.mail-agent/logs/runs.ndjson');
```

---

## Privacy guarantee

- The only network call the agent ever makes is to `localhost:11434` (Ollama).
- You can verify this while a run is in flight:
  ```bash
  lsof -i -P | grep agent.py
  ```
  The only entry should show `localhost:11434`.
- No telemetry. No cloud APIs. No data leaves your machine.
- The model weights are stored locally by Ollama, typically under
  `~/.ollama/models/` (~10 GB on disk for `gemma4:e4b`).

---

## Uninstall

```bash
cd /Users/martin/projects/mail-agent
./uninstall.sh
```

This unloads the launchd job and removes the plist. It does **not** delete your
user data. To fully wipe everything:

```bash
# Remove the queue file from your vault
rm "/Users/martin/Dropbox (Personal)/Obsidian - Personal/Inbox/Mail Triage.md"

# Remove agent state, logs, and config
rm -rf ~/.mail-agent
```

---

## Out of scope (v1)

- Drafting reply text — the agent enqueues the action item; composing the reply is manual.
- Per-account routing — all accounts feed a single configured vault. Work mail
  and personal mail go to the same queue file.
- Two-way sync — checking off an item in Obsidian does not archive or mark the
  original email as read in Mail.
- Snooze, due dates, or recurrence.
- HTML-to-text fidelity — Mail's `content` property is plaintext-ish; complex
  HTML emails may produce noisy snippets, which is acceptable for a
  classification signal.
