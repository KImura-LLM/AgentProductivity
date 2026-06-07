from datetime import date
from zoneinfo import ZoneInfo

from productivity_agent.connectors.notion import _extract_date
from productivity_agent.connectors.ticktick import _parse_due_date

MOSCOW = ZoneInfo("Europe/Moscow")


def test_ticktick_due_date_converts_utc_to_configured_timezone() -> None:
    due_date, due_time = _parse_due_date("2026-05-26T19:00:00.000+0000", MOSCOW)

    assert due_date == date(2026, 5, 26)
    assert due_time == "22:00"


def test_ticktick_all_day_due_date_keeps_date_without_time() -> None:
    due_date, due_time = _parse_due_date(
        "2026-05-26T00:00:00.000+0000",
        MOSCOW,
        all_day=True,
    )

    assert due_date == date(2026, 5, 26)
    assert due_time is None


def test_notion_date_converts_utc_to_configured_timezone() -> None:
    due_date, due_time = _extract_date(
        {"date": {"start": "2026-05-26T19:00:00.000+00:00", "time_zone": None}},
        MOSCOW,
    )

    assert due_date == date(2026, 5, 26)
    assert due_time == "22:00"


def test_notion_date_only_keeps_date_without_time() -> None:
    due_date, due_time = _extract_date({"date": {"start": "2026-05-26"}}, MOSCOW)

    assert due_date == date(2026, 5, 26)
    assert due_time is None
