from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

import httpx

from productivity_agent.config import NotionDatabaseConfig, Settings
from productivity_agent.connectors.base import SourceUnavailable
from productivity_agent.models import Energy, NormalizedTask, TaskPriority, TaskSource, TaskStatus

logger = logging.getLogger(__name__)


class NotionConnector:
    base_url = "https://api.notion.com/v1"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=30)
        self._schema_cache: dict[str, dict[str, str]] = {}

    def configured(self) -> bool:
        return bool(self.settings.notion_token and self.settings.notion_databases)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.notion_token}",
            "Content-Type": "application/json",
            "Notion-Version": self.settings.notion_version,
        }

    async def list_tasks(
        self,
        date_from: date | None = None,
        date_to: date | None = None,
        project: str | None = None,
        include_done: bool = False,
    ) -> list[NormalizedTask]:
        if not self.configured():
            return []

        tasks: list[NormalizedTask] = []
        for database in self.settings.notion_databases:
            if database.type != "tasks":
                continue
            pages = await self._query_database(database)
            for page in pages:
                task = self._normalize_page(database, page)
                if not include_done and task.is_done:
                    continue
                if project and task.project and project.lower() not in task.project.lower():
                    if project.lower() not in task.title.lower():
                        continue
                if date_from or date_to:
                    if task.deadline is None:
                        continue
                    if date_from and task.deadline < date_from:
                        continue
                    if date_to and task.deadline > date_to:
                        continue
                tasks.append(task)
        return tasks

    async def retrieve_database_data_sources(self, database_id: str) -> list[dict[str, str]]:
        response = await self._request("GET", f"/databases/{database_id}")
        body = response.json()
        data_sources = body.get("data_sources", [])
        return [
            {
                "id": item.get("id", ""),
                "name": item.get("name", ""),
            }
            for item in data_sources
            if isinstance(item, dict)
        ]

    async def retrieve_data_source_properties(self, data_source_id: str) -> dict[str, str]:
        response = await self._request("GET", f"/data_sources/{data_source_id}")
        properties = response.json().get("properties", {})
        return {
            name: raw.get("type", "unknown")
            for name, raw in properties.items()
            if isinstance(raw, dict)
        }

    async def create_task(
        self,
        title: str,
        deadline: date | None = None,
        time: str | None = None,
        project: str | None = None,
        destination: str | None = None,
        notes: str | None = None,
    ) -> NormalizedTask:
        database = self._resolve_database(destination)
        schema = await self._schema(database)
        properties = self._properties_for_create(database, schema, title, deadline, project, notes)
        parent = {"type": database.parent_type, database.parent_type: database.notion_id}
        response = await self._request("POST", "/pages", json={"parent": parent, "properties": properties})
        return self._normalize_page(database, response.json())

    async def complete_task(self, task: NormalizedTask) -> NormalizedTask:
        database = self._database_for_task(task)
        return await self.update_status(task, database.done_status)

    async def reschedule_task(self, task: NormalizedTask, deadline: date) -> NormalizedTask:
        database = self._database_for_task(task)
        field = database.fields_map.get("deadline", "Deadline")
        await self._patch_page(
            task.id,
            {field: {"date": {"start": deadline.isoformat()}}},
        )
        refreshed = task.model_copy(update={"deadline": deadline})
        return refreshed

    async def update_status(self, task: NormalizedTask, status: str) -> NormalizedTask:
        database = self._database_for_task(task)
        schema = await self._schema(database)
        field = database.fields_map.get("status", "Status")
        prop_type = schema.get(field, "select")
        value = {"status": {"name": status}} if prop_type == "status" else {"select": {"name": status}}
        await self._patch_page(task.id, {field: value})
        return task.model_copy(update={"status": _status_from_name(status), "metadata": task.metadata})

    async def _query_database(self, database: NotionDatabaseConfig) -> list[dict[str, Any]]:
        cursor: str | None = None
        pages: list[dict[str, Any]] = []
        endpoint = (
            f"/data_sources/{database.notion_id}/query"
            if database.data_source_id
            else f"/databases/{database.notion_id}/query"
        )

        while True:
            payload: dict[str, Any] = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            try:
                response = await self._request("POST", endpoint, json=payload)
            except SourceUnavailable:
                if endpoint.startswith("/data_sources/"):
                    legacy = f"/databases/{database.notion_id}/query"
                    response = await self._request("POST", legacy, json=payload)
                else:
                    raise
            body = response.json()
            pages.extend(body.get("results", []))
            cursor = body.get("next_cursor")
            if not body.get("has_more") or not cursor:
                return pages

    async def _schema(self, database: NotionDatabaseConfig) -> dict[str, str]:
        if database.name in self._schema_cache:
            return self._schema_cache[database.name]

        endpoints = []
        if database.data_source_id:
            endpoints.append(f"/data_sources/{database.data_source_id}")
        if database.database_id:
            endpoints.append(f"/databases/{database.database_id}")

        for endpoint in endpoints:
            try:
                response = await self._request("GET", endpoint)
                raw_props = response.json().get("properties", {})
                schema = {
                    name: raw.get("type", "rich_text")
                    for name, raw in raw_props.items()
                    if isinstance(raw, dict)
                }
                self._schema_cache[database.name] = schema
                return schema
            except SourceUnavailable:
                continue
        return {}

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if not self.settings.notion_token:
            raise SourceUnavailable("notion", "Notion token is not configured")
        try:
            response = await self.client.request(
                method,
                f"{self.base_url}{path}",
                headers=self._headers(),
                **kwargs,
            )
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            detail = _safe_error_detail(exc.response)
            raise SourceUnavailable("notion", f"Notion API error: {detail}") from exc
        except httpx.HTTPError as exc:
            raise SourceUnavailable("notion", f"Notion API is unavailable: {exc}") from exc

    async def _patch_page(self, page_id: str, properties: dict[str, Any]) -> None:
        await self._request("PATCH", f"/pages/{page_id}", json={"properties": properties})

    def _resolve_database(self, destination: str | None) -> NotionDatabaseConfig:
        databases = [db for db in self.settings.notion_databases if db.type == "tasks"]
        if not databases:
            raise SourceUnavailable("notion", "No Notion task databases configured")
        if destination:
            lowered = destination.lower()
            exact = [db for db in databases if db.name.lower() == lowered or db.notion_id == destination]
            if exact:
                return exact[0]
            partial = [db for db in databases if lowered in db.name.lower()]
            if len(partial) == 1:
                return partial[0]
        if len(databases) == 1:
            return databases[0]
        raise SourceUnavailable("notion", "Notion destination database is ambiguous")

    def _database_for_task(self, task: NormalizedTask) -> NotionDatabaseConfig:
        database_name = task.metadata.get("database_name")
        database_id = task.metadata.get("database_id")
        for database in self.settings.notion_databases:
            if database.name == database_name or database.notion_id == database_id:
                return database
        raise SourceUnavailable("notion", "Notion database for task is unknown")

    def _properties_for_create(
        self,
        database: NotionDatabaseConfig,
        schema: dict[str, str],
        title: str,
        deadline: date | None,
        project: str | None,
        notes: str | None,
    ) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        fields = database.fields_map
        title_field = fields.get("title", "Name")
        properties[title_field] = {"title": [{"type": "text", "text": {"content": title}}]}
        self._maybe_set_select(properties, schema, fields.get("status"), database.todo_status)
        if deadline and fields.get("deadline"):
            properties[fields["deadline"]] = {"date": {"start": deadline.isoformat()}}
        self._maybe_set_select(properties, schema, fields.get("project"), project)
        self._maybe_set_text(properties, schema, fields.get("notes"), notes)
        return properties

    @staticmethod
    def _maybe_set_select(
        properties: dict[str, Any], schema: dict[str, str], field: str | None, value: str | None
    ) -> None:
        if not field or not value:
            return
        prop_type = schema.get(field, "select")
        if prop_type == "status":
            properties[field] = {"status": {"name": value}}
        elif prop_type == "multi_select":
            properties[field] = {"multi_select": [{"name": value}]}
        elif prop_type == "rich_text":
            properties[field] = {"rich_text": [{"type": "text", "text": {"content": value}}]}
        else:
            properties[field] = {"select": {"name": value}}

    @staticmethod
    def _maybe_set_text(
        properties: dict[str, Any], schema: dict[str, str], field: str | None, value: str | None
    ) -> None:
        if not field or not value:
            return
        if schema.get(field) == "title":
            properties[field] = {"title": [{"type": "text", "text": {"content": value}}]}
        else:
            properties[field] = {"rich_text": [{"type": "text", "text": {"content": value}}]}

    def _normalize_page(self, database: NotionDatabaseConfig, page: dict[str, Any]) -> NormalizedTask:
        properties = page.get("properties", {})
        fields = database.fields_map
        title = _extract_text(properties.get(fields.get("title", "Name"))) or "Без названия"
        status_name = _extract_option(properties.get(fields.get("status", "Status")))
        priority_name = _extract_option(properties.get(fields.get("priority", "Priority")))
        deadline_value, time_value = _extract_date(properties.get(fields.get("deadline", "Deadline")))
        energy_name = _extract_option(properties.get(fields.get("energy", "Energy")))
        estimated = _extract_number(properties.get(fields.get("estimated_minutes", "Estimated time")))
        project = _extract_text_or_option(properties.get(fields.get("project", "Project")))
        notes = _extract_text(properties.get(fields.get("notes", "Notes")))
        field_types = {
            name: prop.get("type", "unknown")
            for name, prop in properties.items()
            if isinstance(prop, dict)
        }
        return NormalizedTask(
            id=page["id"],
            source=TaskSource.NOTION,
            title=title,
            project=project,
            area=database.area,
            status=_status_from_name(status_name, database),
            priority=_priority_from_name(priority_name),
            deadline=deadline_value,
            time=time_value,
            estimated_minutes=estimated,
            energy=_energy_from_name(energy_name),
            notes=notes,
            url=page.get("url"),
            source_name=database.name,
            source_id=page["id"],
            created_time=_parse_datetime(page.get("created_time")),
            updated_time=_parse_datetime(page.get("last_edited_time")),
            metadata={
                "database_name": database.name,
                "database_id": database.notion_id,
                "field_types": field_types,
            },
        )


