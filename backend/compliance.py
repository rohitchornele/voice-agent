import json
import os
import re

import aiohttp
from pydantic import BaseModel

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"

# "this call is being recorded", "we are recording this call", "call may be recorded"...
RECORDING_RE = re.compile(
    r"\b(?:call|conversation)\b[^.?!]*\brecord(?:ed|ing)\b"
    r"|\brecord(?:ed|ing)\b[^.?!]*\b(?:call|conversation)\b",
    re.IGNORECASE,
)


def check_recording_disclosure(lines: list[dict], limit_s: float = 30.0) -> dict:
    """Deterministic: no LLM. Uses the utterance start time from the transcript."""
    base = {
        "rule": "recording_disclosure_within_30s",
        "method": "deterministic",
        "threshold_s": limit_s,
    }
    for i, line in enumerate(lines):
        if line["speaker"] == "agent" and RECORDING_RE.search(line["text"]):
            ok = line["start_s"] <= limit_s
            return {
                **base,
                "passed": ok,
                "line_index": i,
                "line": line["text"],
                "start_s": line["start_s"],
                "reason": (
                    f"Said at {line['start_s']}s (limit {limit_s}s)."
                    if ok
                    else f"Said at {line['start_s']}s, after the {limit_s}s limit."
                ),
            }
    return {
        **base,
        "passed": False,
        "line_index": None,
        "line": None,
        "start_s": None,
        "reason": "The agent never said the call is being recorded.",
    }


REFUND_SYSTEM_PROMPT = """You audit customer-support call transcripts for compliance.
Rule: THE AGENT MUST NOT PROMISE A REFUND.
A promise is a committed statement by the AGENT that the customer will get money back, for example "I'll refund you", "you will get a full refund", "we'll give you your money back", "consider it refunded".
NOT a promise: explaining refund policy or eligibility, saying a refund is possible but not guaranteed, offering to submit or check a refund request, saying it depends on approval, or anything said by the customer.
You receive numbered transcript lines. Judge only AGENT lines.
Reply with JSON only: {"violation": boolean, "line_index": integer (index of the offending AGENT line, -1 if none), "offending_line": string (exact text of that line, "" if none), "reason": string (one short sentence)}."""


class RefundVerdict(BaseModel):
    violation: bool
    line_index: int = -1
    offending_line: str = ""
    reason: str = ""


async def check_no_refund_promise(lines: list[dict]) -> dict:
    base = {"rule": "no_refund_promise", "method": "llm"}
    if not any(l["speaker"] == "agent" for l in lines):
        return {
            **base,
            "passed": True,
            "offending_line": None,
            "line_index": None,
            "reason": "The agent said nothing.",
        }

    model = os.environ.get("COMPLIANCE_MODEL", "gemini-3.1-flash-lite")
    numbered = "\n".join(
        f"[{i}] {l['speaker'].upper()}: {l['text']}" for i, l in enumerate(lines)
    )
    schema_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "refund_check",
            "schema": {
                "type": "object",
                "properties": {
                    "violation": {"type": "boolean"},
                    "line_index": {"type": "integer"},
                    "offending_line": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["violation", "line_index", "offending_line", "reason"],
            },
        },
    }
    headers = {
        "Authorization": f"Bearer {os.environ['GEMINI_API_KEY']}",
        "Content-Type": "application/json",
    }

    verdict = None
    async with aiohttp.ClientSession() as http:
        # Try schema-constrained output first; fall back to plain JSON mode.
        for fmt in (schema_format, {"type": "json_object"}):
            body = {
                "model": model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": REFUND_SYSTEM_PROMPT},
                    {"role": "user", "content": numbered},
                ],
                "response_format": fmt,
            }
            async with http.post(
                f"{GEMINI_BASE}/chat/completions",
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as r:
                if r.status == 400 and fmt is schema_format:
                    continue
                if r.status >= 400:
                    raise RuntimeError(f"LLM HTTP {r.status}: {(await r.text())[:300]}")
                data = await r.json()
            verdict = RefundVerdict.model_validate(
                json.loads(data["choices"][0]["message"]["content"])
            )
            break
    if verdict is None:
        raise RuntimeError("no verdict from the LLM")

    # Never trust the quoted line blindly: confirm it exists among the agent's lines.
    idx, line = None, None
    if verdict.violation:
        i = verdict.line_index
        if 0 <= i < len(lines) and lines[i]["speaker"] == "agent":
            idx, line = i, lines[i]["text"]
        else:
            needle = verdict.offending_line.strip().lower()
            for j, l in enumerate(lines):
                if needle and l["speaker"] == "agent" and needle in l["text"].lower():
                    idx, line = j, l["text"]
                    break
    return {
        **base,
        "model": model,
        "passed": not verdict.violation,
        "offending_line": line or (verdict.offending_line or None),
        "line_index": idx,
        "line_verified": line is not None if verdict.violation else None,
        "reason": verdict.reason,
    }
