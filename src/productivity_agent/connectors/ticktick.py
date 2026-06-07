from __future__ import annotations

import logging
import secrets
from datetime import date, datetime, timedelta, tzinfo
from datetime import time as dt_time
from typing import Any
from urllib.parse import urlencode

import httpx

from productivity_agent.config import Settings
from productivity_agent.connectors.base import SourceUnavailable
from productivity_agent.models import NormalizedTask, TaskPriority, TaskSource, TaskStatus
from productivity_agent.storage import JsonStateStore

logger = logging.getLogger(__name__)


class TickTickConnector:
    base_url = "https://api.ticktick.com/open/v1"
    auth_url_base = "https://ticktick.com/oauth/authorize"
    token_url = "https://ticktick.com/oauth/token"

    def __init__(
        self,
        settings: Settings,
        state_store: JsonStateStore,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.state_store = state_store
        self.client = client or httpx.AsyncClient(timeout=30)

    def configured(self) -> bool:
        tokens = self.state_store.get_ticktick_tokens()
        return bool(
            self.settings.ticktick_access_token
            or self.settings.ticktick_refresh_token
            or tokens.get("access_token")
            or tokens.get("refresh_token")
        )

    def build_authorization_url(
        self,
        *,
        include_redirect_uri: bool = True,
        include_state: bool = True,
        scope: str = "tasks:write tasks:read",
    ) -> str:
        params = {
            "client_id": self.settings.ticktick_client_id,
            "scope": scope,
            "response_type": "code",
        }
        if include_state:
            params["state"] = secrets.token_urlsafe(16)
        if include_redirect_uri:
            params["redirect_uri"] = self.settings.ticktick_redirect_uri
        return f"{self.auth_url_base}?{urlencode(params)}"

    async def exchange_code(self, code: str) -> dict[str, Any]:
        if not self.settings.ticktick_client_id or not self.settings.ticktick_client_secret:
            raise SourceUnavailable("ticktick", "TickTick client credentials are not configured")
        response = await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.settings.ticktick_redirect_uri,
            }
        )
        response.raise_for_status()
        tokens = self._with_expiry(response.json())
        self.state_store.set_ticktick_tokens(tokens)
        return tokens

    async def list_tasks(
        self,
        date_from: date | None = None,
        date_to: date | None = None,
        project: str | None = None,
        include_done: bool = False,
    ) -> list[NormalizedTask]:
        if not self.configured():
            return []
        projects = await self._get_projects()
        tasks: list[NormalizedTask] = []
        for ticktick_project in projects:
            project_id = ticktick_project["id"]
            project_name = ticktick_project.get("name") or ticktick_project.get("title") or "Inbox"
            if project and project.lower() not in project_name.lower():
                continue
            data = await self._request("GET", f"/project/{project_id}/data")
            for raw in data.json().get("tasks", []):
                task = self._normalize_task(raw, project_id=project_id, project_name=project_name)
                if not include_done and task.is_done:
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

    async def create_task(
        self,
        title: str,
        deadline: date | None = None,
        time: str | None = None,
        project: str | None = None,
        destination: str | None = None,
        notes: str | None = None,
    ) -> NormalizedTask:
        project_id, project_name = await self._resolve_project(destination or project)
        payload: dict[str, Any] = {
            "title": title,
            "content": notes or "",
        }
        if project_id:
            payload["projectId"] = project_id
        if deadline:
            payload.update(self._task_date_payload(deadline, time))
        response = await self._request("POST", "/task", json=payload)
        raw = response.json()
        return self._normalize_task(raw, project_id=raw.get("projectId") or project_id, project_name=project_name)

    async def complete_task(self, task: NormalizedTask) -> NormalizedTask:
        project_id = task.project_id or task.metadata.get("project_id")
        if not project_id:
            raise SourceUnavailable("ticktick", "TickTick project id is required to complete task")
        await self._request("POST", f"/project/{project_id}/task/{task.id}/complete")
        return task.model_copy(update={"status": TaskStatus.DONE})

    async def reschedule_task(self, task: NormalizedTask, deadline: date) -> NormalizedTask:
        project_id = task.project_id or task.metadata.get("project_id")
        if not project_id:
            raise SourceUnavailable("ticktick", "TickTick project id is required to update task")
        payload = {
            "id": task.id,
            "projectId": project_id,
            "title": task.title,
            "content": task.notes or "",
            **self._task_date_payload(deadline, task.time),
        }
        await self._request("POST", f"/task/{task.id}", json=payload)
        return task.model_copy(update={"deadline": deadline})

    async def update_status(self, task: NormalizedTask, status: str) -> NormalizedTask:
        if status.strip().lower() in {"done", "completed", "готово", "сделано"}:
            return await self.complete_task(task)
        raise SourceUnavailable("ticktick", "TickTick Open API supports completion, not arbitrary statuses")

    async def _get_projects(self) -> list[dict[str, Any]]:
        response = await self._request("GET", "/project")
        body = response.json()
        if isinstance(body, list):
            return body
        return body.get("projects", [])

    async def _resolve_project(self, destination: str | None) -> tuple[str | None, str | None]:
        if not destination:
            return None, "Inbox"
        projects = await self._get_projects()
        lowered = destination.lower()
        for project in projects:
            name = project.get("name") or project.get("title") or ""
            if project.get("id") == destination or name.lower() == lowered:
                return project["id"], name
        for project in projects:
            name = project.get("name") or project.get("title") or ""
            if lowered in name.lower():
                return project["id"], name
        raise SourceUnavailable("ticktick", f"TickTick project not found: {destination}")

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        token = await self._access_token()
        try:
            response = await self.client.request(
                method,
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {token}"},
                **kwargs,
            )
            if response.status_code == 401 and await self._refresh_token():
                token = await self._access_token()
                response = await self.client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers={"Authorization": f"Bearer {token}"},
                    **kwargs,
                )
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            raise SourceUnavailable("ticktick", f"TickTick API error: {_safe_error_detail(exc.response)}") from exc
        except httpx.HTTPError as exc:
            raise SourceUnavailable("ticktick", f"TickTick API is unavailable: {exc}") from exc

    async def _access_token(self) -> str:
        if self.settings.ticktick_access_token:
            return self.settings.ticktick_access_token
        tokens = self.state_store.get_ticktick_tokens()
        access_token = tokens.get("access_token")
        expires_at = _parse_datetime(tokens.get("expires_at"))
        if access_token and (not expires_at or expires_at > datetime.now().astimezone()):
            return access_token
        if await self._refresh_token():
            tokens = self.state_store.get_ticktick_tokens()
            if tokens.get("access_token"):
                return tokens["access_token"]
        raise SourceUnavailable("ticktick", "TickTick access token is not configured")

    async def _refresh_token(self) -> bool:
        refresh_token = (
            self.settings.ticktick_refresh_token
            or self.state_store.get_ticktick_tokens().get("refresh_token")
        )
        if not refresh_token:
            return False
        if not self.settings.ticktick_client_id or not self.settings.ticktick_client_secret:
            return False
        response = await self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )
        if response.status_code >= 400:
            logger.warning("TickTick token refresh failed: %s", response.status_code)
            return False
        tokens = self._with_expiry(response.json())
        if "refresh_token" not in tokens:
            tokens["refresh_token"] = refresh_token
        self.state_store.set_ticktick_tokens(tokens)
        return True

    async def _token_request(self, data: dict[str, Any]) -> httpx.Response:
        payload = {
            **data,
            "client_id": self.settings.ticktick_client_id,
            "client_secret": self.settings.ticktick_client_secret,
        }
        response = await self.client.post(
            self.token_url,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code < 400:
            return response
        return await self.client.post(
            self.token_url,
            data=data,
            auth=(self.settings.ticktick_client_id, self.settings.ticktick_client_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    def _with_expiry(self, tokens: dict[str, Any]) -> dict[str, Any]:
        expires_in = int(tokens.get("expires_in") or 3600)
        tokens["expires_at"] = (datetime.now().astimezone() + timedelta(seconds=expires_in - 60)).isoformat()
        return tokens

    def _normalize_task(
        self,
        raw: dict[str, Any],
        project_id: str | None,
        project_name: str | None,
    ) -> NormalizedTask:
        due_date, due_time = _parse_due_date(
            raw.get("dueDate"),
            self.settings.tzinfo,
            all_day=bool(raw.get("isAllDay")),
        )
        status = TaskStatus.DONE if raw.get("status") == 2 else TaskStatus.TODO
        task_id = raw.get("id")
        return NormalizedTask(
            id=task_id,
            source=TaskSource.TICKTICK,
            title=raw.get("title") or "Без названия",
            project=project_name,
            area="life",
            status=status,
            priority=_priority_from_ticktick(raw.get("priority")),
            deadline=due_date,
            time=due_time,
            notes=raw.get("content") or raw.get("desc"),
            source_name=project_name,
            source_id=task_id,
            project_id=project_id or raw.get("projectId"),
            metadata={
                "project_id": project_id or raw.get("projectId"),
                "raw_status": raw.get("status"),
            },
        )

    def _format_due_date(self, deadline: date, time_value: str | None) -> str:
        hour = 0
        minute = 0
        if time_value:
            hour_s, minute_s = time_value.split(":", 1)
            hour = int(hour_s)
            minute = int(minute_s)
        value = datetime.combine(deadline, dt_time(hour, minute), tzinfo=self.settings.tzinfo)
        return value.strftime("%Y-%m-%dT%H:%M:%S%z")

    def _task_date_payload(self, deadline: date, time_value: str | None) -> dict[str, Any]:
        scheduled_at = self._format_due_date(deadline, time_value)
        return {
            "startDate": scheduled_at,
            "dueDate": scheduled_at,
            "isAllDay": not time_value,
            "timeZone": self.settings.timezone,
        }


def _parse_due_date(
    value: str | None,
    display_tz: tzinfo | None = None,
    *,
    all_day: bool = False,
) -> tuple[date | None, str | None]:
    if not value:
        return None, None
    normalized = value.replace("Z", "+00:00")
    if "T" not in normalized:
        return date.fromisoformat(normalized), None
    if len(normalized) >= 5 and normalized[-5] in {"+", "-"} and normalized[-3] != ":":
        normalized = f"{normalized[:-2]}:{normalized[-2:]}"
    parsed = datetime.fromisoformat(normalized)
    if all_day:
        return parsed.date(), None
    if parsed.tzinfo and display_tz:
        parsed = parsed.astimezone(display_tz)
    return parsed.date(), parsed.strftime("%H:%M") if parsed.hour or parsed.minute else None


def _priority_from_ticktick(value: Any) -> TaskPriority | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number >= 5:
        return TaskPriority.HIGH
    if number >= 3:
        return TaskPriority.MEDIUM
    if number >= 1:
        return TaskPriority.LOW
    return None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _safe_error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
        return str(body.get("error_description") or body.get("message") or body)
    except ValueError:
        return response.text[:300]
