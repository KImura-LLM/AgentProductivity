from __future__ import annotations

import json
from datetime import time
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

TaskArea = Literal["work", "study", "life", "health", "admin", "other"]


DEFAULT_FIELDS_MAP = {
    "title": "Name",
    "status": "Status",
    "deadline": "Deadline",
    "priority": "Priority",
    "project": "Project",
    "area": "Area",
    "energy": "Energy",
    "estimated_minutes": "Estimated time",
    "notes": "Notes",
}


class NotionDatabaseConfig(BaseModel):
    name: str
    data_source_id: str | None = None
    database_id: str | None = None
    type: Literal["tasks", "projects"] = "tasks"
    area: TaskArea = "other"
    fields_map: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_FIELDS_MAP))
    done_status: str = "Done"
    todo_status: str = "To Do"
    in_progress_status: str = "In Progress"
    waiting_status: str = "Waiting"

    @model_validator(mode="after")
    def ensure_identifier(self) -> NotionDatabaseConfig:
        if not self.data_source_id and not self.database_id:
            raise ValueError("Either data_source_id or database_id is required")
        return self

    @property
    def notion_id(self) -> str:
        return self.data_source_id or self.database_id or ""

    @property
    def parent_type(self) -> str:
        return "data_source_id" if self.data_source_id else "database_id"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    telegram_bot_token: str = Field(default="", validation_alias="TELEGRAM_BOT_TOKEN")
    telegram_allowed_user_id: int | None = Field(
        default=None, validation_alias="TELEGRAM_ALLOWED_USER_ID"
    )

    openrouter_api_key: str = Field(default="", validation_alias="OPENROUTER_API_KEY")
    openrouter_model: str = Field(default="~google/gemini-flash-latest", validation_alias="OPENROUTER_MODEL")
    openrouter_image_model: str = Field(
        default="google/gemini-2.5-flash-image",
        validation_alias="OPENROUTER_IMAGE_MODEL",
    )
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1",
        validation_alias="OPENROUTER_BASE_URL",
    )
    openrouter_http_referer: str = Field(default="", validation_alias="OPENROUTER_HTTP_REFERER")
    openrouter_app_title: str = Field(default="AgentProductivity", validation_alias="OPENROUTER_APP_TITLE")

    notion_token: str = Field(default="", validation_alias="NOTION_TOKEN")
    notion_version: str = Field(default="2025-09-03", validation_alias="NOTION_VERSION")
    notion_databases_json: str = Field(default="", validation_alias="NOTION_DATABASES_JSON")
    notion_tasks_database_id: str = Field(default="", validation_alias="NOTION_TASKS_DATABASE_ID")
    notion_projects_database_id: str = Field(
        default="", validation_alias="NOTION_PROJECTS_DATABASE_ID"
    )

    ticktick_client_id: str = Field(default="", validation_alias="TICKTICK_CLIENT_ID")
    ticktick_client_secret: str = Field(default="", validation_alias="TICKTICK_CLIENT_SECRET")
    ticktick_redirect_uri: str = Field(
        default="http://127.0.0.1:8765/callback", validation_alias="TICKTICK_REDIRECT_URI"
    )
    ticktick_access_token: str = Field(default="", validation_alias="TICKTICK_ACCESS_TOKEN")
    ticktick_refresh_token: str = Field(default="", validation_alias="TICKTICK_REFRESH_TOKEN")

    timezone: str = Field(default="Europe/Moscow", validation_alias="TIMEZONE")
    morning_briefing_time: str = Field(default="07:30", validation_alias="MORNING_BRIEFING_TIME")
    evening_review_time: str = Field(default="21:00", validation_alias="EVENING_REVIEW_TIME")
    weekly_review_time: str = Field(default="10:00", validation_alias="WEEKLY_REVIEW_TIME")
    overdue_check_time: str = Field(default="12:00", validation_alias="OVERDUE_CHECK_TIME")
    target_sleep_time: str = Field(default="23:30", validation_alias="TARGET_SLEEP_TIME")
    target_wake_time: str = Field(default="07:30", validation_alias="TARGET_WAKE_TIME")
    wind_down_time: str = Field(default="22:45", validation_alias="WIND_DOWN_TIME")
    work_shutdown_time: str = Field(default="21:30", validation_alias="WORK_SHUTDOWN_TIME")
    telegram_connect_timeout: float = Field(default=30.0, validation_alias="TELEGRAM_CONNECT_TIMEOUT")
    telegram_read_timeout: float = Field(default=30.0, validation_alias="TELEGRAM_READ_TIMEOUT")
    telegram_write_timeout: float = Field(default=30.0, validation_alias="TELEGRAM_WRITE_TIMEOUT")
    telegram_pool_timeout: float = Field(default=30.0, validation_alias="TELEGRAM_POOL_TIMEOUT")
    telegram_get_updates_read_timeout: float = Field(
        default=45.0,
        validation_alias="TELEGRAM_GET_UPDATES_READ_TIMEOUT",
    )

    app_state_path: Path = Field(default=Path(".state/agent-state.json"), validation_alias="APP_STATE_PATH")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator("telegram_allowed_user_id", mode="before")
    @classmethod
    def blank_user_id_is_missing(cls, value: Any) -> Any:
        if value == "":
            return None
        return value

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def notion_databases(self) -> list[NotionDatabaseConfig]:
        if self.notion_databases_json:
            raw = json.loads(self.notion_databases_json)
            databases = raw["databases"] if isinstance(raw, dict) and "databases" in raw else raw
            return [NotionDatabaseConfig.model_validate(item) for item in databases]

        databases: list[NotionDatabaseConfig] = []
        if self.notion_tasks_database_id:
            databases.append(
                NotionDatabaseConfig(
                    name="Tasks",
                    database_id=self.notion_tasks_database_id,
                    type="tasks",
                    area="work",
                )
            )
        if self.notion_projects_database_id:
            databases.append(
                NotionDatabaseConfig(
                    name="Projects",
                    database_id=self.notion_projects_database_id,
                    type="projects",
                    area="work",
                )
            )
        return databases

    def parse_time(self, value: str) -> time:
        try:
            hour_s, minute_s = value.split(":", 1)
            return time(int(hour_s), int(minute_s), tzinfo=self.tzinfo)
        except ValueError as exc:
            raise ValueError(f"Invalid time value {value!r}; expected HH:MM") from exc

    def secret_values(self) -> list[str]:
        values: list[str] = [
            self.telegram_bot_token,
            self.openrouter_api_key,
            self.notion_token,
            self.ticktick_client_secret,
            self.ticktick_access_token,
            self.ticktick_refresh_token,
        ]
        return [value for value in values if value]

    def has_telegram(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_allowed_user_id)

    def as_safe_dict(self) -> dict[str, Any]:
        return {
            "timezone": self.timezone,
            "morning_briefing_time": self.morning_briefing_time,
            "evening_review_time": self.evening_review_time,
            "weekly_review_time": self.weekly_review_time,
            "overdue_check_time": self.overdue_check_time,
            "target_sleep_time": self.target_sleep_time,
            "target_wake_time": self.target_wake_time,
            "wind_down_time": self.wind_down_time,
            "work_shutdown_time": self.work_shutdown_time,
            "telegram_connect_timeout": self.telegram_connect_timeout,
            "telegram_read_timeout": self.telegram_read_timeout,
            "telegram_write_timeout": self.telegram_write_timeout,
            "telegram_pool_timeout": self.telegram_pool_timeout,
            "telegram_get_updates_read_timeout": self.telegram_get_updates_read_timeout,
            "notion_databases": [db.name for db in self.notion_databases],
            "ticktick_configured": bool(self.ticktick_access_token or self.ticktick_refresh_token),
            "openrouter_model": self.openrouter_model,
            "openrouter_image_model": self.openrouter_image_model,
        }


def load_settings() -> Settings:
    return Settings()
