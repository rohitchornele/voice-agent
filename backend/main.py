import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException
from sqlalchemy import JSON, Column

import compliance

from fastapi.middleware.cors import CORSMiddleware

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from livekit import api
from sqlmodel import Field, Session, SQLModel, create_engine, select

load_dotenv()
LIVEKIT_URL = os.environ["LIVEKIT_URL"]  # ws:// URL, what browsers connect to
LIVEKIT_API_KEY = os.environ["LIVEKIT_API_KEY"]
LIVEKIT_API_SECRET = os.environ["LIVEKIT_API_SECRET"]
LIVEKIT_HTTP_URL = os.environ.get("LIVEKIT_INTERNAL_URL", LIVEKIT_URL).replace("wss://", "https://").replace("ws://", "http://")
AGENT_NAME = "support-agent"  # must match the worker's agent_name

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "voice.db"))
engine = create_engine("sqlite:///" + DB_PATH, connect_args={"check_same_thread": False})


# ---------- models ----------
class AgentCreate(SQLModel):
    name: str
    system_prompt: str
    voice: str = "aura-2-thalia-en"
    model: str = "gemini-3.1-flash-lite"
    greeting: Optional[str] = None


class AgentConfig(AgentCreate, table=True):
    __tablename__ = "agents"
    id: Optional[int] = Field(default=None, primary_key=True)


class Call(SQLModel, table=True):
    __tablename__ = "calls"
    id: Optional[int] = Field(default=None, primary_key=True)
    agent_id: int = Field(foreign_key="agents.id")
    room_name: str
    status: str = "created"  # created | active | ended | missed
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    transcript: Optional[list] = Field(default=None, sa_column=Column(JSON))
    compliance: Optional[dict] = Field(default=None, sa_column=Column(JSON))


class CallStatusUpdate(SQLModel):
    status: str


class CallCreate(SQLModel):
    agent_id: int


class CallStarted(SQLModel):
    call_id: int
    room_name: str
    livekit_url: str
    token: str
    identity: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    SQLModel.metadata.create_all(engine)
    yield

def ensure_columns():
    with engine.connect() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(calls)")}
        for name in ("transcript", "compliance"):
            if name not in cols:
                conn.exec_driver_sql(f"ALTER TABLE calls ADD COLUMN {name} JSON")
        conn.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    SQLModel.metadata.create_all(engine)
    ensure_columns()
    yield


app = FastAPI(title="Vexa Voice Backend", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5500",
        "http://127.0.0.1:5500",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- agents ----------
@app.post("/agents", response_model=AgentConfig, status_code=201)
def create_agent(body: AgentCreate):
    agent = AgentConfig.model_validate(body)
    with Session(engine) as s:
        s.add(agent)
        s.commit()
        s.refresh(agent)
    return agent


@app.get("/agents", response_model=list[AgentConfig])
def list_agents():
    with Session(engine) as s:
        return s.exec(select(AgentConfig)).all()


@app.get("/agents/{agent_id}", response_model=AgentConfig)
def get_agent(agent_id: int):
    with Session(engine) as s:
        agent = s.get(AgentConfig, agent_id)
    if agent is None:
        raise HTTPException(404, "agent not found")
    return agent


@app.put("/agents/{agent_id}", response_model=AgentConfig)
def update_agent(agent_id: int, body: AgentCreate):
    with Session(engine) as s:
        agent = s.get(AgentConfig, agent_id)
        if agent is None:
            raise HTTPException(404, "agent not found")
        for key, value in body.model_dump().items():
            setattr(agent, key, value)
        s.add(agent)
        s.commit()
        s.refresh(agent)
    return agent


# ---------- calls ----------
@app.post("/calls", response_model=CallStarted, status_code=201)
async def create_call(body: CallCreate):
    with Session(engine) as s:
        if s.get(AgentConfig, body.agent_id) is None:
            raise HTTPException(404, "agent not found")
        call = Call(agent_id=body.agent_id, room_name=f"call-{uuid.uuid4().hex[:10]}")
        s.add(call)
        s.commit()
        s.refresh(call)
        call_id, room_name = call.id, call.room_name

    identity = f"customer-{call_id}"

    lk = api.LiveKitAPI(LIVEKIT_HTTP_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
    try:
        await lk.room.create_room(
            api.CreateRoomRequest(name=room_name, empty_timeout=120)
        )
        await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=AGENT_NAME,
                room=room_name,
                metadata=json.dumps({"call_id": call_id, "agent_id": body.agent_id}),
            )
        )
    except Exception as e:
        raise HTTPException(502, f"LiveKit error: {e}")
    finally:
        await lk.aclose()

    token = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name("Customer")
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .to_jwt()
    )
    return CallStarted(
        call_id=call_id,
        room_name=room_name,
        livekit_url=LIVEKIT_URL,
        token=token,
        identity=identity,
    )


