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
    day: date | None = None
    sleep_deviation_reason: str | None = None
    wake_deviation_reason: str | None = None
    note: str | None = None


SLEEP_MARKER_PATTERNS = (
    r"\bлег(?:ла)?(?:\s+спать)?\b",
    r"\bуснул(?:а)?\b",
    r"\bсон\s+в\b",
    r"\bпош[её]л\s+спать\b",
    r"\bпошла\s+спать\b",
)
WAKE_MARKER_PATTERNS = (
    r"\bпроснулся\b",
    r"\bпроснулась\b",
    r"\bвстал\b",
    r"\bвстала\b",
    r"\bпод[ъь]ем\s+в\b",
    r"\bподъём\s+в\b",
    r"\bпросыпаюсь\b",
)
WIND_DOWN_MARKER_PATTERNS = (
    r"\bотход\s+ко\s+сну\b",
    r"\bготовиться\s+ко\s+сну\b",
    r"\bпойду\s+спать\b",
    r"\bпойду\s+через\b",
    r"\bрежим\s+сна\b",
)
WORK_FINISHED_MARKER_PATTERNS = (
    r"\bзакончил(?:а)?\s+работу\b",
    r"\bзавершил(?:а)?\s+работу\b",
    r"\bзакончил(?:а)?\s+задачи\b",
    r"\bвыключил(?:а)?\s+ноутбук\b",
)
TIME_EVENT_MARKER_PATTERNS = (
    *SLEEP_MARKER_PATTERNS,
    *WAKE_MARKER_PATTERNS,
    *WIND_DOWN_MARKER_PATTERNS,
    *WORK_FINISHED_MARKER_PATTERNS,
)


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
    has_sleep = _has_marker(text, SLEEP_MARKER_PATTERNS)
    has_wake = _has_marker(text, WAKE_MARKER_PATTERNS)
    has_wind_down = _has_marker(text, WIND_DOWN_MARKER_PATTERNS)
    has_work_finished = _has_marker(text, WORK_FINISHED_MARKER_PATTERNS)
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

    sleep_time = _extract_event_time(text, SLEEP_MARKER_PATTERNS, now=now, tzinfo=tzinfo) if has_sleep else None
    wake_time = _extract_event_time(text, WAKE_MARKER_PATTERNS, now=now, tzinfo=tzinfo) if has_wake else None
    wind_down_time = (
        _extract_event_time(text, WIND_DOWN_MARKER_PATTERNS, now=now, tzinfo=tzinfo) if has_wind_down else None
    )
    work_finished_time = (
        _extract_event_time(text, WORK_FINISHED_MARKER_PATTERNS, now=now, tzinfo=tzinfo)
        if has_work_finished
        else None
    )
    explicit_now = any(marker in lowered for marker in ("сейчас", "только что"))
    if explicit_now:
        sleep_time = sleep_time or (now.strftime("%H:%M") if has_sleep else None)
        wake_time = wake_time or (now.strftime("%H:%M") if has_wake else None)
        wind_down_time = wind_down_time or (now.strftime("%H:%M") if has_wind_down else None)
        work_finished_time = work_finished_time or (now.strftime("%H:%M") if has_work_finished else None)

    focus_done: bool | None = None
    if has_focus_missed:
        focus_done = False
    elif has_focus_done:
        focus_done = True

    if not any((sleep_time, wake_time, wind_down_time, work_finished_time)) and focus_done is None:
        return None

    reason = _extract_reason(text)
    return ParsedEffectivenessText(
        sleep_time=sleep_time,
        wake_time=wake_time,
        wind_down_time=wind_down_time,
        work_finished_time=work_finished_time,
        focus_done=focus_done,
        day=_extract_day(text, now=now, tzinfo=tzinfo),
        sleep_deviation_reason=reason if reason and has_sleep else None,
        wake_deviation_reason=reason if reason and has_wake and not has_sleep else None,
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

    spoken_time = _extract_spoken_time(text)
    if spoken_time:
        return spoken_time

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
    for matched_text, parsed_dt in reversed(matches):
        if not _dateparser_match_has_time(matched_text):
            continue
        if not parsed_dt.hour and not parsed_dt.minute:
            continue
        return parsed_dt.strftime("%H:%M")
    return None


def _has_marker(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _extract_event_time(text: str, patterns: tuple[str, ...], now: datetime, tzinfo: ZoneInfo) -> str | None:
    marker = _first_marker(text, patterns)
    if not marker:
        return None
    end = _next_time_event_start(text, marker.start()) or len(text)
    after_marker = text[marker.start() : end]
    time_value = _extract_time(after_marker, now=now, tzinfo=tzinfo)
    if time_value:
        return time_value
    with_prefix = text[max(0, marker.start() - 32) : end]
    return _extract_time(with_prefix, now=now, tzinfo=tzinfo)


def _first_marker(text: str, patterns: tuple[str, ...]) -> re.Match[str] | None:
    matches = [
        match
        for pattern in patterns
        if (match := re.search(pattern, text, flags=re.IGNORECASE))
    ]
    if not matches:
        return None
    return min(matches, key=lambda match: match.start())


def _next_time_event_start(text: str, start: int) -> int | None:
    starts = [
        match.start()
        for pattern in TIME_EVENT_MARKER_PATTERNS
        for match in re.finditer(pattern, text, flags=re.IGNORECASE)
        if match.start() > start
    ]
    return min(starts) if starts else None


def _extract_spoken_time(text: str) -> str | None:
    patterns = (
        r"\b(?:в|около|примерно)\s+(?P<hour_word>полночь|полдень|час|(?P<hour>\d{1,2}))"
        r"(?:\s*час(?:ов|а)?)?(?:\s*(?P<period>ночи|утра|дня|вечера))?\b",
        r"\b(?P<hour>\d{1,2})\s*(?:час(?:ов|а)?)?\s*(?P<period>ночи|утра|дня|вечера)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        hour_word = match.groupdict().get("hour_word")
        hour_value = match.groupdict().get("hour")
        period = match.groupdict().get("period")
        hour = _normalize_spoken_hour(hour_word or hour_value or "", period)
        if hour is not None:
            return f"{hour:02d}:00"
    return None


def _normalize_spoken_hour(value: str, period: str | None) -> int | None:
    lowered = value.lower()
    if lowered == "полночь":
        return 0
    if lowered == "полдень":
        return 12
    hour = 1 if lowered == "час" else int(lowered) if lowered.isdigit() else None
    if hour is None or hour > 23:
        return None
    if period in {"вечера"} and 1 <= hour < 12:
        hour += 12
    elif period == "дня" and 1 <= hour < 12:
        hour += 12
    elif period == "ночи" and hour == 12:
        hour = 0
    elif period == "утра" and hour == 12:
        hour = 0
    return hour


def _dateparser_match_has_time(text: str) -> bool:
    lowered = text.lower()
    return bool(re.search(r"\d|час|полноч|полден|утра|вечера|ночи|дня|мин", lowered))


def _extract_day(text: str, now: datetime, tzinfo: ZoneInfo) -> date | None:
    lowered = text.lower()
    today = now.astimezone(tzinfo).date()
    if "позавчера" in lowered:
        return today - timedelta(days=2)
    if "вчера" in lowered:
        return today - timedelta(days=1)
    if "сегодня" in lowered:
        return today
    return None


def _extract_reason(text: str) -> str | None:
    match = re.search(r"\b(?:потому\s*,?\s*что|так\s+как|из-за|из\s+за)\s+(?P<reason>.+)", text, flags=re.IGNORECASE)
    if not match:
        match = re.search(r"(?:^|[,;])\s*так\s+(?P<reason>.+)", text, flags=re.IGNORECASE)
    if not match:
        return None
    reason = match.group("reason")
    reason = re.split(
        r"[,;]?\s+(?:и\s+)?(?:проснул\w*|встал[а]?|под[ъь]ем|подъём)\b",
        reason,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    reason = reason.strip(" .,!?:;")
    return reason[:500] or None

def is_compound_message(text: str) -> bool:
    lowered = text.lower().strip()
    return any(marker in lowered for marker in ("рекомендац", "посовет", "можешь", "систем", "эффективн", "?"))


def should_auto_record_effectiveness(text: str, now: datetime, tzinfo: ZoneInfo) -> bool:
    if is_compound_message(text):
        return False
    parsed = parse_effectiveness_text(text, now=now, tzinfo=tzinfo)
    if parsed and any((parsed.sleep_time, parsed.wake_time)):
        return True
    return _has_marker(text, SLEEP_MARKER_PATTERNS) or _has_marker(text, WAKE_MARKER_PATTERNS)


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
