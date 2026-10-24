"""HTTP API агента для фронтенда (участник 3) и автопрогонов (участник 4).

    uvicorn api.main:app --reload --port 8000

POST /api/chat     {session_id?, message?, action?} -> AgentResponse
GET  /api/session/{id}                              -> TripState
GET  /api/health
Контракт ответа — agent/schemas.py и docs/CONTRACTS.md.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from cli import load_env  # noqa: E402

load_env()

from agent import TravelAgent  # noqa: E402
from agent.schemas import AgentResponse  # noqa: E402

app = FastAPI(title="О!дин запрос — travel agent", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
                   allow_methods=["*"], allow_headers=["*"])
agent = TravelAgent()


class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str | None = None
    action: dict[str, Any] | None = None


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "llm": agent.llm.enabled, "model": agent.llm.model or None,
            "backend": os.getenv("TOOLS_BACKEND", "tools.mock_backend")}


@app.post("/api/chat", response_model=AgentResponse)
def chat(req: ChatRequest) -> AgentResponse:
    if not req.message and not req.action:
        raise HTTPException(400, "нужно message или action")
    return agent.handle(req.session_id, message=req.message, action=req.action)


@app.get("/api/session/{session_id}")
def session(session_id: str) -> dict:
    state = agent.get_state(session_id)
    if not state:
        raise HTTPException(404, "сессия не найдена")
    return state.model_dump(mode="json")
