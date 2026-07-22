from __future__ import annotations

import re
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.settings import get_settings
from app.storage.workspace import bootstraps_dir

_WEEKDAY_ZH = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
_IANA_TZ_RE = re.compile(r"\b([A-Za-z]+(?:/[A-Za-z0-9_+-]+)+)\b")
_OFFSET_TZ_RE = re.compile(r"\b(?:UTC|GMT)\s*([+-])\s*(\d{1,2})(?::(\d{2}))?\b", re.IGNORECASE)
_CN_TZ_HINTS: dict[str, str] = {
    "北京时间": "Asia/Shanghai",
    "中国标准时间": "Asia/Shanghai",
    "东京时间": "Asia/Tokyo",
    "首尔时间": "Asia/Seoul",
    "香港时间": "Asia/Hong_Kong",
    "台北时间": "Asia/Taipei",
}
# Windows 未安装 tzdata 时，常见 IANA 时区的固定偏移回退（不含夏令时）
_IANA_OFFSET_FALLBACK_HOURS: dict[str, int] = {
    "UTC": 0,
    "Asia/Shanghai": 8,
    "Asia/Hong_Kong": 8,
    "Asia/Taipei": 8,
    "Asia/Tokyo": 9,
    "Asia/Seoul": 9,
    "Asia/Singapore": 8,
    "Europe/London": 0,
    "America/New_York": -5,
    "America/Los_Angeles": -8,
}


def _read_user_md() -> str:
    path = bootstraps_dir(get_settings().workspace_path) / "USER.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _parse_offset(name: str) -> dt_timezone | None:
    match = _OFFSET_TZ_RE.search(name)
    if not match:
        return None
    sign = 1 if match.group(1) == "+" else -1
    hours = int(match.group(2))
    minutes = int(match.group(3) or 0)
    if hours > 23 or minutes > 59:
        return None
    return dt_timezone(sign * timedelta(hours=hours, minutes=minutes))


def _resolve_zoneinfo(name: str) -> ZoneInfo | dt_timezone | None:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        hours = _IANA_OFFSET_FALLBACK_HOURS.get(name)
        if hours is not None:
            return dt_timezone(timedelta(hours=hours))
        return None


def resolve_timezone(name: str | None = None) -> ZoneInfo | dt_timezone:
    """解析时区：显式参数 > USER.md 偏好 > 系统本地。"""
    candidates: list[str] = []
    if name and name.strip():
        candidates.append(name.strip())

    user_text = _read_user_md()
    if user_text:
        for hint, tz in _CN_TZ_HINTS.items():
            if hint in user_text:
                candidates.append(tz)
        for match in _IANA_TZ_RE.finditer(user_text):
            candidates.append(match.group(1))

    for candidate in candidates:
        offset = _parse_offset(candidate)
        if offset is not None:
            return offset
        zone = _resolve_zoneinfo(candidate)
        if zone is not None:
            return zone

    return datetime.now().astimezone().tzinfo or dt_timezone.utc


def _format_offset(tzinfo: ZoneInfo | dt_timezone) -> str:
    now = datetime.now(tzinfo)
    offset = now.utcoffset()
    if offset is None:
        return "UTC"
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    hours, minutes = divmod(total_minutes, 60)
    if minutes:
        return f"UTC{sign}{hours:02d}:{minutes:02d}"
    return f"UTC{sign}{hours}"


def _timezone_label(tzinfo: ZoneInfo | dt_timezone) -> str:
    if isinstance(tzinfo, ZoneInfo):
        return tzinfo.key
    if tzinfo is dt_timezone.utc:
        return "UTC"
    for name, hours in _IANA_OFFSET_FALLBACK_HOURS.items():
        if tzinfo == dt_timezone(timedelta(hours=hours)):
            return name
    return _format_offset(tzinfo)


def get_current_time(timezone: str | None = None) -> str:
    tzinfo = resolve_timezone(timezone)
    now = datetime.now(tzinfo)
    utc_now = now.astimezone(dt_timezone.utc)
    label = _timezone_label(tzinfo)
    offset = _format_offset(tzinfo)
    weekday = _WEEKDAY_ZH[now.weekday()]

    lines = [
        f"当前时间: {now:%Y-%m-%d %H:%M:%S} ({weekday})",
        f"时区: {label} ({offset})",
        f"ISO: {now.isoformat(timespec='seconds')}",
        f"UTC: {utc_now.isoformat(timespec='seconds')}",
    ]
    return "\n".join(lines)
