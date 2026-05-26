from datetime import date, datetime

import pytest

from productivity_agent.config import Settings
from productivity_agent.models import CandidateMatch, NormalizedTask, TaskSource, TaskStatus
from productivity_agent.services import ProductivityService
from productivity_agent.storage import JsonStateStore


class FakeRepository:
    def __init__(self, task: NormalizedTask) -> None:
        self.task = task
        self.completed = 0

    async def find_candidates(self, query: str, limit: int = 5) -> list[CandidateMatch]:
        return [CandidateMatch(task=self.task, score=96)]

    async def complete_task(self, task: NormalizedTask) -> NormalizedTask:
        self.completed += 1
        return task.model_copy(update={"status": TaskStatus.DONE})


class FakeLLM:
    async def generate(self, *args, **kwargs) -> str:
        return "ok"


@pytest.mark.asyncio
async def test_done_requires_confirmation(tmp_path) -> None:
    settings = Settings(
        TELEGRAM_ALLOWED_USER_ID=123,
        TELEGRAM_BOT_TOKEN="token",
        APP_STATE_PATH=tmp_path / "state.json",
    )
    task = NormalizedTask(
        id="task-1",
        source=TaskSource.TICKTICK,
        title="Подготовить отчет",
        status=TaskStatus.TODO,
        deadline=date(2026, 5, 24),
        project_id="project-1",
        updated_time=datetime.fromisoformat("2026-05-20T12:00:00+00:00"),
    )
    repository = FakeRepository(task)
    service = ProductivityService(
        settings=settings,
        repository=repository,
        llm=FakeLLM(),
        state_store=JsonStateStore(tmp_path / "state.json"),
    )

    prompt = await service.start_done("/done Подготовить отчет", user_id=123)

    assert "Подтверди" in prompt
    assert repository.completed == 0

    result = await service.handle_pending_response("да", user_id=123)

    assert "Отметил как выполненную" in result
    assert repository.completed == 1