@app.get("/calls/{call_id}", response_model=Call)
def get_call(call_id: int):
    with Session(engine) as s:
        call = s.get(Call, call_id)
    if call is None:
        raise HTTPException(404, "call not found")
    return call


@app.patch("/calls/{call_id}/status", response_model=Call)
def update_call_status(call_id: int, body: CallStatusUpdate):
    if body.status not in {"active", "ended", "missed"}:
        raise HTTPException(422, "status must be active, ended or missed")
    with Session(engine) as s:
        call = s.get(Call, call_id)
        if call is None:
            raise HTTPException(404, "call not found")
        if call.status in {"ended", "missed"}:  # final states: ignore duplicates
            return call
        now = datetime.now(timezone.utc)
        call.status = body.status
        if body.status == "active":
            call.started_at = now
        else:
            call.ended_at = now
        s.add(call)
        s.commit()
        s.refresh(call)
        return call


class TranscriptLine(SQLModel):
    speaker: str  # "agent" | "user"
    text: str
    start_s: float  # seconds since the customer joined
    end_s: float
    ts: str  # ISO-8601 UTC time the utterance started
    interrupted: bool = False


class TranscriptIn(SQLModel):
    lines: list[TranscriptLine]


async def run_compliance(call_id: int):
    with Session(engine) as s:
        call = s.get(Call, call_id)
        lines = list(call.transcript or [])
    r1 = compliance.check_recording_disclosure(lines)
    try:
        r2 = await compliance.check_no_refund_promise(lines)
    except Exception as e:
        r2 = {"rule": "no_refund_promise", "method": "llm", "passed": None,
              "error": str(e)[:300]}
    result = {
        "status": "done",
        "all_passed": r1["passed"] is True and r2["passed"] is True,
        "checks": [r1, r2],
    }
    with Session(engine) as s:
        call = s.get(Call, call_id)
        call.compliance = result
        s.add(call)
        s.commit()


@app.post("/calls/{call_id}/transcript", status_code=202)
async def save_transcript(call_id: int, body: TranscriptIn, background: BackgroundTasks):
    lines = sorted((l.model_dump() for l in body.lines), key=lambda l: l["start_s"])
    with Session(engine) as s:
        call = s.get(Call, call_id)
        if call is None:
            raise HTTPException(404, "call not found")
        call.transcript = lines
        call.compliance = {"status": "pending"}
        s.add(call)
        s.commit()
    background.add_task(run_compliance, call_id)
    return {"saved": len(lines)}



@app.get("/calls", response_model=list[Call], response_model_exclude={"transcript", "compliance"})
def list_calls(status: Optional[str] = None):
    with Session(engine) as s:
        q = select(Call).order_by(Call.id.desc())
        if status:
            q = q.where(Call.status == status)
        return s.exec(q).all()


class OperatorToken(SQLModel):
    call_id: int
    room_name: str
    livekit_url: str
    token: str
    identity: str


@app.post("/calls/{call_id}/operator-token", response_model=OperatorToken)
def operator_token(call_id: int):
    with Session(engine) as s:
        call = s.get(Call, call_id)
    if call is None:
        raise HTTPException(404, "call not found")
    if call.status != "active":
        raise HTTPException(409, "call is not active")

    identity = f"operator-{call_id}-{uuid.uuid4().hex[:6]}"
    token = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name("Operator")
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=call.room_name,
                hidden=True,  # invisible to the customer and the agent
                can_subscribe=True,
                can_publish=False,  # listen-only
                can_publish_data=True,  # needed for the whisper in 4d
            )
        )
        .to_jwt()
    )
    return OperatorToken(
        call_id=call_id,
        room_name=call.room_name,
        livekit_url=LIVEKIT_URL,
        token=token,
        identity=identity,
    )
