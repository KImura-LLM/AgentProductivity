from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from dateparser.search import search_dates

SOURCE_TICKTICK_MARKERS = ("ticktick", "тиктик", "личн", "жизн", "быт", "здоров")
SOURCE_NOTION_MARKERS = ("notion", "ноушн", "проект", "рабоч", "учеб", "study", "work")


@dataclass(frozen=True)
class ParsedTaskText:
    title: str
    deadline: date | None
    time: str | None
    source_hint: str | None
    database_hint: str | None = None


def detect_source(text: str) -> str | None:
    lowered = text.lower()
    if any(marker in lowered for marker in SOURCE_TICKTICK_MARKERS):
        return "ticktick"
    if any(marker in lowered for marker in SOURCE_NOTION_MARKERS):
        return "notion"
    return None


def _strip_command(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^/(add|done|reschedule|project)\b", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"^(добавь\s+задачу|создай\s+задачу|добавить\s+задачу)\s*:?", "", text, flags=re.IGNORECASE).strip()
    return text


def parse_task_text(text: str, now: datetime, tzinfo: ZoneInfo) -> ParsedTaskText:
    cleaned = _strip_command(text)
    source_hint = detect_source(cleaned)
    matches = search_dates(
        cleaned,
        languages=["ru", "en"],
        settings={
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": now,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "TIMEZONE": str(tzinfo),
        },
    )

    parsed_dt: datetime | None = None
    matched_text = ""
    if matches:
        matched_text, parsed_dt = matches[-1]
        cleaned = cleaned.replace(matched_text, " ")

    title = re.sub(r"\b(ticktick|тиктик|notion|ноушн)\b", " ", cleaned, flags=re.IGNORECASE)
    title = re.sub(r"\s+", " ", title).strip(" .,-")

    deadline = parsed_dt.date() if parsed_dt else None
    time_value = parsed_dt.strftime("%H:%M") if parsed_dt and (parsed_dt.hour or parsed_dt.minute) else None
    return ParsedTaskText(
        title=title or cleaned.strip() or text.strip(),
        deadline=deadline,
        time=time_value,
        source_hint=source_hint,
    )


def parse_reschedule_text(text: str, now: datetime, tzinfo: ZoneInfo) -> tuple[str, date | None]:
    cleaned = _strip_command(text)
    matches = search_dates(
        cleaned,
        languages=["ru", "en"],
        settings={
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": now,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "TIMEZONE": str(tzinfo),
        },
    )
    if not matches:
        return cleaned.strip(), None
    matched_text, parsed_dt = matches[-1]
    query = cleaned.replace(matched_text, " ")
    query = re.sub(r"\b(на|к|до)\b", " ", query, flags=re.IGNORECASE)
    query = re.sub(r"\s+", " ", query).strip(" .,-")
    return query, parsed_dt.date()


def parse_status_change(text: str) -> tuple[str, str] | None:
    patterns = [
        r"поставь\s+задачу\s+[«\"]?(?P<title>.+?)[»\"]?\s+в\s+(?P<status>[\w\s-]+)$",
        r"измени\s+статус\s+[«\"]?(?P<title>.+?)[»\"]?\s+на\s+(?P<status>[\w\s-]+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, text.strip(), flags=re.IGNORECASE)
        if match:
            return match.group("title").strip(" «»\""), match.group("status").strip()
    return None


def parse_natural_command(text: str) -> str | None:
    lowered = text.lower().strip()
    if lowered in {"да", "yes", "y"}:
        return "confirm"
    if lowered in {"нет", "no", "n", "отмена", "cancel"}:
        return "cancel"
    if re.fullmatch(r"\d{1,2}", lowered):
        return "select"
    if "что мне делать сегодня" in lowered or "план на сегодня" in lowered:
        return "today"
    if "что делать на неделе" in lowered or "план на неделю" in lowered or "обзор недели" in lowered:
        return "week"
    if "главный фокус" in lowered or "на чем сфокусироваться" in lowered:
        return "focus"
    if "что зависло" in lowered or "зависшие задачи" in lowered:
        return "stuck"
    if "личные задачи" in lowered or "задачи ticktick" in lowered:
        return "life"
    if lowered.startswith(("добавь задачу", "создай задачу")):
        return "add"
    if lowered.startswith(("отметь задачу", "закрой задачу")):
        return "done"
    if lowered.startswith(("перенеси задачу", "перенеси", "перенести задачу")):
        return "reschedule"
    return None
