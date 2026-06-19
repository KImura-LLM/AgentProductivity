from datetime import date, datetime

import pytest

from productivity_agent.config import Settings
from productivity_agent.models import PendingAction, TaskCollection
from productivity_agent.parsing import (
    ParsedEffectivenessText,
    parse_effectiveness_text,
    parse_natural_command,
    should_auto_record_effectiveness,
)
from productivity_agent.services import ProductivityService
from productivity_agent.storage import JsonStateStore


class EmptyRepository:
    async def collect(self, *args, **kwargs) -> TaskCollection:
        return TaskCollection()


class FakeLLM:
    async def generate(self, *args, **kwargs) -> str:
        return "ok"

    async def chat(self, *args, **kwargs) -> str:
        return "chat"


class ExtractingLLM(FakeLLM):
    def __init__(self, parsed: ParsedEffectivenessText) -> None:
        self.parsed = parsed
        self.calls = 0

    async def extract_effectiveness(self, *args, **kwargs) -> ParsedEffectivenessText:
        self.calls += 1
        return self.parsed


def test_compound_reschedule_message_stays_conversational() -> None:
    message = (
        "Перенеси задачу про отчет на завтра, дай небольшие рекомендации по продуктивности "
        "и скажи, можешь ли ты вести систему эффективности и привычку раннего сна?"
    )

    assert parse_natural_command(message) is None


def test_short_reschedule_message_stays_actionable() -> None:
    assert parse_natural_command("Перенеси задачу отчет на завтра") == "reschedule"


def test_short_sleep_metric_auto_records() -> None:
    settings = Settings()
    now = datetime(2026, 6, 7, 12, 0, tzinfo=settings.tzinfo)

    assert should_auto_record_effectiveness("лег спать в 22:10", now=now, tzinfo=settings.tzinfo)


def test_natural_sleep_and_wake_report_parses_separate_times() -> None:
    settings = Settings()
    now = datetime(2026, 6, 19, 12, 0, tzinfo=settings.tzinfo)

    parsed = parse_effectiveness_text(
        "вчера лег в час ночи, потому что засиделся с другом, и проснулся в 10 часов утра",
        now=now,
        tzinfo=settings.tzinfo,
    )

    assert parsed is not None
    assert parsed.day == datetime(2026, 6, 18, tzinfo=settings.tzinfo).date()
    assert parsed.sleep_time == "01:00"
    assert parsed.wake_time == "10:00"
    assert parsed.sleep_deviation_reason == "засиделся с другом"


def test_sleep_reason_without_because_marker_is_parsed() -> None:
    settings = Settings()
    now = datetime(2026, 6, 19, 12, 0, tzinfo=settings.tzinfo)

    parsed = parse_effectiveness_text(
        "вчера лег в час ночи, так засиделся с другом, проснулся в 9:51",
        now=now,
        tzinfo=settings.tzinfo,
    )

    assert parsed is not None
    assert parsed.sleep_time == "01:00"
    assert parsed.wake_time == "09:51"
    assert parsed.sleep_deviation_reason == "засиделся с другом"


def test_long_sleep_report_without_question_auto_records() -> None:
    settings = Settings()
    now = datetime(2026, 6, 19, 12, 0, tzinfo=settings.tzinfo)
    message = (
        "вчера лег в час ночи потому что засиделся с другом после долгого разговора "
        "и проснулся в 10 часов утра без будильника но чувствовал себя нормально"
    )

    assert should_auto_record_effectiveness(message, now=now, tzinfo=settings.tzinfo)


def test_compound_sleep_message_stays_conversational() -> None:
    settings = Settings()
    now = datetime(2026, 6, 7, 12, 0, tzinfo=settings.tzinfo)
    message = (
        "Я лег спать в 01:10, проснулся в 08:20, потому что долго не мог остановить работу, "
        "можешь разобрать почему так произошло и что поменять завтра?"
    )

    assert not should_auto_record_effectiveness(message, now=now, tzinfo=settings.tzinfo)


@pytest.mark.asyncio
async def test_effectiveness_entry_is_recorded(tmp_path) -> None:
    settings = Settings(APP_STATE_PATH=tmp_path / "state.json")
    store = JsonStateStore(tmp_path / "state.json")
    service = ProductivityService(
        settings=settings,
        repository=EmptyRepository(),
        llm=FakeLLM(),
        state_store=store,
    )

    response = await service.record_effectiveness("лег спать в 23:20")
    entries = store.list_effectiveness_entries()

    assert response is not None
    assert len(entries) == 1
    entry = entries[0]
    assert entry is not None
    assert entry.sleep_time == "23:20"
    assert "Балл дня" in response
    assert "Нарушение режима сна" in response
    assert "Отход ко сну" not in response
    assert "Работа завершена" not in response
    assert "Главный фокус" not in response


@pytest.mark.asyncio
async def test_effectiveness_report_renders_chart(tmp_path) -> None:
    settings = Settings(APP_STATE_PATH=tmp_path / "state.json")
    service = ProductivityService(
        settings=settings,
        repository=EmptyRepository(),
        llm=FakeLLM(),
        state_store=JsonStateStore(tmp_path / "state.json"),
    )
    await service.record_effectiveness("лег спать в 21:50")
    await service.record_effectiveness("проснулся в 11:00")

    response = await service.effectiveness()

    assert "График" in response
    assert "sleep score" in response
    assert "100" in response
    assert "работа" not in response.lower()
    assert "фокус" not in response.lower()


