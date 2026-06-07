from __future__ import annotations

import base64
from datetime import date, datetime, timedelta
from typing import Any

from productivity_agent.agent import AgentRuntime
from productivity_agent.analyzer import ProductivityAnalyzer, format_task, now_in_timezone
from productivity_agent.config import Settings
from productivity_agent.llm import LLMGenerator
from productivity_agent.models import (
    AnalysisSnapshot,
    CandidateMatch,
    EffectivenessEntry,
    NormalizedTask,
    PendingAction,
    TaskSource,
)
from productivity_agent.parsing import (
    ParsedEffectivenessText,
    parse_effectiveness_text,
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
        generated = await self._generate("briefing", collection.tasks, collection.errors)
        return (
            f"{generated}\n\n"
            "🌙 Сон\n"
            "Если еще не записал: ответь «уснул в 23:40» и «проснулся в 07:30». "
            "Если было отклонение от цели, я отдельно спрошу причину."
        )

    async def week(self) -> str:
        collection = await self.repository.collect()
        return await self._generate("week", collection.tasks, collection.errors)

    async def review(self) -> str:
        today = self._now().date()
        collection = await self.repository.collect(date_to=today, include_done=True)
        generated = await self._generate("review", collection.tasks, collection.errors)
        return (
            f"{generated}\n\n"
            "📈 Метрики дня\n"
            "Когда будет удобно, ответь одной строкой: «закончил работу в 21:10», "
            "«начал отход ко сну в 22:40», «пойду через 20 минут спать», "
            "«лег спать в 23:25» или «главный фокус выполнен»."
        )

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
            "📈 Эффективность\n"
            f"• Отбой: до {safe['target_sleep_time']}\n"
            f"• Подъем: около {safe['target_wake_time']}\n"
            f"• Отход ко сну: до {safe['wind_down_time']}\n"
            f"• Завершить работу: до {safe['work_shutdown_time']}\n\n"
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

        if action.kind == "record_sleep_reason" and parse_effectiveness_text(
            text,
            now=now,
            tzinfo=self.settings.tzinfo,
        ):
            return None

        if action.kind == "record_sleep_reason":
            result = self._record_sleep_reason(action, text.strip())
            self.state_store.clear_pending_action(user_id)
            return result

        if normalized in YES_VALUES:
            result = await self._execute_pending(action)
            self.state_store.clear_pending_action(user_id)
            return result

        return None

    async def record_effectiveness(self, raw_text: str) -> str | None:
        parsed = parse_effectiveness_text(raw_text, now=self._now(), tzinfo=self.settings.tzinfo)
        if not parsed:
            return None

        day = self._effectiveness_day(parsed)
        existing = self.state_store.get_effectiveness_entry(day.isoformat())
        entry = existing or EffectivenessEntry(day=day, updated_at=self._now())
        notes = [*entry.notes, parsed.note] if parsed.note else entry.notes
        entry = entry.model_copy(
            update={
                "sleep_time": parsed.sleep_time or entry.sleep_time,
                "wake_time": parsed.wake_time or entry.wake_time,
                "wind_down_time": parsed.wind_down_time or entry.wind_down_time,
                "work_finished_time": parsed.work_finished_time or entry.work_finished_time,
                "focus_done": parsed.focus_done if parsed.focus_done is not None else entry.focus_done,
                "notes": notes[-5:],
                "updated_at": self._now(),
            }
        )
        self.state_store.set_effectiveness_entry(entry)
        score = self._effectiveness_score(entry)
        sleep_score = self._sleep_score(entry)
        reason_prompt = self._maybe_ask_sleep_reason(entry)
        response = (
            "📈 Записал метрику эффективности\n\n"
            f"• День: {entry.day.strftime('%d.%m.%Y')}\n"
            f"• Сон: {entry.sleep_time or 'нет данных'} / цель до {self.settings.target_sleep_time}\n"
            f"• Подъем: {entry.wake_time or 'нет данных'} / цель около {self.settings.target_wake_time}\n"
            f"• Отход ко сну: {entry.wind_down_time or 'нет данных'} / цель до {self.settings.wind_down_time}\n"
            f"• Работа завершена: {entry.work_finished_time or 'нет данных'} / "
            f"цель до {self.settings.work_shutdown_time}\n"
            f"• Главный фокус: {self._focus_label(entry.focus_done)}\n"
            f"• Sleep score: {sleep_score}/100\n"
            f"• Балл дня: {score}/100\n\n"
            "Можешь писать так же: «закончил работу в 21:10», «начал отход ко сну в 22:40», "
            "«пойду через 20 минут спать», «лег спать в 23:25», "
            "«проснулся в 07:40», «главный фокус выполнен»."
        )
        if reason_prompt:
            response = f"{response}\n\n{reason_prompt}"
        return response

    async def effectiveness(self) -> str:
        entries = self.state_store.list_effectiveness_entries(limit=14)
        if not entries:
            return (
                "📈 Эффективность\n\n"
                "Пока нет записей. Я буду собирать минимум четыре сигнала: окончание работы, отход ко сну, "
                "фактический сон, подъем и выполнение главного фокуса.\n\n"
                "Примеры:\n"
                "• закончил работу в 21:10\n"
                "• начал отход ко сну в 22:40\n"
                "• лег спать в 23:25\n"
                "• проснулся в 07:40\n"
                "• главный фокус выполнен"
            )

        scores = [self._effectiveness_score(entry) for entry in entries]
        sleep_scores = [self._sleep_score(entry) for entry in entries]
        avg_score = round(sum(scores) / len(scores))
        avg_sleep_score = round(sum(sleep_scores) / len(sleep_scores))
        sleep_on_time = sum(
            1
            for entry in entries
            if entry.sleep_time and self._minutes_late(entry.sleep_time, self.settings.target_sleep_time) <= 0
        )
        wake_on_time = sum(
            1
            for entry in entries
            if entry.wake_time and abs(self._wake_minutes_late(entry.wake_time, self.settings.target_wake_time)) <= 30
        )
        chart = "\n".join(self._effectiveness_chart_line(entry) for entry in entries)
        latest = entries[-1]
        latest_reasons = self._latest_sleep_reasons(entries)
        return (
            "📈 Эффективность\n\n"
            f"Средний балл за {len(entries)} дн.: {avg_score}/100\n"
            f"Средний sleep score: {avg_sleep_score}/100\n"
            f"Сон вовремя: {sleep_on_time}/{len(entries)}\n"
            f"Подъем около цели: {wake_on_time}/{len(entries)}\n"
            f"Цели: работа до {self.settings.work_shutdown_time}, отход ко сну до {self.settings.wind_down_time}, "
            f"сон до {self.settings.target_sleep_time}, подъем около {self.settings.target_wake_time}.\n\n"
            "График:\n"
            f"{chart}\n\n"
            "Последняя запись:\n"
            f"• {latest.day.strftime('%d.%m')}: сон {latest.sleep_time or '-'}, "
            f"подъем {latest.wake_time or '-'}, "
            f"отход {latest.wind_down_time or '-'}, работа {latest.work_finished_time or '-'}, "
            f"фокус {self._focus_label(latest.focus_done)}."
            f"{latest_reasons}"
        )

    async def sleep_chart_image(self) -> bytes | None:
        entries = self.state_store.list_effectiveness_entries(limit=14)
        if not entries:
            return None
        rows = [
            {
                "day": entry.day.strftime("%d.%m"),
                "sleep_time": entry.sleep_time,
                "wake_time": entry.wake_time,
                "sleep_score": self._sleep_score(entry),
                "sleep_reason": entry.sleep_deviation_reason,
                "wake_reason": entry.wake_deviation_reason,
            }
            for entry in entries
        ]
        prompt = (
            "Create a clean Russian-language sleep analytics chart image for a Telegram bot. "
            "Use a dark readable dashboard style with a line or bar chart for sleep score, "
            "a compact table with dates, sleep time, wake time, and short reason labels. "
            "Do not include any API names, JSON, technical metadata, or model names. "
            f"Targets: sleep by {self.settings.target_sleep_time}, wake around {self.settings.target_wake_time}. "
            f"Data: {rows}"
        )
        data_url = await self.llm.generate_image(prompt)
        if not data_url:
            return None
        return self._decode_data_url(data_url)

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
        return await self.llm.chat(message, snapshot, history=history, effectiveness=self._effectiveness_context())

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
        self.runtime.register("effectiveness", "Return effectiveness and sleep habit report", self.effectiveness)
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

    def _effectiveness_day(self, parsed: ParsedEffectivenessText) -> date:
        now = self._now()
        if parsed.sleep_time and self._time_to_minutes(parsed.sleep_time) < 6 * 60 and now.hour < 12:
            return now.date() - timedelta(days=1)
        if parsed.wake_time and now.hour < 12:
            return now.date() - timedelta(days=1)
        return now.date()

    def _effectiveness_context(self) -> dict[str, Any]:
        entries = self.state_store.list_effectiveness_entries(limit=7)
        return {
            "targets": {
                "sleep_time": self.settings.target_sleep_time,
                "wake_time": self.settings.target_wake_time,
                "wind_down_time": self.settings.wind_down_time,
                "work_shutdown_time": self.settings.work_shutdown_time,
            },
            "recent_days": [
                {
                    "day": entry.day.isoformat(),
                    "score": self._effectiveness_score(entry),
                    "sleep_score": self._sleep_score(entry),
                    "sleep_time": entry.sleep_time,
                    "wake_time": entry.wake_time,
                    "wind_down_time": entry.wind_down_time,
                    "work_finished_time": entry.work_finished_time,
                    "focus_done": entry.focus_done,
                    "sleep_deviation_reason": entry.sleep_deviation_reason,
                    "wake_deviation_reason": entry.wake_deviation_reason,
                }
                for entry in entries
            ],
        }

    def _effectiveness_score(self, entry: EffectivenessEntry) -> int:
        score = 0
        if entry.sleep_time:
            late = self._minutes_late(entry.sleep_time, self.settings.target_sleep_time)
            if late <= 0:
                score += 45
            elif late <= 30:
                score += 35
            elif late <= 60:
                score += 25
            elif late <= 120:
                score += 10
        if entry.wind_down_time:
            late = self._minutes_late(entry.wind_down_time, self.settings.wind_down_time)
            if late <= 0:
                score += 25
            elif late <= 30:
                score += 18
            elif late <= 60:
                score += 10
        if entry.work_finished_time:
            late = self._minutes_late(entry.work_finished_time, self.settings.work_shutdown_time)
            if late <= 0:
                score += 20
            elif late <= 30:
                score += 14
            elif late <= 60:
                score += 8
        if entry.focus_done is True:
            score += 10
        return score

    def _effectiveness_chart_line(self, entry: EffectivenessEntry) -> str:
        score = self._effectiveness_score(entry)
        sleep_score = self._sleep_score(entry)
        filled = round(score / 10)
        bar = "█" * filled + "░" * (10 - filled)
        sleep_on_time = entry.sleep_time and self._minutes_late(entry.sleep_time, self.settings.target_sleep_time) <= 0
        sleep_status = "сон ✓" if sleep_on_time else "сон ×"
        return (
            f"{entry.day.strftime('%d.%m')} {bar} день {score:3d} "
            f"сон {sleep_score:3d} {sleep_status} "
            f"{entry.sleep_time or '--:--'} -> {entry.wake_time or '--:--'}"
        )

    def _sleep_score(self, entry: EffectivenessEntry) -> int:
        score = 0
        if entry.sleep_time:
            late = self._minutes_late(entry.sleep_time, self.settings.target_sleep_time)
            if late <= 0:
                score += 45
            elif late <= 30:
                score += 35
            elif late <= 60:
                score += 25
            elif late <= 120:
                score += 10
        if entry.wake_time:
            late = abs(self._wake_minutes_late(entry.wake_time, self.settings.target_wake_time))
            if late <= 15:
                score += 35
            elif late <= 30:
                score += 28
            elif late <= 60:
                score += 18
            elif late <= 120:
                score += 8
        if entry.wind_down_time:
            late = self._minutes_late(entry.wind_down_time, self.settings.wind_down_time)
            if late <= 0:
                score += 20
            elif late <= 30:
                score += 14
            elif late <= 60:
                score += 8
        return score

    def _maybe_ask_sleep_reason(self, entry: EffectivenessEntry) -> str | None:
        if entry.sleep_time and not entry.sleep_deviation_reason:
            late = self._minutes_late(entry.sleep_time, self.settings.target_sleep_time)
            if late > 15:
                return self._start_sleep_reason_prompt(entry, "sleep", f"лег позже цели на {late} мин")
            if late < -30:
                return self._start_sleep_reason_prompt(entry, "sleep", f"лег раньше цели на {abs(late)} мин")
        if entry.wake_time and not entry.wake_deviation_reason:
            late = self._wake_minutes_late(entry.wake_time, self.settings.target_wake_time)
            if late > 30:
                return self._start_sleep_reason_prompt(entry, "wake", f"проснулся позже цели на {late} мин")
            if late < -30:
                return self._start_sleep_reason_prompt(entry, "wake", f"проснулся раньше цели на {abs(late)} мин")
        return None

    def _start_sleep_reason_prompt(self, entry: EffectivenessEntry, reason_type: str, deviation: str) -> str:
        action = self._pending_action(
            user_id=self.settings.telegram_allowed_user_id or 0,
            kind="record_sleep_reason",
            summary="Записать причину отклонения сна.",
            payload={
                "day": entry.day.isoformat(),
                "reason_type": reason_type,
                "deviation": deviation,
            },
        )
        self.state_store.set_pending_action(action)
        return f"📝 {deviation}. Напиши одной фразой причину, я сохраню ее в статистику сна."

    def _record_sleep_reason(self, action: PendingAction, reason: str) -> str:
        if not reason:
            return "Причина пустая, ничего не записал."
        day = action.payload["day"]
        entry = self.state_store.get_effectiveness_entry(day)
        if not entry:
            return "Не нашел запись сна для этой причины. Повтори отметку времени."
        reason_type = action.payload.get("reason_type")
        field = "wake_deviation_reason" if reason_type == "wake" else "sleep_deviation_reason"
        updated = entry.model_copy(update={field: reason[:500], "updated_at": self._now()})
        self.state_store.set_effectiveness_entry(updated)
        return (
            "📝 Записал причину отклонения сна\n\n"
            f"• День: {updated.day.strftime('%d.%m.%Y')}\n"
            f"• Отклонение: {action.payload.get('deviation', 'от цели')}\n"
            f"• Причина: {reason[:500]}"
        )

    def _latest_sleep_reasons(self, entries: list[EffectivenessEntry]) -> str:
        reasons: list[str] = []
        for entry in reversed(entries):
            if entry.sleep_deviation_reason:
                reasons.append(f"• {entry.day.strftime('%d.%m')}: сон — {entry.sleep_deviation_reason}")
            if entry.wake_deviation_reason:
                reasons.append(f"• {entry.day.strftime('%d.%m')}: подъем — {entry.wake_deviation_reason}")
            if len(reasons) >= 3:
                break
        if not reasons:
            return ""
        return "\n\nПоследние причины отклонений:\n" + "\n".join(reasons[:3])

    @staticmethod
    def _decode_data_url(value: str) -> bytes | None:
        if not value.startswith("data:image/") or "," not in value:
            return None
        _, encoded = value.split(",", 1)
        try:
            return base64.b64decode(encoded, validate=True)
        except ValueError:
            return None

    def _minutes_late(self, value: str, target: str) -> int:
        actual = self._time_to_bedtime_minutes(value)
        planned = self._time_to_bedtime_minutes(target)
        return actual - planned

    def _wake_minutes_late(self, value: str, target: str) -> int:
        return self._time_to_minutes(value) - self._time_to_minutes(target)

    @staticmethod
    def _time_to_minutes(value: str) -> int:
        hour_s, minute_s = value.split(":", 1)
        return int(hour_s) * 60 + int(minute_s)

    def _time_to_bedtime_minutes(self, value: str) -> int:
        minutes = self._time_to_minutes(value)
        return minutes + 24 * 60 if minutes < 12 * 60 else minutes

    @staticmethod
    def _focus_label(value: bool | None) -> str:
        if value is True:
            return "выполнен"
        if value is False:
            return "не выполнен"
        return "нет данных"

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
