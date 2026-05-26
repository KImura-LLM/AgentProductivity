from datetime import UTC, date, datetime, timedelta

from productivity_agent.analyzer import ProductivityAnalyzer
from productivity_agent.models import NormalizedTask, TaskPriority, TaskSource, TaskStatus


def task(title: str, deadline: date | None, priority: TaskPriority | None = None) -> NormalizedTask:
    return NormalizedTask(
        id=title,
        source=TaskSource.NOTION,
        title=title,
        status=TaskStatus.TODO,
        priority=priority,
        deadline=deadline,
        updated_time=datetime.now(UTC),
    )


def test_deadline_and_priority_drive_key_tasks() -> None:
    today = date(2026, 5, 24)
    analyzer = ProductivityAnalyzer(today=today)
    overdue = task("overdue", today - timedelta(days=1), TaskPriority.LOW)
    later_high = task("later high", today + timedelta(days=4), TaskPriority.HIGH)
    no_deadline = task("no deadline", None, None)

    assert analyzer.key_tasks([no_deadline, later_high, overdue], limit=1) == [overdue]
    assert analyzer.score(later_high) > analyzer.score(no_deadline)