def _safe_error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
        return str(body.get("message") or body)
    except ValueError:
        return response.text[:300]


def _extract_text(prop: dict[str, Any] | None) -> str | None:
    if not prop:
        return None
    prop_type = prop.get("type")
    values = prop.get(prop_type, [])
    if isinstance(values, list):
        text = "".join(item.get("plain_text", "") for item in values if isinstance(item, dict))
        return text.strip() or None
    if isinstance(values, dict):
        return values.get("name") or values.get("id")
    return None


def _extract_option(prop: dict[str, Any] | None) -> str | None:
    if not prop:
        return None
    prop_type = prop.get("type")
    value = prop.get(prop_type)
    if isinstance(value, dict):
        return value.get("name")
    if isinstance(value, list):
        names = [item.get("name") for item in value if isinstance(item, dict) and item.get("name")]
        return ", ".join(names) if names else None
    return None


def _extract_text_or_option(prop: dict[str, Any] | None) -> str | None:
    return _extract_option(prop) or _extract_text(prop)


def _extract_date(prop: dict[str, Any] | None) -> tuple[date | None, str | None]:
    if not prop:
        return None, None
    value = prop.get("date")
    if not isinstance(value, dict) or not value.get("start"):
        return None, None
    start = value["start"]
    if "T" in start:
        parsed = datetime.fromisoformat(start.replace("Z", "+00:00"))
        return parsed.date(), parsed.strftime("%H:%M")
    return date.fromisoformat(start), None


