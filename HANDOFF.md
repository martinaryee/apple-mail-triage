# Handoff Notes

For the next agent / engineer picking this up. Read this **before** changing
performance-sensitive code, before swapping the LLM, or before refactoring the
mail fetcher. Most of these were learned the hard way.

---

## Status as of handoff

- All 63 tests pass.
- End-to-end verified manually with the user's live Apple Mail (4 accounts,
  including a 132k-message Gmail INBOX).
- Real classifier latency: **~2s per email** in the steady state.
- Last test run: 7 batches of 10 messages each from a 30-day window produced
  10 actionable items in 16 minutes.
- The launchd job is **not yet loaded** on the user's machine — they wanted to
  hand-test before going live. Run `./install.sh` (twice — see README) to
  load it.

---

## Performance war stories — do not undo these without reading

These took hours to find. Each is currently load-bearing.

### 1. JXA: bulk `dateReceived()` + per-message `properties()`

**File**: `jxa/fetch_new_messages.js`.

The original approach used `inbox.messages.whose({dateReceived: {_greaterThanEquals: sinceDate}})`
which forces Apple Mail to enumerate the entire mailbox before returning.
On a 132k-message Gmail INBOX this routinely **timed out at 120 seconds**.

The current approach does two things:

1. **Bulk-fetch all dates per account** via `inbox.messages.dateReceived()`
   — collection-level fetch returns 132k Date objects in ~2s. We binary-find
   the boundary where dates cross `sinceDate` in JS.
2. **For each message we'll actually emit**, call `inbox.messages.at(idx).properties()`
   — fetches all 22 properties (subject, sender, content, headers, etc.) in
   ~7s per message. Calling individual properties one at a time is ~6s
   **per property**, so `properties()` is roughly an 8× speedup over the
   naive approach.

**Do not** rewrite this to use individual property accessors. Profile data:

| Pattern | Time |
|---|---|
| `messages.at(i).subject()` (each call) | 5–25 s per call |
| `messages.at(i).properties()` (single call returns all) | ~7 s per call |
| `messages.allHeaders()` (bulk over whole inbox) | times out at 120s |
| `messages.dateReceived()` (bulk over whole inbox) | ~2 s for 132k |

There is no documentation explaining why `properties()` is dramatically
faster than calling each property individually; presumably Apple's
AppleScript bridge serialises the whole message dictionary in one
round trip but does separate round trips for each property accessor.

### 2. `think: false` is essential on `gemma4:e4b`

**File**: `classify.py`.

The model has a "thinking" capability (visible in `ollama show gemma4:e4b`).
With thinking enabled (default), each call generates 100+ "reasoning"
tokens that go to `message.thinking`, **not** `message.content`. From the
caller's perspective:

- `eval_count` reports the full token count (reasoning + answer)
- `eval_duration` reports the full time
- But `message.content` is empty or contains only the (possibly truncated)
  answer after reasoning

Result: ~7s of latency we never see, and frequently a missing answer. With
`"think": False` in the request body, latency drops from ~10s to ~2s and
output is reliable.

**Do not** remove `think: false` without re-benchmarking. If you swap the
model to a non-reasoning architecture, the flag is a no-op. If you swap to
a reasoning model where the chain-of-thought meaningfully improves
classification quality, you'd want to re-enable thinking AND raise
`num_predict` AND parse the answer from the post-thinking content.

### 3. We do **not** use `format: "json"` or JSON Schema

**File**: `classify.py`.

Constrained-sampling modes in Ollama add **~1–2s of opaque overhead per
call** for Gemma. This time is *not* reflected in `prompt_eval_duration`
or `eval_duration` — it's in the gap between (load + prompt + eval) and
`total_duration`.

Free generation + manual JSON extraction (`_extract_json_object` in
classify.py: brace-balanced first-`{`-to-matching-`}` extraction) is just
as reliable in practice and meaningfully faster.

If you swap to a model where `format: "json"` is fast (Llama, Qwen, some
DeepSeek), you can re-enable it and drop the helper. Re-benchmark before
flipping back.

### 4. Don't keep multiple large models loaded in Ollama

**Operational, not in code.**

If a user has both `gemma4:e4b` (10 GB) and another big model loaded,
Ollama hot-swaps between them on every call, paying ~4s of `load_duration`
each time. Symptom: classify calls suddenly take ~12s instead of ~2s.

