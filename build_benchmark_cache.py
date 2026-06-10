"""One-time: snapshot the 98-message benchmark set to benchmark_cache.json.

This is the ONLY step that touches the slow Mail fetch. It captures the four
fields classify() actually needs (sender, subject, dateReceived, content) plus
the manual ground-truth label, keyed by messageId. After this runs once,
bench_prompt.py can re-classify the set repeatedly with zero fetch cost.

Manual labels are sourced from rebenchmark_tuned_prompt.json (messageId ->
manual). Re-run this only if the benchmark message set itself changes.
"""

import json

import fetcher
from prefilter import filter_message

with open("rebenchmark_tuned_prompt.json") as f:
    labelled = json.load(f)

manual_by_id = {x["messageId"]: x["manual"] for x in labelled}
wanted = set(manual_by_id)
print(f"Looking for {len(wanted)} messages...")

cache = {}
fetched = 0
for msg, err in fetcher.stream_messages_since("2026-05-01T00:00:00", 4000, 4096):
    if err:
        continue
    fetched += 1
    mid = msg.get("messageId")
    if mid not in wanted or mid in cache:
        continue
    keep, _ = filter_message(msg)
    if not keep:
        continue
    cache[mid] = {
        "messageId": mid,
        "sender": msg.get("sender", ""),
        "subject": msg.get("subject", ""),
        "dateReceived": msg.get("dateReceived", ""),
        "content": msg.get("content", ""),
        "manual": manual_by_id[mid],
    }
    if len(cache) == len(wanted):
        break

print(f"Fetched {fetched} messages, cached {len(cache)} of {len(wanted)}")

with open("benchmark_cache.json", "w") as f:
    json.dump(list(cache.values()), f, indent=2)

print("Wrote benchmark_cache.json")
print("DONE")
