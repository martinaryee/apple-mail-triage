"""Fast prompt-tuning benchmark for the CONTENT_TAGGING classifier.

Loads the pre-fetched benchmark_cache.json (built once by
build_benchmark_cache.py) and re-classifies every message in-process with
classify.classify() — NO Mail fetch. This is the loop to use when iterating
on prompts/classify_system.md: edit the prompt, rerun this, read the score.

Usage:
    python bench_prompt.py                  # score the live prompt file
    python bench_prompt.py some_prompt.md   # score an alternate prompt file
    python bench_prompt.py --details        # also list every misclassification

Scoring is against the manual ground-truth label cached per message.
Permanent-error messages (e.g. UnsupportedLanguageOrLocaleError) score as
actionable=False, exactly as the production agent would record them.
"""

import sys
import time

import classify as classify_mod

CACHE_PATH = "benchmark_cache.json"


def load_cache():
    import json
    with open(CACHE_PATH) as f:
        return json.load(f)


def score(cache, prompt_text):
    # classify._respond reads the module-level SYSTEM_PROMPT, so swapping it
    # here changes the instructions for every classify() call below.
    classify_mod.SYSTEM_PROMPT = prompt_text

    results = []
    t0 = time.perf_counter()
    for i, m in enumerate(cache):
        r = classify_mod.classify(m)
        results.append({
            "subject": m["subject"],
            "manual": m["manual"],
            "pred": r["actionable"],
            "error": r["error"],
        })
        print(f"[{i+1}/{len(cache)}] manual={m['manual']!s:5} pred={r['actionable']!s:5} "
              f"{m['subject'][:55]!r}")
    elapsed = time.perf_counter() - t0

    tp = sum(1 for r in results if r["pred"] and r["manual"])
    tn = sum(1 for r in results if not r["pred"] and not r["manual"])
    fp = sum(1 for r in results if r["pred"] and not r["manual"])
    fn = sum(1 for r in results if not r["pred"] and r["manual"])
    n = len(results)
    sens = tp / (tp + fn) if (tp + fn) else float("nan")
    spec = tn / (tn + fp) if (tn + fp) else float("nan")
    acc = (tp + tn) / n if n else float("nan")

    print()
    print(f"n={n}  ({elapsed:.1f}s, {elapsed/n:.2f}s/msg)")
    print(f"TP={tp}  FN={fn}  TN={tn}  FP={fp}")
    print(f"sensitivity={sens:.1%}  specificity={spec:.1%}  accuracy={acc:.1%}")
    return results, {"tp": tp, "tn": tn, "fp": fp, "fn": fn,
                     "sens": sens, "spec": spec, "acc": acc}


def main():
    args = [a for a in sys.argv[1:] if a != "--details"]
    details = "--details" in sys.argv[1:]
    prompt_path = args[0] if args else classify_mod._PROMPT_PATH

    with open(prompt_path, encoding="utf-8") as f:
        prompt_text = f.read().strip()

    print(f"Prompt: {prompt_path}")
    print(f"({len(prompt_text)} chars)\n")

    cache = load_cache()
    results, _ = score(cache, prompt_text)

    if details:
        print("\n=== False negatives (missed actionable) ===")
        for r in results:
            if r["manual"] and not r["pred"]:
                err = f"  [{r['error']}]" if r["error"] else ""
                print(f"  {r['subject'][:60]!r}{err}")
        print("\n=== False positives (flagged noise) ===")
        for r in results:
            if r["pred"] and not r["manual"]:
                print(f"  {r['subject'][:60]!r}")


if __name__ == "__main__":
    main()
