"""One-shot scheduled account switches, fired by a live auto-switch engine.

Owns the ``autoswitch_schedule.json`` format; nothing else reads its JSON.
Entries are claimed (removed) before the switch runs, so concurrent engines
deliver each one at most once.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from claude_swap.exceptions import ValidationError
from claude_swap.locking import FileLock
from claude_swap.settings import atomic_write_json

SCHEDULE_FILENAME = "autoswitch_schedule.json"
SCHEMA_VERSION = 1

_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_RELATIVE_RE = re.compile(r"^\+(?:(\d+)h)?(?:(\d+)m)?$")


@dataclass(frozen=True)
class ScheduledSwitch:
    id: str
    number: str
    email: str
    at: float
    created_at: float

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "number": self.number,
            "email": self.email,
            "at": self.at,
            "createdAt": self.created_at,
        }


def _from_json(raw: object) -> ScheduledSwitch | None:
    if not isinstance(raw, dict):
        return None
    try:
        entry_id, number, email = raw["id"], raw["number"], raw["email"]
        at, created = raw["at"], raw["createdAt"]
    except KeyError:
        return None
    if not all(isinstance(v, str) and v for v in (entry_id, number)):
        return None
    if not isinstance(email, str):
        return None
    for value in (at, created):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
    return ScheduledSwitch(entry_id, number, email, float(at), float(created))


def parse_when(text: str, now: datetime) -> float:
    """Epoch seconds for ``HH:MM``, ``YYYY-MM-DD HH:MM``, ISO-8601 or ``+1h30m``.

    Naive times are local. ``now`` is the reference for relative and
    clock-only forms.
    """
    text = text.strip()
    if not text:
        raise ValidationError("Missing time: try 23:30, +90m or 2026-11-18 09:00")

    relative = _RELATIVE_RE.match(text)
    if relative and (relative.group(1) or relative.group(2)):
        minutes = int(relative.group(1) or 0) * 60 + int(relative.group(2) or 0)
        if minutes <= 0:
            raise ValidationError(f"Relative time must be positive: {text!r}")
        return now.timestamp() + minutes * 60

    clock = _CLOCK_RE.match(text)
    if clock:
        hour, minute = int(clock.group(1)), int(clock.group(2))
        if hour > 23 or minute > 59:
            raise ValidationError(f"Invalid time of day: {text!r}")
        local_now = now.astimezone().replace(tzinfo=None)
        target = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= local_now:
            target += timedelta(days=1)
        return target.astimezone().timestamp()

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValidationError(
            f"Cannot parse time {text!r}: try 23:30, +90m or 2026-11-18 09:00"
        ) from None
    at = parsed.timestamp()
    if at <= now.timestamp():
        raise ValidationError(f"{text!r} is in the past")
    return at


def _path(backup_dir: Path) -> Path:
    return backup_dir / SCHEDULE_FILENAME


def _lock(backup_dir: Path) -> FileLock:
    return FileLock(backup_dir / ".autoswitch_schedule.lock")


def _read(backup_dir: Path) -> list[ScheduledSwitch]:
    try:
        raw = json.loads(_path(backup_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    items = raw.get("pending") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return []
    return [e for e in map(_from_json, items) if e is not None]


def _write(backup_dir: Path, entries: list[ScheduledSwitch]) -> None:
    atomic_write_json(
        _path(backup_dir),
        {"schemaVersion": SCHEMA_VERSION, "pending": [e.to_json() for e in entries]},
    )


def _mutate(
    backup_dir: Path,
    fn: Callable[[list[ScheduledSwitch]], object],
) -> object:
    with _lock(backup_dir):
        entries = _read(backup_dir)
        result = fn(entries)
        _write(backup_dir, entries)
        return result


def add(
    backup_dir: Path, number: str, email: str, at: float, now: float
) -> ScheduledSwitch:
    def apply(entries: list[ScheduledSwitch]) -> ScheduledSwitch:
        taken = {e.id for e in entries}
        entry_id = secrets.token_hex(3)
        while entry_id in taken:
            entry_id = secrets.token_hex(3)
        entry = ScheduledSwitch(entry_id, str(number), email, at, now)
        entries.append(entry)
        return entry

    return _mutate(backup_dir, apply)  # type: ignore[return-value]


def pending(backup_dir: Path) -> list[ScheduledSwitch]:
    return sorted(_read(backup_dir), key=lambda e: e.at)


def cancel(backup_dir: Path, entry_id: str) -> bool:
    def apply(entries: list[ScheduledSwitch]) -> bool:
        keep = [e for e in entries if e.id != entry_id]
        removed = len(keep) != len(entries)
        entries[:] = keep
        return removed

    return bool(_mutate(backup_dir, apply))


def claim_due(backup_dir: Path, now: float) -> ScheduledSwitch | None:
    """Remove and return the earliest entry whose time has come."""
    # Cheap unlocked peek: an engine ticks constantly, and most ticks have
    # nothing due, so don't take the lock (or rewrite the file) for them.
    if not any(e.at <= now for e in _read(backup_dir)):
        return None

    def apply(entries: list[ScheduledSwitch]) -> ScheduledSwitch | None:
        due = [e for e in entries if e.at <= now]
        if not due:
            return None
        first = min(due, key=lambda e: e.at)
        entries.remove(first)
        return first

    return _mutate(backup_dir, apply)  # type: ignore[return-value]


def next_due_at(backup_dir: Path) -> float | None:
    entries = _read(backup_dir)
    return min((e.at for e in entries), default=None)
