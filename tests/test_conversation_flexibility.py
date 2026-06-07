import pytest

from productivity_agent.config import Settings
from productivity_agent.models import TaskCollection
from productivity_agent.parsing import parse_natural_command
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


def test_compound_reschedule_message_stays_conversational() -> None:
    message = (
        "Перенеси задачу про отчет на завтра, дай небольшие рекомендации по продуктивности "
        "и скажи, можешь ли ты вести систему эффективности и привычку раннего сна?"
    )

    assert parse_natural_command(message) is None


def test_short_reschedule_message_stays_actionable() -> None:
    assert parse_natural_command("Перенеси задачу отчет на завтра") == "reschedule"


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


@pytest.mark.asyncio
async def test_effectiveness_report_renders_chart(tmp_path) -> None:
    settings = Settings(APP_STATE_PATH=tmp_path / "state.json")
    service = ProductivityService(
        settings=settings,
        repository=EmptyRepository(),
        llm=FakeLLM(),
        state_store=JsonStateStore(tmp_path / "state.json"),
    )
    await service.record_effectiveness("закончил работу в 21:00")
    await service.record_effectiveness("начал отход ко сну в 22:30")
    await service.record_effectiveness("лег спать в 23:20")
    await service.record_effectiveness("проснулся в 07:30")
    await service.record_effectiveness("главный фокус выполнен")

    response = await service.effectiveness()

    assert "График" in response
    assert "sleep score" in response
    assert "100" in response


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

    assert response is not None
    assert len(entries) == 1
    assert entries[0].wind_down_time is not None


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
