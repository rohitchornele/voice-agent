import json
import statistics
import sys
from pathlib import Path

path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("latency.jsonl")
rows = [
    json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()
]
cols = [
    ("stt_final_ms", "User stop → STT final"),
    ("turn_commit_ms", "STT final → turn committed"),
    ("llm_ttft_ms", "→ LLM first token"),
    ("tts_first_audio_ms", "→ TTS first audio"),
    ("publish_ms", "→ audio published"),
    ("total_ms", "Total (from VAD stop)"),
    ("fw_e2e_ms", "Framework e2e (from last voiced audio)"),
]


def pct(values, p):
    s = sorted(values)
    return s[min(len(s) - 1, round(p / 100 * (len(s) - 1)))]


print(f"{len(rows)} turns, all values in ms\n")
print("| Call | Turn | " + " | ".join(label for _, label in cols) + " |")
print("|---|---|" + "---|" * len(cols))
for r in rows:
    print(
        f"| {r['call_id']} | {r['turn']} | "
        + " | ".join(str(r.get(k, "")) for k, _ in cols)
        + " |"
    )

print("\n| Stat | " + " | ".join(label for _, label in cols) + " |")
print("|---|" + "---|" * len(cols))
for name, fn in [
    ("median", statistics.median),
    ("p95", lambda v: pct(v, 95)),
    ("max", max),
]:
    cells = []
    for k, _ in cols:
        vals = [r[k] for r in rows if k in r]
        cells.append(str(round(fn(vals))) if vals else "-")
    print(f"| {name} | " + " | ".join(cells) + " |")
