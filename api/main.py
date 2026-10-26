"""HTTP API агента для фронтенда (участник 3) и автопрогонов (участник 4).

    uvicorn api.main:app --reload --port 8000

GET  /             запасной веб-интерфейс (static/index.html, без сборки)
POST /api/chat     {session_id?, message?, action?} -> AgentResponse
WS   /api/chat/ws  те же запросы; в ответ поток событий step_start / trace, затем response
GET  /api/session/{id}                              -> TripState
GET  /api/health
Контракт ответа — agent/schemas.py и docs/CONTRACTS.md.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from cli import load_env  # noqa: E402

load_env()

from agent import TravelAgent  # noqa: E402
from agent.memory import ProfileStore, SessionStore  # noqa: E402
from agent.schemas import AgentResponse  # noqa: E402

app = FastAPI(title="О!дин запрос — travel agent", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
                   allow_methods=["*"], allow_headers=["*"])
# Профили пользователей и сессии в SQLite: перезапуск сервера не теряет разговоры.
# Путь меняется MEMORY_DB (файл в .gitignore)
MEMORY_DB = os.getenv("MEMORY_DB") or Path(__file__).resolve().parents[1] / "data" / "profiles.sqlite3"
agent = TravelAgent(memory=ProfileStore(MEMORY_DB), sessions=SessionStore(MEMORY_DB))


class ChatRequest(BaseModel):
    session_id: str | None = None
    user_id: str | None = None          # необязательно: включает память между сессиями
    message: str | None = None
    action: dict[str, Any] | None = None


INDEX = Path(__file__).resolve().parents[1] / "static" / "index.html"


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """Запасной веб-интерфейс: чат, карточки, допущения, шаги агента по WebSocket."""
    return FileResponse(INDEX, media_type="text/html; charset=utf-8")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "llm": agent.llm.enabled, "llm_available": agent.llm.available,
            "llm_mode": agent.llm.mode, "llm_last_error": agent.llm.last_error, "model": agent.llm.model or None,
            "backend": os.getenv("TOOLS_BACKEND", "tools.mock_backend")}


# state.candidates — внутренний кэш результатов инструментов (~половина ответа), фронтенду не нужен
@app.post("/api/chat", response_model=AgentResponse, response_model_exclude={"state": {"candidates"}})
def chat(req: ChatRequest) -> AgentResponse:
    if not req.message and not req.action:
        raise HTTPException(400, "нужно message или action")
    return agent.handle(req.session_id, message=req.message, action=req.action, user_id=req.user_id)


@app.get("/api/session/{session_id}")
def session(session_id: str) -> dict:
    state = agent.get_state(session_id)
    if not state:
        raise HTTPException(404, "сессия не найдена")
    return state.model_dump(mode="json", exclude={"candidates"})


log = logging.getLogger(__name__)


@app.websocket("/api/chat/ws")
async def chat_ws(ws: WebSocket) -> None:
    """Стриминг шагов агента. Клиент шлёт те же JSON, что в POST /api/chat (сколько угодно ходов
    в одном соединении), сервер на каждый ход отвечает событиями:
      {"type": "step_start", "step": "search_hotels", "label": "ищу отели…"}
      {"type": "trace", "step": "search_hotels", "detail": "14 отелей", "ms": 3}
      {"type": "response", "data": AgentResponse}      — последним
      {"type": "error", "detail": "..."}               — если запрос неверный или агент упал
    """
    await ws.accept()
    loop = asyncio.get_running_loop()
    try:
        while True:
            req = await ws.receive_json()
            if not isinstance(req, dict) or not (req.get("message") or req.get("action")):
                await ws.send_json({"type": "error", "detail": "нужно message или action"})
                continue
            queue: asyncio.Queue = asyncio.Queue()

            def on_event(event: dict) -> None:       # вызывается из потока агента
                loop.call_soon_threadsafe(queue.put_nowait, event)

            task = loop.run_in_executor(None, lambda: agent.handle(
                req.get("session_id"), message=req.get("message"), action=req.get("action"), on_event=on_event,
                user_id=req.get("user_id")))
            while True:
                getter = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    await ws.send_json(getter.result())
                    continue
                getter.cancel()
                if getter.done() and not getter.cancelled():   # успел забрать событие до отмены
                    await ws.send_json(getter.result())
                break
            while not queue.empty():
                await ws.send_json(queue.get_nowait())
            try:
                resp = task.result()
            except Exception as e:
                log.exception("agent failed")
                await ws.send_json({"type": "error", "detail": f"агент упал: {e}"})
                continue
            await ws.send_json({"type": "response",
                                "data": resp.model_dump(mode="json", exclude={"state": {"candidates"}})})
    except WebSocketDisconnect:
        pass
