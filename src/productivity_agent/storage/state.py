from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from productivity_agent.models import EffectivenessEntry, PendingAction


class JsonStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"pending_actions": {}, "ticktick": {}, "effectiveness": {}}
        with self.path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        data.setdefault("pending_actions", {})
        data.setdefault("ticktick", {})
        data.setdefault("effectiveness", {})
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2, sort_keys=True)
        tmp_path.replace(self.path)

    def get_pending_action(self, user_id: int) -> PendingAction | None:
        data = self._read()
        raw = data["pending_actions"].get(str(user_id))
        if not raw:
            return None
        return PendingAction.model_validate(raw)

    def set_pending_action(self, action: PendingAction) -> None:
        data = self._read()
        data["pending_actions"][str(action.user_id)] = action.model_dump(mode="json")
        self._write(data)

    def clear_pending_action(self, user_id: int) -> None:
        data = self._read()
        data["pending_actions"].pop(str(user_id), None)
        self._write(data)

    def clear_expired_actions(self, now: datetime) -> None:
        data = self._read()
        kept = {}
        for user_id, raw in data["pending_actions"].items():
            action = PendingAction.model_validate(raw)
            if not action.expired(now):
                kept[user_id] = raw
        data["pending_actions"] = kept
        self._write(data)

    def get_ticktick_tokens(self) -> dict[str, Any]:
        data = self._read()
        return dict(data.get("ticktick", {}))

    def set_ticktick_tokens(self, tokens: dict[str, Any]) -> None:
        data = self._read()
        data["ticktick"] = tokens
        self._write(data)

    def get_effectiveness_entry(self, day: datetime | str) -> EffectivenessEntry | None:
        key = day.date().isoformat() if isinstance(day, datetime) else day
        data = self._read()
        raw = data["effectiveness"].get(key)
        if not raw:
            return None
        return EffectivenessEntry.model_validate(raw)

    def set_effectiveness_entry(self, entry: EffectivenessEntry) -> None:
        data = self._read()
        data["effectiveness"][entry.day.isoformat()] = entry.model_dump(mode="json")
        self._write(data)

    def list_effectiveness_entries(self, limit: int = 14) -> list[EffectivenessEntry]:
        data = self._read()
        entries = [EffectivenessEntry.model_validate(raw) for raw in data["effectiveness"].values()]
        return sorted(entries, key=lambda item: item.day)[-limit:]
