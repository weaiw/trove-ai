"""Agentic research endpoint — SSE streaming of multi-stage research progress."""
import json
import logging
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session, get_db
from app.dependencies import get_current_user
from app.models import User
from app.services.research_agent import run_research
from app.services.tool_agent import run_tool_agent

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/research", tags=["research"])


class AskBody(BaseModel):
    query: str = Field(..., min_length=2)


async def _sse_stream(gen) -> AsyncIterator[bytes]:
    """SSE format: each event is `data: <json>\\n\\n`."""
    async for ev in gen:
        payload = json.dumps(ev, ensure_ascii=False)
        yield f"data: {payload}\n\n".encode("utf-8")


async def _wrap(query: str, user_id, mode: str, kb_purpose: str = "") -> AsyncIterator[bytes]:
    """Open a fresh DB session bound to the stream, run the agent, stream events."""
    async with async_session() as db:
        if mode == "tool":
            async for ev in run_tool_agent(db, query, user_id):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8")
        else:
            async for ev in run_research(db, query, user_id, kb_purpose=kb_purpose):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8")


@router.post("/ask")
async def research_ask(
    body: AskBody,
    current_user: User = Depends(get_current_user),
):
    """4-stage sequential research (plan → retrieve → synthesize → critique)."""
    return StreamingResponse(
        _wrap(body.query, current_user.id, mode="sequential", kb_purpose=current_user.kb_purpose or ""),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/agent")
async def research_agent(
    body: AskBody,
    current_user: User = Depends(get_current_user),
):
    """Tool-using agent (ReAct loop with library tools). The agent decides
    which tool to call when, vs. /ask's fixed pipeline. Useful for exploratory
    questions where the right sequence of retrievals isn't known upfront."""
    return StreamingResponse(
        _wrap(body.query, current_user.id, mode="tool"),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