Mitigation in code: `keep_alive: "30m"` keeps the agent's model warm.
Mitigation operationally: `ollama ps` to see what's loaded; unload others
with `OLLAMA_KEEP_ALIVE=0` or `curl POST /api/chat ... keep_alive: 0`.

If this becomes a recurring issue, consider setting
`OLLAMA_MAX_LOADED_MODELS=1` in the launchd plist's `EnvironmentVariables`
to force Ollama to evict on load.

### 5. `num_ctx: 4096` instead of Gemma's default 131072

`num_ctx` allocates the KV cache. The 131K default sets aside ~GB of GPU
memory for context that we never use (our prompts are ~250 tokens, our
outputs ~50). Smaller `num_ctx` is faster prompt-eval and frees memory
for keeping the model resident.

**Watch out**: changing `num_ctx` *between* calls forces Ollama to reload
the model. The agent always sends the same `num_ctx`, so this isn't a
problem — but if you experiment with different values during debugging,
remember each change is a free model reload.

### 6. Python `splitlines()` vs `split("\n")` for NDJSON

**File**: `agent.py`, function `fetch_messages`.

`str.splitlines()` splits on every Unicode line separator including
U+2028 (LINE SEPARATOR) and U+2029 (PARAGRAPH SEPARATOR). Those characters
are *valid* inside JSON strings, and the JS engine in `osascript`'s JXA
mode does **not** escape them in `JSON.stringify` output. Many emails
(particularly marketing newsletters with rich HTML-to-text conversion)
contain these characters.

Use `split("\n")` and strip stray `"\r"` only. Do not switch back to
`splitlines()`.

---

## Known limitations and open work

### High value, easy

#### Per-account `since` watermark

`compute_since` in `agent.py` currently returns a single global
`MAX(date_received)`. This means: if one account is processed faster than
another, messages older than the leading account's high-water in the
slower account are **skipped**. We chose `MAX` over `MIN` because `MIN`
caused the agent to keep re-fetching already-processed messages from the
faster account on every batch (slow account drags `since` backward).

The proper fix is per-account `since`. Two reasonable shapes:

1. Pass per-account `since` as a JSON arg to JXA, e.g.
   `--since-by-account '{"gmail": "2026-04-26T...", "DFCI": "2026-04-20T..."}'`,
   and JXA picks the right boundary per inbox.
2. Or: agent.py invokes JXA once per account, collecting and merging the
   results. Simpler in JXA but more processes spawned.

Either is straightforward. The win is correct catch-up across accounts
that arrive at different rates.

#### Improve prefilter precision