@pytest.mark.asyncio
async def test_wake_time_is_recorded(tmp_path) -> None:
    settings = Settings(APP_STATE_PATH=tmp_path / "state.json")
    store = JsonStateStore(tmp_path / "state.json")
    service = ProductivityService(
        settings=settings,
        repository=EmptyRepository(),
        llm=FakeLLM(),
        state_store=store,
    )

    response = await service.record_effectiveness("проснулся в 07:40")
    entries = store.list_effectiveness_entries()

    assert response is not None
    assert len(entries) == 1
    assert entries[0].wake_time == "07:40"
    assert "Sleep score" in response


@pytest.mark.asyncio
async def test_relative_wind_down_time_is_recorded(tmp_path) -> None:
    settings = Settings(APP_STATE_PATH=tmp_path / "state.json")
    store = JsonStateStore(tmp_path / "state.json")
    service = ProductivityService(
        settings=settings,
        repository=EmptyRepository(),
        llm=FakeLLM(),
        state_store=store,
    )

    response = await service.record_effectiveness("пойду через 20 минут спать")
    entries = store.list_effectiveness_entries()

    assert response is None
    assert entries == []


@pytest.mark.asyncio
async def test_late_wake_does_not_penalize_sleep_score(tmp_path) -> None:
    settings = Settings(APP_STATE_PATH=tmp_path / "state.json")
    store = JsonStateStore(tmp_path / "state.json")
    service = ProductivityService(
        settings=settings,
        repository=EmptyRepository(),
        llm=FakeLLM(),
        state_store=store,
    )

    response = await service.record_effectiveness("лег спать в 21:50, проснулся в 11:00")

    assert response is not None
    assert "Sleep score: 100/100" in response
    assert "Балл дня: 100/100" in response
    assert "Нарушение режима сна: нет" in response


@pytest.mark.asyncio
async def test_llm_extraction_fills_sleep_words_and_reason(tmp_path) -> None:
    settings = Settings(APP_STATE_PATH=tmp_path / "state.json")
    store = JsonStateStore(tmp_path / "state.json")
    llm = ExtractingLLM(
        ParsedEffectivenessText(
            day=date(2026, 6, 18),
            sleep_time="01:00",
            wake_time="09:51",
            sleep_deviation_reason="засиделся с другом",
            note="вчера лег сильно за полночь, так засиделся с другом, проснулся в 9:51",
        )
    )
    service = ProductivityService(
        settings=settings,
        repository=EmptyRepository(),
        llm=llm,
        state_store=store,
    )

    response = await service.record_effectiveness(
        "вчера лег сильно за полночь, так засиделся с другом, проснулся в 9:51"
    )
    entries = store.list_effectiveness_entries()

    assert llm.calls == 1
    assert response is not None
    assert "Нарушение режима сна: засиделся с другом" in response
    assert entries[0].sleep_time == "01:00"
    assert entries[0].wake_time == "09:51"
    assert entries[0].sleep_deviation_reason == "засиделся с другом"


@pytest.mark.asyncio
async def test_sleep_deviation_reason_is_saved(tmp_path) -> None:
    settings = Settings(
        TELEGRAM_ALLOWED_USER_ID=123,
        TELEGRAM_BOT_TOKEN="token",
        APP_STATE_PATH=tmp_path / "state.json",
    )
    store = JsonStateStore(tmp_path / "state.json")
    service = ProductivityService(
        settings=settings,
        repository=EmptyRepository(),
        llm=FakeLLM(),
        state_store=store,
    )

    response = await service.record_effectiveness("лег спать в 01:10")
    reason_response = await service.handle_pending_response("долго сидел с ноутбуком", user_id=123)
    entries = store.list_effectiveness_entries()

    assert response is not None
    assert "Напиши одной фразой причину" in response
    assert reason_response is not None
    assert "Записал причину" in reason_response
    assert entries[0].sleep_deviation_reason == "долго сидел с ноутбуком"


@pytest.mark.asyncio
async def test_expired_pending_does_not_block_new_sleep_report(tmp_path) -> None:
    settings = Settings(
        TELEGRAM_ALLOWED_USER_ID=123,
        TELEGRAM_BOT_TOKEN="token",
        APP_STATE_PATH=tmp_path / "state.json",
    )
    store = JsonStateStore(tmp_path / "state.json")
    service = ProductivityService(
        settings=settings,
        repository=EmptyRepository(),
        llm=FakeLLM(),
        state_store=store,
    )
    store.set_pending_action(
        PendingAction(
            kind="create_task",
            user_id=123,
            summary="Создать задачу",
            payload={},
            expires_at=datetime(2026, 1, 1, tzinfo=settings.tzinfo),
        )
    )

    text = "вчера лег в час ночи, потому что засиделся с другом, и проснулся в 10 часов утра"
    pending = await service.handle_pending_response(text, user_id=123)
    response = await service.record_effectiveness(text)
    entries = store.list_effectiveness_entries()

    assert pending is None
    assert response is not None
    assert "Напиши одной фразой причину" not in response
    assert entries[0].sleep_time == "01:00"
    assert entries[0].wake_time == "10:00"
    assert entries[0].sleep_deviation_reason == "засиделся с другом"
