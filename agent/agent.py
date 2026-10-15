import asyncio
import json
import logging
import os
import time
import aiohttp
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    Agent,
    AgentSession,
    ConversationItemAddedEvent,
    JobContext,
    JobProcess,
    WorkerOptions,
    cli,
)
from livekit.plugins import openai, deepgram, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

load_dotenv()
log = logging.getLogger("voice-agent")

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
AGENT_NAME = "support-agent"  # must match backend/main.py
WHISPER_TOPIC = "operator-whisper"
LATENCY_FILE = Path(os.environ.get("LATENCY_FILE", Path(__file__).with_name("latency.jsonl")))

GUIDANCE_HEADER = (
    "\n\n--- PRIVATE GUIDANCE (highest priority, for you only) ---\n"
    "A colleague is silently coaching you during this call. Follow the guidance "
    "below in your next replies, naturally, as if it were your own idea. "
    "NEVER read it out, quote it, or say you were told or advised. "
    "NEVER mention an operator, supervisor, manager, colleague, coach or "
    "any guidance. Guidance so far, oldest first:\n"
)


def build_instructions(base: str, whispers: list[str]) -> str:
    if not whispers:
        return base
    return base + GUIDANCE_HEADER + "\n".join(f"- {w}" for w in whispers)


# ---------------------------------------------------------------- latency
class TurnTimer:
    """Wall-clock timestamps for one user turn -> agent reply.

    user_stop -> stt_final -> turn_committed -> llm_first_token
              -> tts_first_audio -> audio_published

    A turn begins when the user STARTS speaking, so stages that complete before the
    VAD declares end-of-speech (fast STT, preemptive LLM) are still recorded.
    """

    ORDER = [
        "user_stop",
        "stt_final",
        "turn_committed",
        "llm_first_token",
        "tts_first_audio",
        "audio_published",
    ]
    COLUMNS = {
        "stt_final": "stt_final_ms",
        "turn_committed": "turn_commit_ms",
        "llm_first_token": "llm_ttft_ms",
        "tts_first_audio": "tts_first_audio_ms",
        "audio_published": "publish_ms",
    }

    def __init__(self, call_id: int):
        self.call_id = call_id
        self.t: dict[str, float] = {}
        self.turn = 0
        self.pending: dict | None = None  # finished row, waiting for framework metrics

    def begin(self):
        self.t = {"speech_start": time.time()}

    def user_stopped(self):
        if "speech_start" in self.t:
            self.t["user_stop"] = time.time()

    def mark(self, key: str):
        if "speech_start" in self.t:
            self.t[key] = time.time()

    def forget(self, *keys: str):
        for k in keys:
            self.t.pop(k, None)

    def finish(self):
        t, self.t = self.t, {}
        if not all(k in t for k in self.ORDER):
            return  # e.g. the greeting, or a turn that was cancelled
        self.flush()  # a previous row that never received its metrics
        self.turn += 1
        row = {
            "call_id": self.call_id,
            "turn": self.turn,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        prev = t["user_stop"]
        for key in self.ORDER[1:]:
            cur = max(t[key], prev)  # a stage cannot end before the previous one
            row[self.COLUMNS[key]] = round((cur - prev) * 1000)
            prev = cur
        row["total_ms"] = round((prev - t["user_stop"]) * 1000)
        row["llm_started_before_turn_commit"] = (
            t["llm_first_token"] < t["turn_committed"]
        )
        self.pending = row

    def flush(self, metrics=None):
        """Write the pending row, adding the framework's own measurements if given."""
        row, self.pending = self.pending, None
        if row is None:
            return
        if isinstance(metrics, dict):
            for src, dst in (
                ("e2e_latency", "fw_e2e_ms"),
                ("llm_node_ttft", "fw_llm_ttft_ms"),
                ("tts_node_ttfb", "fw_tts_ttfb_ms"),
            ):
                if metrics.get(src) is not None:
                    row[dst] = round(metrics[src] * 1000)
        log.info("LATENCY %s", json.dumps(row))
        with LATENCY_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")


def _has_content(chunk) -> bool:
    if isinstance(chunk, str):
        return bool(chunk)
    return bool(getattr(getattr(chunk, "delta", None), "content", None))


class SupportAgent(Agent):
    def __init__(self, instructions: str, timer: TurnTimer):
        super().__init__(instructions=instructions)
        self.timer = timer

    async def llm_node(self, chat_ctx, tools, model_settings):
        # a new generation (possibly a re-run) starts: drop stale marks
        self.timer.forget("llm_first_token", "tts_first_audio")
        first = True
        async for chunk in Agent.default.llm_node(
            self, chat_ctx, tools, model_settings
        ):
            if first and _has_content(chunk):
                first = False
                self.timer.mark("llm_first_token")
            yield chunk

    async def tts_node(self, text, model_settings):
        first = True
        async for frame in Agent.default.tts_node(self, text, model_settings):
            if first:
                first = False
                self.timer.mark("tts_first_audio")
            yield frame


# ---------------------------------------------------------------- backend calls
async def fetch_agent_config(agent_id: int) -> dict:
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{BACKEND_URL}/agents/{agent_id}") as r:
            r.raise_for_status()
            return await r.json()


async def report_status(call_id: int, status: str) -> None:
    try:
        async with aiohttp.ClientSession() as http:
            async with http.patch(
                f"{BACKEND_URL}/calls/{call_id}/status", json={"status": status}
            ) as r:
                r.raise_for_status()
        log.info("call %s -> %s", call_id, status)
    except Exception:
        log.exception("could not report status %r for call %s", status, call_id)


async def post_transcript(call_id: int, lines: list[dict]) -> None:
    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(
                f"{BACKEND_URL}/calls/{call_id}/transcript", json={"lines": lines}
            ) as r:
                r.raise_for_status()
        log.info("call %s: transcript saved (%d lines)", call_id, len(lines))
    except Exception:
        log.exception("could not save transcript for call %s", call_id)


def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()


# ---------------------------------------------------------------- the call
async def entrypoint(ctx: JobContext):
    meta = json.loads(ctx.job.metadata or "{}")
    call_id = meta["call_id"]
    cfg = await fetch_agent_config(meta["agent_id"])
    log.info(
        "call %s: agent %r (model=%s, voice=%s)",
        call_id,
        cfg["name"],
        cfg["model"],
        cfg["voice"],
    )

    await ctx.connect()
    try:
        await ctx.wait_for_participant()  # don't greet an empty room
    except RuntimeError:
        await report_status(call_id, "missed")
        return
    t0 = time.time()  # "call start" for the transcript: the customer has joined
    await report_status(call_id, "active")

    session = AgentSession(
        stt=deepgram.STT(model="nova-3"),
        llm=openai.LLM(
            model=cfg["model"],
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key=os.environ["GEMINI_API_KEY"],
        ),
        tts=deepgram.TTS(model=cfg["voice"]),
        vad=ctx.proc.userdata["vad"],
        aec_warmup_duration=0.0,
        turn_handling={
            "turn_detection": MultilingualModel(),
            "interruption": {"mode": "vad", "min_duration": 0.3},
        },
    )

    timer = TurnTimer(call_id)
    base_instructions = cfg["system_prompt"]
    support_agent = SupportAgent(base_instructions, timer)

    # ---------- operator whispers ----------
    whispers: list[str] = []
    background: set[asyncio.Task] = set()

    async def apply_guidance():
        try:
            await support_agent.update_instructions(
                build_instructions(base_instructions, whispers)
            )
            log.info("WHISPER applied (%d active)", len(whispers))
        except Exception:
            log.exception("could not apply whisper")

    def on_data(packet: rtc.DataPacket):
        if packet.topic != WHISPER_TOPIC:
            return
        sender = packet.participant.identity if packet.participant else None
        if sender is not None and not sender.startswith("operator-"):
            log.warning("ignored whisper from non-operator %s", sender)
            return
        text = packet.data.decode("utf-8", errors="replace").strip()
        if not text:
            return
        whispers.append(text[:500])
        log.info("WHISPER from %s: %r", sender or "hidden participant", text)
        task = asyncio.create_task(apply_guidance())
        background.add(task)
        task.add_done_callback(background.discard)

    ctx.room.on("data_received", on_data)

    # ---------- transcript + latency events ----------
    transcript: list[dict] = []
    turn_start: dict[str, float | None] = {"user": None, "agent": None}
    turn_end: dict[str, float | None] = {"user": None, "agent": None}

    def _track(speaker: str, ev):
        now = time.time()
        if ev.new_state == "speaking":
            if turn_start[speaker] is None:
                turn_start[speaker] = now
            turn_end[speaker] = None  # talking again: forget any earlier pause
        elif ev.old_state == "speaking":
            turn_end[speaker] = now  # stopped: finished, paused, or cut off

    @session.on("user_state_changed")
    def _on_user_state(ev):
        log.info("STATE user %s -> %s", ev.old_state, ev.new_state)
        _track("user", ev)
        if ev.new_state == "speaking":
            timer.begin()
        elif ev.old_state == "speaking" and ev.new_state == "listening":
            timer.user_stopped()

    @session.on("agent_state_changed")
    def _on_agent_state(ev):
        log.info("STATE agent %s -> %s", ev.old_state, ev.new_state)
        _track("agent", ev)
        if ev.new_state == "speaking":
            timer.mark("audio_published")
            timer.finish()

    @session.on("user_input_transcribed")
    def _on_transcribed(ev):
        if ev.is_final:
            timer.mark("stt_final")

    @session.on("conversation_item_added")
    def _on_item(ev: ConversationItemAddedEvent):
        item = ev.item
        role = getattr(item, "role", None)
        text = getattr(item, "text_content", None)
        interrupted = bool(getattr(item, "interrupted", False))
        log.info("HISTORY role=%s interrupted=%s text=%r", role, interrupted, text)
        if role == "user":
            timer.mark("turn_committed")
        if role == "assistant":
            metrics = getattr(item, "metrics", None)
            log.info("FRAMEWORK_METRICS %s", metrics)
            timer.flush(metrics)

        speaker = {"assistant": "agent", "user": "user"}.get(role)
        if speaker is None or not text:
            return
        # end = when this speaker actually stopped talking (not when the line was committed)
        end = turn_end[speaker] or time.time()
        start = min(turn_start[speaker] or end, end)
        turn_start[speaker] = None
        turn_end[speaker] = None
        transcript.append(
            {
                "speaker": speaker,
                "text": text,
                "start_s": round(max(start - t0, 0.0), 2),
                "end_s": round(max(end - t0, 0.0), 2),
                "ts": datetime.fromtimestamp(start, timezone.utc).isoformat(),
                "interrupted": interrupted,
            }
        )

    # ---------- ending the call: save transcript FIRST, then mark ended ----------
    finish_holder: list[asyncio.Task] = []

    async def _finish():
        timer.flush()
        await post_transcript(call_id, transcript)
        await report_status(call_id, "ended")

    def finish_call() -> asyncio.Task:
        if not finish_holder:
            finish_holder.append(asyncio.create_task(_finish()))
        return finish_holder[0]

    @session.on("close")
    def _on_close(ev):
        finish_call()

    async def _on_shutdown(*_):
        await finish_call()

    ctx.add_shutdown_callback(_on_shutdown)

    await session.start(room=ctx.room, agent=support_agent)
    if cfg.get("greeting"):
        session.say(cfg["greeting"], allow_interruptions=False)


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint, prewarm_fnc=prewarm, agent_name=AGENT_NAME
        )
    )
