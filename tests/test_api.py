"""Тесты HTTP API и WebSocket-стриминга. Без сети и без LLM.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["AGENT_OFFLINE"] = "1"
os.environ["MEMORY_DB"] = str(Path(tempfile.mkdtemp()) / "profiles.sqlite3")   # не пишем в папку проекта

from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402

PLAN_QUERY = "Сочи в ноябре вдвоём из Москвы"


def receive_turn(ws) -> list[dict]:
    """Все события одного хода, последнее — response или error."""
    events = []
    while True:
        events.append(ws.receive_json())
        if events[-1]["type"] in ("response", "error"):
            return events


class RestTest(unittest.TestCase):
    def test_chat_hides_internal_cache(self):
        r = TestClient(app).post("/api/chat", json={"message": PLAN_QUERY}).json()
        self.assertEqual(r["stage"], "plan")
        self.assertNotIn("candidates", r["state"])

    def test_empty_request_is_400(self):
        self.assertEqual(TestClient(app).post("/api/chat", json={}).status_code, 400)


class MemoryApiTest(unittest.TestCase):
    def test_profile_from_previous_booking_skips_questions(self):
        c = TestClient(app)
        r = c.post("/api/chat", json={"user_id": "u-api", "message": "Сочи в ноябре вдвоём с ребёнком 6 лет из Казани"}).json()
        c.post("/api/chat", json={"session_id": r["session_id"], "action": {"type": "checkout"}})
        r = c.post("/api/chat", json={"user_id": "u-api", "message": "хочу куда-нибудь на море в декабре"}).json()
        self.assertNotEqual(r["stage"], "clarify")                     # откуда и кто — уже знаем
        self.assertEqual(r["state"]["origin_city"], "Казань")
        self.assertIn("profile", [a["source"] for a in r["state"]["assumptions"]])


class WebSocketTest(unittest.TestCase):
    def test_steps_stream_before_response(self):
        with TestClient(app).websocket_connect("/api/chat/ws") as ws:
            ws.send_json({"message": PLAN_QUERY})
            events = receive_turn(ws)
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds[-1], "response")
        self.assertIn("ищу отели…", [e.get("label") for e in events if e["type"] == "step_start"])
        response = events[-1]["data"]
        streamed = [(e["step"], e["detail"]) for e in events if e["type"] == "trace"]
        self.assertEqual(streamed, [(s["step"], s["detail"]) for s in response["trace"]])
        self.assertNotIn("candidates", response["state"])

    def test_several_turns_in_one_connection(self):
        with TestClient(app).websocket_connect("/api/chat/ws") as ws:
            ws.send_json({"message": PLAN_QUERY})
            sid = receive_turn(ws)[-1]["data"]["session_id"]
            ws.send_json({"session_id": sid, "action": {"type": "checkout"}})
            last = receive_turn(ws)[-1]
        self.assertEqual(last["data"]["stage"], "booked")

    def test_agent_crash_is_reported_and_connection_survives(self):
        from unittest import mock
        from api import main
        with TestClient(app).websocket_connect("/api/chat/ws") as ws:
            with mock.patch.object(main.agent, "handle", side_effect=RuntimeError("бэкенд упал")):
                ws.send_json({"message": PLAN_QUERY})
                last = receive_turn(ws)[-1]
            self.assertEqual(last["type"], "error")
            self.assertIn("бэкенд упал", last["detail"])
            ws.send_json({"message": PLAN_QUERY})
            self.assertEqual(receive_turn(ws)[-1]["type"], "response")

    def test_bad_request_keeps_connection(self):
        with TestClient(app).websocket_connect("/api/chat/ws") as ws:
            ws.send_json({})
            self.assertEqual(ws.receive_json()["type"], "error")
            ws.send_json({"message": PLAN_QUERY})
            self.assertEqual(receive_turn(ws)[-1]["type"], "response")


class LLMOutageMidSessionTest(unittest.TestCase):
    """LLM отвечала, потом провайдер упал (503) — разговор продолжается на правилах, без ошибки API."""

    def test_session_survives_provider_outage(self):
        from types import SimpleNamespace
        from unittest import mock
        from api import main

        class Down(Exception):
            status_code = 503

        def answer(**kw):
            if "tools" in kw:
                call = SimpleNamespace(function=SimpleNamespace(arguments='{"intent": "plan"}'))
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[call], content=None))])
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=None, content="Готово."))])

        def down(**kw):
            raise Down("503 SERVICE_UNAVAILABLE")

        llm = main.agent.llm
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=answer)))
        with mock.patch.multiple(llm, enabled=True, model="fake", fallback_model="", reasoning_effort="",
                                 mode="tools", retry_pause=0, _down_until=0.0, _client=client):
            c = TestClient(app)
            r = c.post("/api/chat", json={"message": PLAN_QUERY}).json()
            self.assertTrue(next(s["detail"] for s in r["trace"] if s["step"] == "understand").startswith("LLM"))
            client.chat.completions.create = down
            import logging
            muted = logging.root.manager.disable           # test_agent глушит предупреждения на весь прогон
            logging.disable(logging.NOTSET)
            try:
                with self.assertLogs("agent.llm", "WARNING") as logs:
                    r = c.post("/api/chat", json={"session_id": r["session_id"], "message": "с нами едет собака"})
            finally:
                logging.disable(muted)
            self.assertTrue(any("повтор" in m for m in logs.output))               # сначала переспросили
            self.assertEqual(r.status_code, 200)
            r = r.json()
            self.assertTrue(r["state"]["plan"]["hotel"]["pets_allowed"])          # правила поняли собаку
            understand = next(s["detail"] for s in r["trace"] if s["step"] == "understand")
            self.assertFalse(understand.startswith("LLM"))
            self.assertIn("503", understand)                                      # в trace видна причина


if __name__ == "__main__":
    unittest.main()
