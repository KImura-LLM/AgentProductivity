from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from productivity_agent.agent import AgentRuntime
from productivity_agent.analyzer import ProductivityAnalyzer, format_task, now_in_timezone
from productivity_agent.config import Settings
from productivity_agent.llm import LLMGenerator
from productivity_agent.models import (
    AnalysisSnapshot,
    CandidateMatch,
    NormalizedTask,
    PendingAction,
    TaskSource,
)
from productivity_agent.parsing import (
    parse_reschedule_text,
    parse_status_change,
    parse_task_text,
)
from productivity_agent.repository import TaskRepository
from productivity_agent.storage import JsonStateStore

YES_VALUES = {"да", "yes", "y", "ок", "подтверждаю"}
NO_VALUES = {"нет", "no", "n", "отмена", "cancel"}


class ProductivityService:
    def __init__(
        self,
        settings: Settings,
        repository: TaskRepository,
        llm: LLMGenerator,
        state_store: JsonStateStore,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.llm = llm
        self.state_store = state_store
        self.runtime = AgentRuntime()
        self._register_tools()

    async def today(self) -> str:
        collection = await self.repository.collect()
        return await self._generate("today", collection.tasks, collection.errors)

    async def briefing(self) -> str:
        collection = await self.repository.collect()
        return await self._generate("briefing", collection.tasks, collection.errors)

    async def week(self) -> str:
        collection = await self.repository.collect()
        return await self._generate("week", collection.tasks, collection.errors)

    async def review(self) -> str:
        today = self._now().date()
        collection = await self.repository.collect(date_to=today, include_done=True)
        return await self._generate("review", collection.tasks, collection.errors)

    async def project(self, project_name: str) -> str:
        collection = await self.repository.collect(project=project_name, include_done=True)
        if not collection.tasks:
            return f"🔎 Проект\n\nПо проекту «{project_name}» задач не найдено."
        return await self._generate("project", collection.tasks, collection.errors, {"project": project_name})

    async def life(self) -> str:
        collection = await self.repository.collect(
            sources={TaskSource.TICKTICK},
        )
        return await self._generate("life", collection.tasks, collection.errors)

    async def focus(self) -> str:
        collection = await self.repository.collect()
        return await self._generate("focus", collection.tasks, collection.errors)

    async def stuck(self) -> str:
        collection = await self.repository.collect(include_done=False)
        analyzer = ProductivityAnalyzer(today=self._now().date())
        stuck_tasks = analyzer.stale_tasks(collection.tasks)
        return await self._generate("stuck", stuck_tasks, collection.errors)

    async def settings_text(self) -> str:
        safe = self.settings.as_safe_dict()
        databases = ", ".join(safe["notion_databases"]) or "не настроены"
        notion_status = f"подключен ({databases})" if self.repository.notion.configured() else "не настроен"
        ticktick_status = "подключен" if self.repository.ticktick.configured() else "не настроен"
        return (
            "⚙️ Настройки\n\n"
            "🔗 Источники\n"
            f"• Notion: {notion_status}\n"
            f"• TickTick: {ticktick_status}\n\n"
            "🕘 Расписание\n"
            f"• Утренний брифинг: {safe['morning_briefing_time']}\n"
            f"• Вечерний review: {safe['evening_review_time']}\n"
            f"• Недельный обзор: воскресенье {safe['weekly_review_time']}\n\n"
            "⌨️ Команды доступны в меню Telegram."
        )

    async def start_add(self, raw_text: str, user_id: int) -> str:
        parsed = parse_task_text(raw_text, now=self._now(), tzinfo=self.settings.tzinfo)
        if not parsed.title:
            return "📝 Не понял название задачи.\n\nПример: /add ticktick завтра в 12:00 позвонить врачу"
        if not parsed.source_hint:
            return (
                "📝 Куда добавить задачу: TickTick или Notion?\n\nПовтори команду с источником, "
                "например: /add ticktick завтра позвонить врачу"
            )

        source = TaskSource(parsed.source_hint)
        destination = None
        if source == TaskSource.NOTION:
            notion_databases = [db for db in self.settings.notion_databases if db.type == "tasks"]
            if len(notion_databases) > 1:
                names = ", ".join(db.name for db in notion_databases)
                return (
                    f"🗂️ В какую Notion-базу добавить задачу?\n\nДоступные базы: {names}. "
                    "Повтори команду с названием базы."
                )
            if notion_databases:
                destination = notion_databases[0].name

        payload = {
            "source": source.value,
            "title": parsed.title,
            "deadline": parsed.deadline.isoformat() if parsed.deadline else None,
            "time": parsed.time,
            "destination": destination,
        }
        action = self._pending_action(
            user_id=user_id,
            kind="create_task",
            summary=self._create_summary(payload),
            payload=payload,
        )
        self.state_store.set_pending_action(action)
        return f"{action.summary}\n\n✅ Подтверди: да / нет."

    async def start_done(self, query: str, user_id: int) -> str:
        cleaned = (
            query.replace("/done", "")
            .replace("Отметь задачу", "")
            .replace("отметь задачу", "")
            .replace("как выполненную", "")
            .strip(" .«»\"")
        )
        return await self._start_candidate_action(cleaned, user_id, kind="complete_task")

    async def start_reschedule(self, raw_text: str, user_id: int) -> str:
        query, new_deadline = parse_reschedule_text(raw_text, now=self._now(), tzinfo=self.settings.tzinfo)
        if not new_deadline:
            return "📅 Не понял новую дату.\n\nПример: /reschedule Подготовить отчет на завтра"
        return await self._start_candidate_action(
            query,
            user_id,
            kind="reschedule_task",
            extra_payload={"deadline": new_deadline.isoformat()},
        )

    async def start_status_change(self, raw_text: str, user_id: int) -> str:
        parsed = parse_status_change(raw_text)
        if not parsed:
            return "🔄 Не понял смену статуса.\n\nПример: Поставь задачу «Дизайн модального окна» в In Progress"
        query, status = parsed
        return await self._start_candidate_action(
            query,
            user_id,
            kind="update_status",
            extra_payload={"status": status},
        )

    async def handle_pending_response(self, text: str, user_id: int) -> str | None:
        action = self.state_store.get_pending_action(user_id)
        if not action:
            return None

        now = self._now()
        normalized = text.strip().lower()
        if action.expired(now):
            self.state_store.clear_pending_action(user_id)
            return "⌛ Подтверждение устарело.\n\nПовтори команду."

        if normalized in NO_VALUES:
            self.state_store.clear_pending_action(user_id)
            return "👌 Ок, ничего не меняю."

        if action.kind == "select_candidate" and normalized.isdigit():
            index = int(normalized) - 1
            choices = action.payload.get("choices", [])
            if index < 0 or index >= len(choices):
                return "🔢 Нет такого номера.\n\nОтветь номером из списка или «отмена»."
            selected = choices[index]
            exact = self._pending_action(
                user_id=user_id,
                kind=action.payload["next_kind"],
                summary=self._change_summary(action.payload["next_kind"], selected["task"], action.payload),
                payload={
                    **action.payload,
                    "task": selected["task"],
                },
            )
            self.state_store.set_pending_action(exact)
            return f"{exact.summary}\n\n✅ Подтверди: да / нет."

        if normalized in YES_VALUES:
            result = await self._execute_pending(action)
            self.state_store.clear_pending_action(user_id)
            return result

        return None

    async def _start_candidate_action(
        self,
        query: str,
        user_id: int,
        kind: str,
        extra_payload: dict[str, Any] | None = None,
    ) -> str:
        if not query:
            return "🔎 Не понял, какую задачу искать."
        candidates = await self.repository.find_candidates(query)
        if not candidates:
            return f"🔎 Не нашел задачу по запросу «{query}».\n\nНичего не изменено."

        payload = {"query": query, **(extra_payload or {})}
        if self._has_single_strong_match(candidates):
            task_dump = candidates[0].task.model_dump(mode="json")
            action = self._pending_action(
                user_id=user_id,
                kind=kind,
                summary=self._change_summary(kind, task_dump, payload),
                payload={**payload, "task": task_dump},
            )
            self.state_store.set_pending_action(action)
            return f"{action.summary}\n\n✅ Подтверди: да / нет."

        choices = [
            {"score": candidate.score, "task": candidate.task.model_dump(mode="json")}
            for candidate in candidates
        ]
        action = self._pending_action(
            user_id=user_id,
            kind="select_candidate",
            summary="Нужно выбрать задачу.",
            payload={**payload, "next_kind": kind, "choices": choices},
        )
        self.state_store.set_pending_action(action)
        lines = ["🔎 Нашел несколько похожих задач:"]
        for idx, candidate in enumerate(candidates, start=1):
            lines.append(f"{idx}. {format_task(candidate.task)}")
        lines.append('\nОтветь номером задачи или "отмена".')
        return "\n".join(lines)

    async def _execute_pending(self, action: PendingAction) -> str:
        payload = action.payload
        if action.kind == "create_task":
            source = TaskSource(payload["source"])
            deadline = date.fromisoformat(payload["deadline"]) if payload.get("deadline") else None
            task = await self.repository.create_task(
                source=source,
                title=payload["title"],
                deadline=deadline,
                time=payload.get("time"),
                destination=payload.get("destination"),
            )
            return f"✅ Создал задачу\n• {format_task(task)}"

        task = NormalizedTask.model_validate(payload["task"])
        if action.kind == "complete_task":
            updated = await self.repository.complete_task(task)
            return f"✅ Отметил как выполненную\n• {format_task(updated)}"
        if action.kind == "reschedule_task":
            deadline = date.fromisoformat(payload["deadline"])
            updated = await self.repository.reschedule_task(task, deadline)
            return f"📅 Перенес задачу\n• {format_task(updated)}"
        if action.kind == "update_status":
            updated = await self.repository.update_status(task, payload["status"])
            return f"🔄 Изменил статус задачи\n• {format_task(updated)}"
        return "⚠️ Неизвестное действие.\n\nНичего не изменено."

    async def chat(self, message: str, history: list[dict[str, str]] | None = None) -> str:
        collection = await self.repository.collect()
        snapshot = AnalysisSnapshot(
            tasks=collection.tasks,
            errors=collection.errors,
            generated_at=self._now(),
            timezone=self.settings.timezone,
        )
        return await self.llm.chat(message, snapshot, history=history)

    async def _generate(
        self,
        mode: str,
        tasks: list[NormalizedTask],
        errors: list[Any],
        extra: dict[str, Any] | None = None,
    ) -> str:
        snapshot = AnalysisSnapshot(
            tasks=tasks,
            errors=errors,
            generated_at=self._now(),
            timezone=self.settings.timezone,
        )
        return await self.llm.generate(mode, snapshot, extra=extra)

    def _register_tools(self) -> None:
        self.runtime.register("today", "Return today's plan", self.today)
        self.runtime.register("briefing", "Return morning briefing", self.briefing)
        self.runtime.register("week", "Return weekly overview", self.week)
        self.runtime.register("review", "Return evening review", self.review)
        self.runtime.register("life", "Return personal TickTick overview", self.life)
        self.runtime.register("chat", "Answer a conversational message with task context", self.chat)

    def _pending_action(self, user_id: int, kind: str, summary: str, payload: dict[str, Any]) -> PendingAction:
        return PendingAction(
            kind=kind,
            user_id=user_id,
            summary=summary,
            payload=payload,
            expires_at=self._now() + timedelta(minutes=15),
        )

    def _now(self) -> datetime:
        return now_in_timezone(self.settings.timezone)

    @staticmethod
    def _has_single_strong_match(candidates: list[CandidateMatch]) -> bool:
        if not candidates or candidates[0].score < 80:
            return False
        if len(candidates) == 1:
            return True
        return candidates[0].score - candidates[1].score >= 12

    @staticmethod
    def _create_summary(payload: dict[str, Any]) -> str:
        deadline = payload.get("deadline")
        time = f" {payload['time']}" if payload.get("time") else ""
        destination = payload.get("destination") or payload["source"]
        return (
            "📝 Создать задачу\n\n"
            f"• Что: {payload['title']}\n"
            f"• Куда: {destination}\n"
            f"• Когда: {ProductivityService._display_date(deadline)}{time}"
        )

    @staticmethod
    def _change_summary(kind: str, task: dict[str, Any], payload: dict[str, Any]) -> str:
        task_model = NormalizedTask.model_validate(task)
        if kind == "complete_task":
            change = "отметить как выполненную"
        elif kind == "reschedule_task":
            old_deadline = task_model.deadline.strftime("%d.%m.%Y") if task_model.deadline else "без дедлайна"
            change = f"перенести с {old_deadline} на {ProductivityService._display_date(payload['deadline'])}"
        elif kind == "update_status":
            change = f"изменить статус на {payload['status']}"
        else:
            change = kind
        return (
            "🔎 Нашел задачу\n\n"
            f"1. {format_task(task_model)}\n\n"
            f"🔄 Изменение: {change}."
        )

    @staticmethod
    def _display_date(value: str | date | None) -> str:
        if not value:
            return "без дедлайна"
        parsed = date.fromisoformat(value) if isinstance(value, str) else value
        return parsed.strftime("%d.%m.%Y")
