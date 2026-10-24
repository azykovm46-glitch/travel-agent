"""Формат ответа агента фронтенду (контракт с участником 3, см. docs/CONTRACTS.md)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from agent.state import TripState

CardType = Literal["destination", "hotel", "transport", "transfer", "plan", "comparison",
                   "issues", "cart", "order", "assumptions"]


class QuickReply(BaseModel):
    label: str
    text: str | None = None              # отправить как сообщение пользователя
    action: dict[str, Any] | None = None  # или как структурное действие


class Question(BaseModel):
    slot: str
    text: str
    options: list[QuickReply] = Field(default_factory=list)


class Card(BaseModel):
    type: CardType
    data: dict[str, Any]


class TraceStep(BaseModel):
    step: str
    detail: str
    ms: int = 0


class AgentResponse(BaseModel):
    session_id: str
    stage: str
    message: str
    questions: list[Question] = Field(default_factory=list)
    cards: list[Card] = Field(default_factory=list)
    quick_replies: list[QuickReply] = Field(default_factory=list)
    diff: list[dict[str, Any]] = Field(default_factory=list)
    trace: list[TraceStep] = Field(default_factory=list)
    state: TripState
