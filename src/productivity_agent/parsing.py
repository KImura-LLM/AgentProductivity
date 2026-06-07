from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
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


@dataclass(frozen=True)
class ParsedEffectivenessText:
    sleep_time: str | None = None
    wake_time: str | None = None
    wind_down_time: str | None = None
    work_finished_time: str | None = None
    focus_done: bool | None = None
    note: str | None = None


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


def parse_effectiveness_text(text: str, now: datetime, tzinfo: ZoneInfo) -> ParsedEffectivenessText | None:
    lowered = text.lower().strip()
    fields: dict[str, str | bool | None] = {
        "sleep_time": None,
        "wake_time": None,
        "wind_down_time": None,
        "work_finished_time": None,
        "focus_done": None,
    }

    has_sleep = any(marker in lowered for marker in ("лег спать", "легла спать", "уснул", "уснула", "сон в"))
    has_wake = any(
        marker in lowered
        for marker in (
            "проснулся",
            "проснулась",
            "встал",
            "встала",
            "подъем в",
            "подъём в",
            "просыпаюсь",
            "проснулся в",
        )
    )
    has_wind_down = any(
        marker in lowered
        for marker in (
            "отход ко сну",
            "готовиться ко сну",
            "пошел спать",
            "пошла спать",
            "пойду спать",
            "пойду через",
            "режим сна",
        )
    )
    has_work_finished = any(
        marker in lowered
        for marker in (
            "закончил работу",
            "закончила работу",
            "завершил работу",
            "завершила работу",
            "закончил задачи",
            "закончила задачи",
            "выключил ноутбук",
            "выключила ноутбук",
        )
    )
    has_focus_done = any(
        marker in lowered
        for marker in (
            "фокус сделал",
            "фокус сделан",
            "фокус выполнил",
            "фокус выполнен",
            "главный фокус сделал",
            "главный фокус выполнен",
        )
    )
    has_focus_missed = any(
        marker in lowered
        for marker in (
            "фокус не сделал",
            "фокус не сделан",
            "фокус не выполнил",
            "фокус не выполнен",
            "главный фокус не сделал",
            "главный фокус не выполнен",
        )
    )

    if not any((has_sleep, has_wake, has_wind_down, has_work_finished, has_focus_done, has_focus_missed)):
        return None

    time_value = _extract_time(text, now=now, tzinfo=tzinfo)
    explicit_now = any(marker in lowered for marker in ("сейчас", "только что"))
    if time_value is None and explicit_now:
        time_value = now.strftime("%H:%M")

    if has_sleep:
        fields["sleep_time"] = time_value
    if has_wake:
        fields["wake_time"] = time_value
    if has_wind_down:
        fields["wind_down_time"] = time_value
    if has_work_finished:
        fields["work_finished_time"] = time_value
    if has_focus_missed:
        fields["focus_done"] = False
    elif has_focus_done:
        fields["focus_done"] = True

    if time_value is None and fields["focus_done"] is None:
        return None

    return ParsedEffectivenessText(
        sleep_time=fields["sleep_time"] if isinstance(fields["sleep_time"], str) else None,
        wake_time=fields["wake_time"] if isinstance(fields["wake_time"], str) else None,
        wind_down_time=fields["wind_down_time"] if isinstance(fields["wind_down_time"], str) else None,
        work_finished_time=fields["work_finished_time"] if isinstance(fields["work_finished_time"], str) else None,
        focus_done=fields["focus_done"] if isinstance(fields["focus_done"], bool) else None,
        note=text.strip(),
    )


def _extract_time(text: str, now: datetime, tzinfo: ZoneInfo) -> str | None:
    explicit_match = re.search(r"\b(?P<hour>[01]?\d|2[0-3])[:.](?P<minute>[0-5]\d)\b", text)
    if explicit_match:
        return f"{int(explicit_match.group('hour')):02d}:{int(explicit_match.group('minute')):02d}"

    relative_minutes = re.search(
        r"\bчерез\s+(?P<minutes>\d{1,3})\s*(?:минут|минуты|минуту|мин)\b",
        text,
        flags=re.IGNORECASE,
    )
    if relative_minutes:
        parsed_dt = now.astimezone(tzinfo) + timedelta(minutes=int(relative_minutes.group("minutes")))
        return parsed_dt.strftime("%H:%M")

    matches = search_dates(
        text,
        languages=["ru", "en"],
        settings={
            "PREFER_DATES_FROM": "past",
            "RELATIVE_BASE": now,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "TIMEZONE": str(tzinfo),
        },
    )
    if not matches:
        return None
    _, parsed_dt = matches[-1]
    if not parsed_dt.hour and not parsed_dt.minute:
        return None
    return parsed_dt.strftime("%H:%M")

def is_compound_message(text: str) -> bool:
    lowered = text.lower().strip()
    words = re.findall(r"\w+", lowered, flags=re.UNICODE)
    if len(words) >= 22:
        return True
    return any(marker in lowered for marker in ("рекомендац", "посовет", "можешь", "систем", "эффективн", "?"))


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
    if "картинка графика сна" in lowered or "изображение графика сна" in lowered or "sleepchart" in lowered:
        return "sleepchart"
    if "эффективность" in lowered or "режим сна" in lowered or "статистика сна" in lowered:
        return "effectiveness"
    if lowered.startswith(("добавь задачу", "создай задачу")) and not is_compound_message(text):
        return "add"
    if lowered.startswith(("отметь задачу", "закрой задачу")) and not is_compound_message(text):
        return "done"
    if lowered.startswith(("перенеси задачу", "перенеси", "перенести задачу")) and not is_compound_message(text):
        return "reschedule"
    return None
