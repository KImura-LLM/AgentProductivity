from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from productivity_agent.models import Energy, NormalizedTask, TaskPriority, TaskStatus


class ProductivityAnalyzer:
    def __init__(self, today: date | None = None) -> None:
        self.today = today or date.today()

    def score(self, task: NormalizedTask) -> int:
        score = 0
        if task.deadline:
            delta = (task.deadline - self.today).days
            if delta < 0:
                score += 100
            elif delta == 0:
                score += 90
            elif delta == 1:
                score += 65
            elif delta <= 7:
                score += 35
        else:
            score += 5

        if task.priority == TaskPriority.HIGH:
            score += 40
        elif task.priority == TaskPriority.MEDIUM:
            score += 20
        elif task.priority == TaskPriority.LOW:
            score += 5

        if task.status == TaskStatus.IN_PROGRESS:
            score += 25
        elif task.status == TaskStatus.WAITING:
            score -= 30

        if task.updated_time and task.updated_time.date() <= self.today - timedelta(days=14):
            score += 15
        if task.energy == Energy.HIGH:
            score -= 10
        if task.estimated_minutes and task.estimated_minutes <= 30:
            score += 8
        return score

    def key_tasks(self, tasks: list[NormalizedTask], limit: int = 5) -> list[NormalizedTask]:
        active = [task for task in tasks if not task.is_done and task.status != TaskStatus.WAITING]
        return sorted(active, key=self.score, reverse=True)[:limit]

    def urgent_tasks(self, tasks: list[NormalizedTask]) -> list[NormalizedTask]:
        return [
            task
            for task in sorted(tasks, key=self.score, reverse=True)
            if task.deadline and task.deadline <= self.today + timedelta(days=1) and not task.is_done
        ]

    def stale_tasks(self, tasks: list[NormalizedTask]) -> list[NormalizedTask]:
        return [
            task
            for task in tasks
            if not task.is_done
            and (
                task.status == TaskStatus.WAITING
                or (task.updated_time and task.updated_time.date() <= self.today - timedelta(days=14))
            )
        ]

    def no_deadline_tasks(self, tasks: list[NormalizedTask], limit: int = 8) -> list[NormalizedTask]:
        return [task for task in tasks if not task.is_done and task.deadline is None][:limit]

    def overloaded_days(self, tasks: list[NormalizedTask]) -> dict[date, list[NormalizedTask]]:
        grouped: dict[date, list[NormalizedTask]] = defaultdict(list)
        for task in tasks:
            if task.deadline and not task.is_done:
                grouped[task.deadline].append(task)
        return {day: day_tasks for day, day_tasks in grouped.items() if len(day_tasks) >= 7}

    def main_focus(self, tasks: list[NormalizedTask]) -> NormalizedTask | None:
        key = self.key_tasks(tasks, limit=1)
        return key[0] if key else None

    def compact_task_payload(self, tasks: list[NormalizedTask], limit: int = 80) -> list[dict[str, str | int | None]]:
        sorted_tasks = sorted(tasks, key=self.score, reverse=True)[:limit]
        return [
            {
                "title": task.title,
                "source": task.display_source,
                "project": task.project,
                "area": task.area,
                "status": task.status.value,
                "priority": task.priority.value if task.priority else None,
                "deadline": task.deadline.isoformat() if task.deadline else None,
                "time": task.time,
                "estimated_minutes": task.estimated_minutes,
                "energy": task.energy.value if task.energy else None,
                "urgency": self._urgency_label(task),
            }
            for task in sorted_tasks
        ]

    def source_summary(self, tasks: list[NormalizedTask]) -> dict[str, int]:
        summary = {"notion": 0, "ticktick": 0}
        for task in tasks:
            summary[task.source.value] = summary.get(task.source.value, 0) + 1
        return summary

    def deterministic_today(self, tasks: list[NormalizedTask]) -> str:
        focus = self.main_focus(tasks)
        urgent = self.urgent_tasks(tasks)[:5]
        key = self.key_tasks(tasks)[:5]
        project_tasks = [task for task in tasks if task.source.value == "notion" and not task.is_done][:5]
        life_tasks = [task for task in tasks if task.source.value == "ticktick" and not task.is_done][:5]
        no_deadline = self.no_deadline_tasks(tasks, limit=5)
        lines = ["📅 План на сегодня"]
        if focus:
            lines.append(f"🎯 Главный фокус\n• {format_task(focus)}")
        if urgent:
            lines.append("🔥 Срочно\n" + "\n".join(f"• {format_task(task)}" for task in urgent))
        if key:
            ordered = "\n".join(
                f"{idx}. {format_task(task)}" for idx, task in enumerate(key, start=1)
            )
            lines.append("🧭 Порядок действий\n" + ordered)
        if project_tasks:
            lines.append("💼 Проекты Notion\n" + "\n".join(f"• {format_task(task)}" for task in project_tasks))
        if life_tasks:
            lines.append("🏠 Личное TickTick\n" + "\n".join(f"• {format_task(task)}" for task in life_tasks))
        if no_deadline:
            lines.append("📝 Без дедлайна\n" + "\n".join(f"• {format_task(task)}" for task in no_deadline))
        if not tasks:
            lines.append("Задач в текущем контексте не найдено.")
        return "\n\n".join(lines)

    def _urgency_label(self, task: NormalizedTask) -> str:
        if not task.deadline:
            return "без дедлайна"
        delta = (task.deadline - self.today).days
        if delta < 0:
            return "просрочено"
        if delta == 0:
            return "сегодня"
        if delta == 1:
            return "завтра"
        if delta <= 7:
            return "на этой неделе"
        return "позже"


def format_task(task: NormalizedTask) -> str:
    bits = [task.title]
    if task.deadline:
        bits.append(f"до {task.deadline.strftime('%d.%m.%Y')}")
    if task.time:
        bits.append(task.time)
    if task.priority:
        bits.append(_priority_label(task.priority))
    bits.append(task.display_source)
    return " — ".join(bits)


def _priority_label(priority: TaskPriority) -> str:
    if priority == TaskPriority.HIGH:
        return "важно"
    if priority == TaskPriority.MEDIUM:
        return "средний приоритет"
    return "низкий приоритет"


def now_in_timezone(tz_name: str) -> datetime:
    from zoneinfo import ZoneInfo

    return datetime.now(tz=ZoneInfo(tz_name))
