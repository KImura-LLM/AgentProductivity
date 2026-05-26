import json
from datetime import UTC, date, datetime

from productivity_agent.analyzer import ProductivityAnalyzer
from productivity_agent.config import Settings
from productivity_agent.llm import LLMGenerator, _clean_output
from productivity_agent.models import AnalysisSnapshot, NormalizedTask, TaskSource, TaskStatus


def test_llm_payload_hides_internal_fields() -> None:
    task = NormalizedTask(
        id="task-1",
        source=TaskSource.NOTION,
        title="Собрать план проекта",
        status=TaskStatus.TODO,
        deadline=date(2026, 5, 25),
    )
    snapshot = AnalysisSnapshot(
        tasks=[task],
        generated_at=datetime(2026, 5, 25, 10, 0, tzinfo=UTC),
        timezone="Europe/Moscow",
    )
    generator = LLMGenerator(Settings())

    payload = generator._payload(
        mode="today",
        snapshot=snapshot,
        analyzer=ProductivityAnalyzer(today=date(2026, 5, 25)),
    )
    encoded = json.dumps(payload, ensure_ascii=False)

    assert "generated_at" not in encoded
    assert "timezone" not in encoded
    assert "Europe/Moscow" not in encoded
    assert "source_errors" not in encoded
    assert "score" not in encoded
    assert payload["tasks"][0]["urgency"] == "сегодня"
    assert payload["source_summary"] == {"notion": 1, "ticktick": 0}


def test_clean_output_removes_leaked_technical_lines() -> None:
    text = (
        "📅 План\n"
        "generated_at: 2026-05-25T10:00:00\n"
        "Time zone: Europe/Moscow\n"
        "• Сделать важное\n"
        "score: 95"
    )

    assert _clean_output(text) == "📅 План\n• Сделать важное"
