from __future__ import annotations

import httpx

from productivity_agent.config import Settings
from productivity_agent.connectors import NotionConnector, TickTickConnector
from productivity_agent.llm import LLMGenerator
from productivity_agent.repository import TaskRepository
from productivity_agent.services import ProductivityService
from productivity_agent.storage import JsonStateStore


def build_service(settings: Settings) -> ProductivityService:
    state_store = JsonStateStore(settings.app_state_path)
    http_client = httpx.AsyncClient(timeout=30)
    notion = NotionConnector(settings=settings, client=http_client)
    ticktick = TickTickConnector(settings=settings, state_store=state_store, client=http_client)
    repository = TaskRepository(notion=notion, ticktick=ticktick)
    llm = LLMGenerator(settings=settings)
    return ProductivityService(
        settings=settings,
        repository=repository,
        llm=llm,
        state_store=state_store,
    )
