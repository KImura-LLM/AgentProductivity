from __future__ import annotations

import json
import logging
import re
from typing import Any

from openai import AsyncOpenAI

from productivity_agent.analyzer import ProductivityAnalyzer
from productivity_agent.config import Settings
from productivity_agent.models import AnalysisSnapshot

logger = logging.getLogger(__name__)


class LLMGenerator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: AsyncOpenAI | None = None

    @property
    def available(self) -> bool:
        return bool(self.settings.openai_api_key)

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(api_key=self.settings.openai_api_key)
        return self._client

    async def generate(self, mode: str, snapshot: AnalysisSnapshot, extra: dict[str, Any] | None = None) -> str:
        analyzer = ProductivityAnalyzer(today=snapshot.generated_at.date())
        fallback = _append_unavailable_sources(analyzer.deterministic_today(snapshot.tasks), snapshot)
        if not self.available:
            return fallback

        payload = self._payload(mode=mode, snapshot=snapshot, analyzer=analyzer, extra=extra)
        try:
            response = await self.client.responses.create(
                model=self.settings.openai_model,
                instructions=_instructions_for(mode),
                input=json.dumps(payload, ensure_ascii=False),
                max_output_tokens=1400,
            )
            return _clean_output(response.output_text.strip()) or fallback
        except Exception as exc:  # noqa: BLE001 - fallback is more important than surfacing SDK internals.
            logger.exception("OpenAI generation failed: %s", exc)
            return fallback

    async def chat(
        self,
        message: str,
        snapshot: AnalysisSnapshot,
        history: list[dict[str, str]] | None = None,
    ) -> str:
        analyzer = ProductivityAnalyzer(today=snapshot.generated_at.date())
        fallback = (
            "💬 Диалог\n"
            "Сейчас могу надежно ответить по задачам только локальным планом.\n\n"
            f"{analyzer.deterministic_today(snapshot.tasks)}"
        )
        fallback = _append_unavailable_sources(fallback, snapshot)
        if not self.available:
            return fallback

        payload = self._payload(
            mode="chat",
            snapshot=snapshot,
            analyzer=analyzer,
            extra={
                "user_message": message,
                "history": history or [],
            },
        )
        try:
            response = await self.client.responses.create(
                model=self.settings.openai_model,
                instructions=_instructions_for("chat"),
                input=json.dumps(payload, ensure_ascii=False),
                max_output_tokens=1400,
            )
            return _clean_output(response.output_text.strip()) or fallback
        except Exception as exc:  # noqa: BLE001 - fallback is more important than surfacing SDK internals.
            logger.exception("OpenAI chat failed: %s", exc)
            return fallback

    def _payload(
        self,
        *,
        mode: str,
        snapshot: AnalysisSnapshot,
        analyzer: ProductivityAnalyzer,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "mode": mode,
            "today": snapshot.generated_at.date().isoformat(),
            "tasks": analyzer.compact_task_payload(snapshot.tasks),
            "source_summary": analyzer.source_summary(snapshot.tasks),
            "unavailable_sources": [error.source.value for error in snapshot.errors],
            "extra": extra or {},
        }


def _instructions_for(mode: str) -> str:
    base = (
        "Ты персональный ассистент продуктивности. Отвечай на русском, живо, кратко и по делу. "
        "Оформляй ответ красивыми смысловыми блоками с уместными emoji в заголовках. "
        "Не выдумывай задач и явно отделяй факты от рекомендаций. "
        "Никогда не показывай пользователю служебные поля, JSON, metadata, generated_at, timezone, model, "
        "API, source_errors, score, payload, technical facts или отладочные детали. "
        "Не упоминай Europe/Moscow и название модели. "
        "Если один источник недоступен, укажи только коротко: «Notion недоступен» или «TickTick недоступен». "
    )
    if mode in {"briefing", "today"}:
        return (
            base
            + "Сформируй план дня. Не игнорируй Notion: отдельно покажи проектные задачи Notion, "
            "если они есть в контексте. Отдельно покажи личные задачи TickTick. "
            "Включай просроченные, сегодняшние, высокоприоритетные задачи без дедлайна, главный фокус, "
            "что можно перенести, порядок действий и риск дня. Уложись в 1200-2500 символов."
        )
    if mode == "week":
        return (
            base
            + "Сформируй недельный обзор: дедлайны, перегруженные дни, ключевые проекты, задачи без дедлайна, "
            "и рекомендации по перераспределению."
        )
    if mode == "review":
        return (
            base
            + "Сформируй вечерний review: что было запланировано, что нужно отметить, что зависло, "
            "и что предложить перенести на завтра."
        )
    if mode == "project":
        return (
            base
            + "Сформируй статус проекта: активные задачи, дедлайны, зависшие задачи, "
            "следующие действия и риски."
        )
    if mode == "life":
        return (
            base
            + "Сформируй обзор личных задач TickTick: сегодня, просроченные, "
            "повторяющиеся и задачи без времени."
        )
    if mode == "focus":
        return base + "Выбери один главный фокус и коротко объясни, почему он первый."
    if mode == "stuck":
        return base + "Покажи зависшие задачи и предложи следующие конкретные действия."
    if mode == "chat":
        return (
            base
            + "Ответь на обычное сообщение пользователя с учетом истории диалога и задач из контекста. "
            "Если пользователь просит план, задачи, приоритеты или проектный обзор, "
            "используй данные Notion и TickTick. "
            "Если пользователь просит создать, закрыть или перенести задачу, не утверждай, что действие выполнено; "
            "предложи отправить явную команду или коротко объясни, что нужно подтвердить."
        )
    return base + "Сформируй полезный краткий ответ по задачам."


def _clean_output(text: str) -> str:
    technical_markers = (
        r"\bgenerated[_ ]?at\b",
        r"\btime\s*zone\b",
        r"\btimezone\b",
        r"\bEurope/Moscow\b",
        r"\bsource_errors\b",
        r"\bpayload\b",
        r"\bmetadata\b",
        r"\bscore\b",
        r"\bOpenAI\b",
        r"\bgpt-[\w.-]+\b",
    )
    marker_re = re.compile("|".join(technical_markers), flags=re.IGNORECASE)
    cleaned_lines: list[str] = []
    for line in text.splitlines():
        line = re.sub(
            r"\s*\((?:generated[_ ]?at|time\s*zone|timezone|source_errors|payload|metadata|score)[^)]+\)",
            "",
            line,
            flags=re.IGNORECASE,
        ).rstrip()
        if marker_re.search(line):
            continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()


def _append_unavailable_sources(text: str, snapshot: AnalysisSnapshot) -> str:
    if not snapshot.errors:
        return text
    names = []
    for error in snapshot.errors:
        if error.source.value == "notion":
            names.append("Notion")
        elif error.source.value == "ticktick":
            names.append("TickTick")
        else:
            names.append(error.source.value)
    unique_names = list(dict.fromkeys(names))
    lines = "\n".join(f"• {name} недоступен" for name in unique_names)
    return f"{text}\n\n⚠️ Источники\n{lines}"
