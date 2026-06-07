from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from productivity_agent.analyzer import ProductivityAnalyzer
from productivity_agent.config import Settings
from productivity_agent.models import AnalysisSnapshot

logger = logging.getLogger(__name__)


class LLMGenerator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: httpx.AsyncClient | None = None

    @property
    def available(self) -> bool:
        return bool(self.settings.openrouter_api_key)

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.settings.openrouter_base_url.rstrip("/"),
                timeout=httpx.Timeout(45.0, connect=30.0),
                headers=self._headers(),
            )
        return self._client

    async def generate(self, mode: str, snapshot: AnalysisSnapshot, extra: dict[str, Any] | None = None) -> str:
        analyzer = ProductivityAnalyzer(today=snapshot.generated_at.date())
        fallback = _append_unavailable_sources(analyzer.deterministic_today(snapshot.tasks), snapshot)
        if not self.available:
            return fallback

        payload = self._payload(mode=mode, snapshot=snapshot, analyzer=analyzer, extra=extra)
        try:
            text = await self._chat_completion(
                model=self.settings.openrouter_model,
                instructions=_instructions_for(mode),
                payload=payload,
            )
            return _clean_output(text.strip()) or fallback
        except Exception as exc:  # noqa: BLE001 - fallback is more important than surfacing SDK internals.
            logger.exception("OpenRouter generation failed: %s", exc)
            return fallback

    async def chat(
        self,
        message: str,
        snapshot: AnalysisSnapshot,
        history: list[dict[str, str]] | None = None,
        effectiveness: dict[str, Any] | None = None,
    ) -> str:
        analyzer = ProductivityAnalyzer(today=snapshot.generated_at.date())
        fallback = (
            "💬 Диалог\n"
            "Сейчас могу надежно ответить по задачам локальным планом, а для изменений задач попрошу подтверждение. "
            "Для эффективности можно записывать: «закончил работу в 21:10», «начал отход ко сну в 22:40», "
            "«лег спать в 23:25», «проснулся в 07:40», «главный фокус выполнен». Отчет: /effectiveness.\n\n"
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
                "effectiveness": effectiveness or {},
            },
        )
        try:
            text = await self._chat_completion(
                model=self.settings.openrouter_model,
                instructions=_instructions_for("chat"),
                payload=payload,
            )
            return _clean_output(text.strip()) or fallback
        except Exception as exc:  # noqa: BLE001 - fallback is more important than surfacing SDK internals.
            logger.exception("OpenRouter chat failed: %s", exc)
            return fallback

    async def generate_image(self, prompt: str) -> str | None:
        if not self.available:
            return None
        request_payload: dict[str, Any] = {
            "model": self.settings.openrouter_image_model,
            "messages": [{"role": "user", "content": prompt}],
            "modalities": ["image", "text"],
            "image_config": {
                "aspect_ratio": "16:9",
                "image_size": "1024x576",
            },
        }
        try:
            response = await self.client.post("/chat/completions", json=request_payload)
            response.raise_for_status()
            data = response.json()
            message = data["choices"][0]["message"]
            images = message.get("images") or []
            if not images:
                return None
            image = images[0]
            return (image.get("image_url") or image.get("imageUrl") or {}).get("url")
        except Exception as exc:  # noqa: BLE001 - image generation should not break bot flow.
            logger.exception("OpenRouter image generation failed: %s", exc)
            return None

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

    async def _chat_completion(self, *, model: str, instructions: str, payload: dict[str, Any]) -> str:
        request_payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "max_tokens": 1400,
        }
        response = await self.client.post("/chat/completions", json=request_payload)
        response.raise_for_status()
        data = response.json()
        return str(data["choices"][0]["message"].get("content") or "")

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.settings.openrouter_api_key}",
            "Content-Type": "application/json",
        }
        if self.settings.openrouter_http_referer:
            headers["HTTP-Referer"] = self.settings.openrouter_http_referer
        if self.settings.openrouter_app_title:
            headers["X-OpenRouter-Title"] = self.settings.openrouter_app_title
        return headers


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
            "Смешанные сообщения обрабатывай гибко: если пользователь одновременно просит действие, совет, "
            "систему привычек или объяснение возможностей, ответь по всем частям, а не только списком задач. "
            "Показывай, что ты умеешь планировать, анализировать перегруз, вести привычку сна, собирать метрики "
            "эффективности, учитывать причины отклонений сна и давать короткие рекомендации. "
            "Если пользователь просит план, задачи, приоритеты или проектный обзор, "
            "используй данные Notion и TickTick. "
            "Если пользователь просит создать, закрыть или перенести задачу, не утверждай, что действие выполнено; "
            "предложи отправить явную команду или коротко объясни, что нужно подтвердить. "
            "Если пользователь спрашивает про систему эффективности или сна, объясни, что можно писать фразы "
            "вроде «закончил работу в 21:10», «начал отход ко сну в 22:40», «лег спать в 23:25», "
            "«проснулся в 07:40», «главный фокус выполнен», отчет доступен через /effectiveness, "
            "а картинка графика сна через /sleepchart."
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
        r"\bOpenRouter\b",
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