def _extract_number(prop: dict[str, Any] | None) -> int | None:
    if not prop:
        return None
    value = prop.get(prop.get("type", "number"))
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, dict) and value.get("name"):
        digits = "".join(ch for ch in value["name"] if ch.isdigit())
        return int(digits) if digits else None
    return None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _status_from_name(name: str | None, database: NotionDatabaseConfig | None = None) -> TaskStatus:
    if not name:
        return TaskStatus.TODO
    lowered = name.strip().lower()
    done = {database.done_status.lower()} if database else {"done", "archived", "completed"}
    waiting = {database.waiting_status.lower()} if database else {"waiting"}
    in_progress = {database.in_progress_status.lower()} if database else {"in progress", "doing"}
    if lowered in done or lowered in {"done", "archived", "completed", "готово", "сделано"}:
        return TaskStatus.DONE
    if lowered in waiting or lowered in {"waiting", "blocked", "ожидание"}:
        return TaskStatus.WAITING
    if lowered in in_progress or lowered in {"in progress", "doing", "в работе"}:
        return TaskStatus.IN_PROGRESS
    return TaskStatus.TODO


def _priority_from_name(name: str | None) -> TaskPriority | None:
    if not name:
        return None
    lowered = name.strip().lower()
    if lowered in {"high", "p1", "urgent", "высокий", "важно"}:
        return TaskPriority.HIGH
    if lowered in {"medium", "normal", "p2", "средний"}:
        return TaskPriority.MEDIUM
    if lowered in {"low", "p3", "низкий"}:
        return TaskPriority.LOW
    return None


def _energy_from_name(name: str | None) -> Energy | None:
    if not name:
        return None
    lowered = name.strip().lower()
    if lowered in {"high", "высокая"}:
        return Energy.HIGH
    if lowered in {"medium", "средняя"}:
        return Energy.MEDIUM
    if lowered in {"low", "низкая"}:
        return Energy.LOW
    return None
