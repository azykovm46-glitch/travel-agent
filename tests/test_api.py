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

    def test_bad_request_keeps_connection(self):
        with TestClient(app).websocket_connect("/api/chat/ws") as ws:
            ws.send_json({})
            self.assertEqual(ws.receive_json()["type"], "error")
            ws.send_json({"message": PLAN_QUERY})
            self.assertEqual(receive_turn(ws)[-1]["type"], "response")


if __name__ == "__main__":
    unittest.main()