The test sample showed a DFCI broadcast slipping through (item #9: "Register
for the Arab American Health and Wellbeing Research event"). It survived
prefilter because broadcast notifications from `Broadcast_Message@dfci...`
don't carry `List-Unsubscribe` headers.

Add a sender-pattern rule for broadcast addresses (`Broadcast_*@`,
`announcements@`, `news@`, etc.) or expand the no-reply pattern to include
common broadcast prefixes.

#### Tune the prompt

`prompts/classify_system.md` is the canonical system prompt. After live
testing, consider adding clauses for:

- "Broadcasts and optional events are NOT actionable unless personally
  addressed."
- "Cold outreach (recruiters, sales) is at most low urgency."
- "Mass-CCed messages where you are one of many recipients are not
  actionable unless explicitly tagged."

The prompt is loaded at module import via `_load_system_prompt()`. No
code change is needed to retune.

### Medium value

#### Per-account routing to different vaults

Currently every actionable item lands in one `Mail Triage.md` file
regardless of account. Work mail (`DFCI`, `HSPH`) and personal mail
(`gmail.com`) often want different homes. Easy lift: extend
`config.toml` with an account → vault mapping, fall back to default
`vault_path` if no match. Plumbing the mapping through agent.py and
`triage_queue.append_block` is mechanical.

#### Smaller / faster classifier

`gemma4:e4b` is an 8B Gemma 3n variant (the "E4B" name is Google's
"effective 4B" branding for their MatFormer architecture; the actual
weight count is 8B). At ~2s/call it's fine for the 5-min cadence but
overkill for binary actionable/not classification. Smaller candidates
worth A/B testing:

- `gemma3:4b` — proper 4B, no thinking mode, expect 0.5-1s/call
- `qwen2.5:3b` — strong on instruction following + JSON
- `llama3.2:3b` — small, fast, well-tested with Ollama JSON mode

Switching is a single line in `~/.mail-agent/config.toml` after
`ollama pull <name>`. Re-benchmark with the existing `tests/test_classify.py`
live tests.

#### Two-way sync

Ticking `- [x]` in Obsidian could mark the original message as read or
archived in Mail (via the `message://` link's id). Not started; no design
yet.

### Low value but interesting

#### Filesystem-based fetcher (skip AppleScript entirely)

For workloads heavier than 5-min triage — say, "summarise everything in my
inbox from last quarter" — Apple Mail's AppleScript layer is the wrong
tool. Two faster paths exist:

- Read `.emlx` files directly from `~/Library/Containers/com.apple.mail/Data/Library/Mail/V*/`.
  RFC 822 with an Apple-specific byte-count prefix and plist suffix.
- Read the `Envelope Index` SQLite database in the same directory (all
  metadata indexed, but schema is undocumented and shifts between
  macOS versions).

Both require **Full Disk Access**, which is more invasive than
Automation. Worth the effort only for batch/analytical workloads, not for
the 5-min steady state.

See README "Out of scope" and PLAN-1 "Design decision" for context. The
agent worked end-to-end on AppleScript via `properties()` — leave it
alone unless analytics needs change.

---

## Things that look wrong but aren't

- **No `git` history**: this is a personal project; commits are local.
- **No formal package layout** (`mail_agent/__init__.py`): we deliberately
  kept everything flat at project root. The one collision was `queue.py`
  (shadowed stdlib `queue` and broke `requests`); we renamed to
  `triage_queue.py`. No other module names collide.
- **`launchctl bootstrap` syntax** in install.sh uses
  `gui/$UID/...` which is correct for per-user LaunchAgents on
  modern macOS; the older `launchctl load` syntax is deprecated.
- **The plist's `ThrottleInterval: 60`** prevents thrashing if the agent
  crashes immediately on launch. It's intentional, not a typo of
  `StartInterval`.
- **`config.toml.example` ships with the user's actual personal Obsidian
  vault path** as the default. This is a personal repo, so it's fine.
  Sanitize before any wider distribution.

---

## Setup gotchas

- **First launchd run will trigger a TCC Automation prompt** ("osascript
  wants to control Mail"). If the user is away from the screen when it
  fires, they have to grant it manually in System Settings → Privacy &
  Security → Automation.
- **Ollama must be running** when the agent fires. The default Homebrew
  install starts a launchd-managed Ollama service; if the user uses the
  GUI Ollama app instead, it must be open. Symptom of Ollama-down:
  `requests.ConnectionError` in agent.log.
- **The user's Apple Mail is configured with 4 accounts** including a
  Gmail account whose "INBOX" is actually `[Gmail]/All Mail` (132k
  messages). Performance on this mailbox was the binding constraint for
  the JXA refactor.

---

## How to verify after a change

```bash
cd /Users/martin/projects/mail-agent

# Unit tests
uv run pytest tests/ -q             # 63 tests, all live except Ollama-skipped

# Smoke test the JXA fetcher
osascript -l JavaScript jxa/fetch_new_messages.js \
  --since "$(date -u -v-1H +%Y-%m-%dT%H:%M:%SZ)" \
  --max 5 --truncate-bytes 200 \
  | python3 -c 'import sys,json; [json.loads(l) for l in sys.stdin]'

# Smoke test the classifier (Ollama must be running)
uv run python -c '
from classify import classify
print(classify({
  "subject": "Can you confirm by Friday?",
  "sender": "alice@example.com",
  "dateReceived": "2026-05-03T10:00:00Z",
  "content": "Can you confirm by Friday?"
}))'

# Dry-run agent end-to-end (no writes)
uv run python agent.py --dry-run --since "$(date -u -v-2H +%Y-%m-%dT%H:%M:%SZ)"

# Read the latest run record
tail -1 ~/.mail-agent/logs/runs.ndjson | python3 -m json.tool
```

If any of these regress, look at HANDOFF.md "war stories" before refactoring
deeper.
