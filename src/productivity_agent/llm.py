from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Any

import httpx

from productivity_agent.analyzer import ProductivityAnalyzer
from productivity_agent.config import Settings
from productivity_agent.models import AnalysisSnapshot
from productivity_agent.parsing import ParsedEffectivenessText

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
        intent = _chat_intent(message)
        fallback = _chat_fallback(message, snapshot, analyzer, effectiveness or {}, intent)
        fallback = _append_unavailable_sources(fallback, snapshot)
        if not self.available:
            return fallback

        tasks = snapshot.tasks if intent in {"tasks", "mixed"} else []
        task_errors = snapshot.errors if intent in {"tasks", "mixed"} else []
        chat_snapshot = snapshot.model_copy(update={"tasks": tasks, "errors": task_errors})
        payload = self._payload(
            mode="chat",
            snapshot=chat_snapshot,
            analyzer=analyzer,
            extra={
                "user_message": message,
                "history": history or [],
                "effectiveness": effectiveness or {},
                "conversation_intent": intent,
                "task_context_available": bool(snapshot.tasks),
                "task_context_included": bool(tasks),
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

    async def extract_effectiveness(
        self,
        message: str,
        *,
        now_iso: str,
        target_sleep_time: str,
    ) -> ParsedEffectivenessText | None:
        if not self.available:
            return None
        payload = {
            "message": message,
            "now": now_iso,
            "target_sleep_time": target_sleep_time,
        }
        try:
            text = await self._chat_completion(
                model=self.settings.openrouter_model,
                instructions=_instructions_for("effectiveness_extract"),
                payload=payload,
                max_tokens=500,
                temperature=0,
            )
            raw = _extract_json_object(text)
            if not raw:
                return None
            return _effectiveness_from_json(raw, note=message)
        except Exception as exc:  # noqa: BLE001 - local parser remains the fallback.
            logger.exception("OpenRouter effectiveness extraction failed: %s", exc)
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

    async def _chat_completion(
        self,
        *,
        model: str,
        instructions: str,
        payload: dict[str, Any],
        max_tokens: int = 1400,
        temperature: float | None = None,
    ) -> str:
        request_payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            request_payload["temperature"] = temperature
        response = await self.client.post("/chat/completions", json=request_payload)
        response.raise_for_status()
        data = response.json()
        return _message_content(data["choices"][0].get("message") or {})

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
        "Все относительные времена, привычки сна и расписание считай по московскому времени, "
        "если пользователь явно не попросил другой часовой пояс. "
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
            "Не выдавай полный план дня, список всех задач, шаблон брифинга или отчет по всем источникам, "
            "если пользователь явно не просит план, задачи, брифинг, проектный обзор, фокус или отчет. "
            "На небольшой вопрос или утверждение отвечай свободно и коротко: 2-6 предложений, только по теме. "
            "Если пользователь спрашивает про сон, отвечай только про сон и последние sleep-метрики; "
            "задачи упоминай только если он сам связал вопрос с задачами или работой. "
            "Смешанные сообщения обрабатывай гибко: если пользователь одновременно просит действие, совет, "
            "систему привычек или объяснение возможностей, ответь по всем частям, но не превращай ответ "
            "в общий список задач без явной просьбы. "
            "Если пользователь просит план, задачи, приоритеты или проектный обзор, "
            "используй данные Notion и TickTick. "
            "Если пользователь просит создать, закрыть или перенести задачу, не утверждай, что действие выполнено; "
            "предложи отправить явную команду или коротко объясни, что нужно подтвердить. "
            "Если пользователь спрашивает про систему эффективности или сна, объясни, что можно писать фразы "
            "вроде «лег спать в 23:25» или «вчера лег в час ночи, потому что засиделся с другом, "
            "проснулся в 09:51», отчет доступен через /effectiveness, "
            "а картинка графика сна через /sleepchart."
        )
    if mode == "effectiveness_extract":
        return (
            "Ты извлекаешь структурированные факты сна из одного сообщения пользователя. "
            "Верни только JSON-объект без markdown и без пояснений. "
            "Поля: sleep_time, wake_time, day, sleep_deviation_reason, wake_deviation_reason. "
            "Формат времени строго HH:MM в 24-часовом формате или null. "
            "Формат day строго YYYY-MM-DD или null. "
            "Извлекай только явно сказанные факты, не придумывай время и причины. "
            "Фразы вроде «в час ночи», «около часа ночи», «в десять утра», «полночь» нормализуй в HH:MM. "
            "Причину нарушения сна заполняй только если пользователь объяснил, почему лег позже. "
            "Поздний подъем сам по себе не является нарушением режима сна: wake_deviation_reason обычно null. "
            "Если в сообщении есть «вчера», «сегодня» или «позавчера», вычисли day относительно поля now. "
            "Если фактов сна или подъема нет, верни все поля null."
        )
    return base + "Сформируй полезный краткий ответ по задачам."


def _chat_intent(message: str) -> str:
    lowered = message.lower()
    task_markers = (
        "задач",
        "делать",
        "план",
        "брифинг",
        "приоритет",
        "дедлайн",
        "проект",
        "notion",
        "ticktick",
        "фокус",
        "просроч",
        "перенести",
    )
    sleep_markers = ("сн", "спал", "спала", "уснул", "уснула", "проснулся", "проснулась", "лег", "легла")
    has_tasks = any(marker in lowered for marker in task_markers)
    has_sleep = any(marker in lowered for marker in sleep_markers)
    if has_tasks and has_sleep:
        return "mixed"
    if has_tasks:
        return "tasks"
    if has_sleep:
        return "sleep"
    return "general"


def _chat_fallback(
    message: str,
    snapshot: AnalysisSnapshot,
    analyzer: ProductivityAnalyzer,
    effectiveness: dict[str, Any],
    intent: str,
) -> str:
    if intent == "tasks":
        return analyzer.deterministic_today(snapshot.tasks)
    if intent == "mixed":
        return (
            f"{_sleep_fallback(effectiveness)}\n\n"
            "По задачам могу дать полный план, если напишешь «план на сегодня» или /today."
        )
    if intent == "sleep":
        return _sleep_fallback(effectiveness)
    return (
        "💬 Принял.\n\n"
        "Могу ответить коротко по конкретному вопросу, а полный план задач дам только когда ты прямо попросишь "
        "план, брифинг, фокус или список задач."
    )


def _sleep_fallback(effectiveness: dict[str, Any]) -> str:
    targets = effectiveness.get("targets") or {}
    recent_days = effectiveness.get("recent_days") or []
    if not recent_days:
        return (
            "🌙 Сон\n\n"
            "Пока мало данных по сну. Записывай короткими фразами: «лег спать в 22:10» и «проснулся в 07:30». "
            f"Цель сейчас: лечь до {targets.get('sleep_time', '22:00')}."
        )
    latest = recent_days[-1]
    sleep_score = latest.get("sleep_score")
    sleep_time = latest.get("sleep_time") or "нет данных"
    wake_time = latest.get("wake_time") or "нет данных"
    reason = latest.get("sleep_deviation_reason")
    text = (
        "🌙 Сон\n\n"
        f"Последняя запись: сон {sleep_time}, подъем {wake_time}. "
        f"Sleep score: {sleep_score if sleep_score is not None else 'нет данных'}/100. "
        f"Цель: лечь до {targets.get('sleep_time', '22:00')}. Подъем сохраняю справочно."
    )
    if reason:
        text += f"\n\nПоследняя отмеченная причина отклонения: {reason}."
    return text


def _message_content(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def _extract_json_object(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _effectiveness_from_json(raw: dict[str, Any], *, note: str) -> ParsedEffectivenessText | None:
    sleep_time = _safe_time(raw.get("sleep_time"))
    wake_time = _safe_time(raw.get("wake_time"))
    parsed_day = _safe_day(raw.get("day"))
    sleep_reason = _safe_text(raw.get("sleep_deviation_reason"))
    wake_reason = _safe_text(raw.get("wake_deviation_reason"))
    if not any((sleep_time, wake_time, parsed_day, sleep_reason, wake_reason)):
        return None
    return ParsedEffectivenessText(
        sleep_time=sleep_time,
        wake_time=wake_time,
        day=parsed_day,
        sleep_deviation_reason=sleep_reason,
        wake_deviation_reason=wake_reason,
        note=note.strip() or None,
    )


def _safe_time(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(?P<hour>[01]\d|2[0-3]):(?P<minute>[0-5]\d)", value.strip())
    if not match:
        return None
    return f"{match.group('hour')}:{match.group('minute')}"


def _safe_day(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _safe_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip(" \n\t.,;:!?")
    return cleaned[:500] or None


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
