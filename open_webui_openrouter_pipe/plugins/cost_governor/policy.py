"""Pure policy helpers for the cost governor: limit parsing, resolution, month windows."""

from __future__ import annotations

import datetime
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_UNLIMITED_WORDS = frozenset({"unlimited", "none", "inf", "infinite", "no", "-1"})


def governor_parse_group_limits(raw: str) -> tuple[dict[str, float | None], list[str]]:
    """Parse ``name=amount`` pairs separated by commas or newlines.

    Names are lower-cased. An amount of ``unlimited`` (or ``-1``) maps to ``None``.
    Returns the limits and the entries that could not be read.
    """
    limits: dict[str, float | None] = {}
    problems: list[str] = []
    for chunk in str(raw or "").replace("\n", ",").split(","):
        entry = chunk.strip()
        if not entry:
            continue
        name, sep, amount = entry.partition("=")
        name = name.strip().lower()
        amount = amount.strip().lower().lstrip("$")
        if not sep or not name or not amount:
            problems.append(entry)
            continue
        if amount in _UNLIMITED_WORDS:
            limits[name] = None
            continue
        try:
            value = float(amount)
        except ValueError:
            problems.append(entry)
            continue
        if value < 0 or math.isnan(value):
            problems.append(entry)
            continue
        limits[name] = value
    return limits, problems


def governor_resolve_limit(
    group_names: list[str],
    group_limits: dict[str, float | None],
    default_limit: float,
    *,
    is_admin: bool,
    exempt_admins: bool,
) -> float | None:
    """Monthly limit in USD for one user; ``None`` means unlimited.

    The most generous matching group wins. A negative default means unlimited.
    """
    if is_admin and exempt_admins:
        return None
    matched = [group_limits[name.lower()] for name in group_names if name.lower() in group_limits]
    if matched:
        if any(value is None for value in matched):
            return None
        return max(value for value in matched if value is not None)
    if default_limit < 0:
        return None
    return float(default_limit)


def governor_month_window(now: float, tz_name: str) -> tuple[int, int]:
    """Epoch bounds ``[start, end)`` of the calendar month holding ``now`` in ``tz_name``."""
    try:
        zone = ZoneInfo(tz_name or "UTC")
    except (ZoneInfoNotFoundError, ValueError, OSError):
        zone = ZoneInfo("UTC")
    local = datetime.datetime.fromtimestamp(now, tz=zone)
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return int(start.timestamp()), int(end.timestamp())
