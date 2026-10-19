"""Read-only OpenAI Codex CLI usage, shown beside the Claude accounts.

Codex is not a Claude account: it is never a switch target and never enters
the usage store, autoswitch, or the sequence file. This module only *reads*
the ChatGPT-plan login Codex already keeps in ``$CODEX_HOME/auth.json`` and
asks the same ``wham/usage`` endpoint Codex's own status line uses.

It must never refresh, rotate, or write Codex tokens, and never calls an auth
endpoint: the refresh token is single-use, so a refresh here would log Codex
out. An expired or rejected access token just falls back to the rate-limit
snapshot Codex itself writes into its session logs.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claude_swap import oauth, poll_policy
from claude_swap.settings import atomic_write_json
from claude_swap.usage_store import UsageEntry

_logger = logging.getLogger(__name__)

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CACHE_FILE = "codex_usage.json"
#: After a failed live attempt, wait this long before the next one.
RETRY_AFTER_FAILURE_S = 60.0
#: Only recent day directories are scanned for the session-log fallback.
_SESSION_LOOKBACK_DAYS = 14
_SESSION_FILES_TRIED = 5

_FIVE_HOUR_S = 5 * 3600
_SEVEN_DAY_S = 7 * 86400


@dataclass(frozen=True)
class CodexUsage:
    """Codex's usage as one display unit.

    ``entry.last_good`` is in the ``oauth.build_usage_result`` shape so the
    existing renderers work unchanged. ``source`` is ``"live"`` (the wham
    endpoint) or ``"session-log"`` (the last rate-limit line Codex logged).
    """

    plan: str
    entry: UsageEntry
    source: str


def _codex_home(codex_home: Path | None) -> Path:
    if codex_home is not None:
        return codex_home
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _read_login(home: Path) -> tuple[str, str] | None:
    """``(access_token, account_id)`` of a ChatGPT-plan login, else None."""
    try:
        data = json.loads((home / "auth.json").read_text())
        if data.get("auth_mode") != "chatgpt":
            return None
        tokens = data.get("tokens") or {}
        access = tokens.get("access_token")
        if not access:
            return None
        return access, tokens.get("account_id") or ""
    except (OSError, ValueError, AttributeError):
        return None


def _iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _window_name(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}m"


def _build_usage(windows: list[tuple[float, int, float]]) -> dict:
    """Normalize ``(pct, length_s, reset_epoch)`` windows by window length."""
    result: dict = {}
    scoped: list[dict] = []
    for pct, length_s, reset_epoch in windows:
        resets_at = _iso(reset_epoch)
        countdown, clock = oauth.format_reset(resets_at)
        entry = {
            "pct": float(pct),
            "resets_at": resets_at,
            "countdown": countdown,
            "clock": clock,
        }
        key = {_FIVE_HOUR_S: "five_hour", _SEVEN_DAY_S: "seven_day"}.get(length_s)
        if key is not None and key not in result:
            result[key] = entry
        else:
            scoped.append({"name": _window_name(length_s), **entry})
    if scoped:
        result["scoped"] = scoped
    return result


def _fetch_live(access_token: str, account_id: str, now: float) -> tuple[str, dict]:
    """GET the wham usage endpoint; the only request this module ever makes."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "claude-swap/1.0",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    req = urllib.request.Request(USAGE_URL, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=5) as resp:
        body = json.loads(resp.read().decode())
    rate = body.get("rate_limit") or {}
    windows = []
    for key in ("primary_window", "secondary_window"):
        w = rate.get(key)
        if not isinstance(w, dict) or w.get("used_percent") is None:
            continue
        length = int(w["limit_window_seconds"])
        reset = w.get("reset_at")
        if reset is None:
            reset = now + float(w.get("reset_after_seconds") or 0)
        windows.append((float(w["used_percent"]), length, float(reset)))
    return str(body.get("plan_type") or ""), _build_usage(windows)


def _session_log_usage(home: Path, now: float) -> tuple[str, dict, float] | None:
    """``(plan, usage, fetched_at)`` from Codex's newest recent session log."""
    sessions = home / "sessions"
    today = datetime.fromtimestamp(now).date()
    files: list[tuple[float, Path]] = []
    for back in range(_SESSION_LOOKBACK_DAYS + 1):
        day = today - timedelta(days=back)
        folder = sessions / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
        try:
            for p in folder.glob("rollout-*.jsonl"):
                files.append((p.stat().st_mtime, p))
        except OSError:
            continue
    files.sort(reverse=True)
    for _mtime, path in files[:_SESSION_FILES_TRIED]:
        found = _last_rate_limits(path)
        if found is None:
            continue
        timestamp, limits = found
        windows = []
        for key in ("primary", "secondary"):
            w = limits.get(key)
            if not isinstance(w, dict) or w.get("used_percent") is None:
                continue
            length = int(float(w["window_minutes"]) * 60)
            windows.append((float(w["used_percent"]), length, float(w["resets_at"])))
        plan = str(limits.get("plan_type") or "")
        return ("" if plan == "unknown" else plan), _build_usage(windows), timestamp
    return None


def _last_rate_limits(path: Path) -> tuple[float, dict] | None:
    last: tuple[float, dict] | None = None
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"rate_limits"' not in line or "token_count" not in line:
                    continue
                try:
                    obj = json.loads(line)
                    limits = obj["payload"]["rate_limits"]
                    if not isinstance(limits, dict):
                        continue
                    stamp = datetime.fromisoformat(
                        obj["timestamp"].replace("Z", "+00:00")
                    ).timestamp()
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
                last = (stamp, limits)
    except OSError:
        return None
    return last


def _load_cache(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _to_result(cache: dict, now: float) -> CodexUsage:
    fetched_at = cache.get("fetchedAt")
    entry = UsageEntry(
        last_good=cache.get("lastGood"),
        fetched_at=fetched_at,
        age_s=max(0.0, now - fetched_at) if fetched_at is not None else None,
        last_attempt_at=cache.get("lastAttemptAt"),
        last_error=cache.get("lastError"),
    )
    return CodexUsage(
        plan=cache.get("plan") or "", entry=entry, source=cache.get("source") or "live"
    )


def read_codex_usage(
    cache_dir: Path,
    *,
    fetch: bool = True,
    codex_home: Path | None = None,
    now: float | None = None,
) -> CodexUsage | None:
    """Codex's current usage, or None when Codex is not logged in.

    Served from ``<cache_dir>/codex_usage.json`` while it is younger than
    ``poll_policy.SERVE_TTL_S``; otherwise (``fetch=True`` only, and not within
    ``RETRY_AFTER_FAILURE_S`` of a failed attempt) the wham endpoint is asked.
    A failed live call falls back to the newer of the cached data and the last
    rate-limit line in Codex's session logs. ``fetch=False`` never touches the
    network. Never raises.
    """
    now = time.time() if now is None else now
    cache_path = cache_dir / CACHE_FILE
    cache = _load_cache(cache_path)
    try:
        return _read(cache_path, cache, fetch, _codex_home(codex_home), now)
    except Exception as e:  # noqa: BLE001 - display-only data must not break callers
        _logger.debug("Codex usage read failed: %r", e)
        if cache is None or _read_login(_codex_home(codex_home)) is None:
            return None
        return _to_result(cache, now)


def _read(
    cache_path: Path, cache: dict | None, fetch: bool, home: Path, now: float
) -> CodexUsage | None:
    login = _read_login(home)
    if login is None:
        # Logged out (or switched to an API key): a leftover cache would show
        # a plan the user no longer has.
        return None

    fetched = (cache or {}).get("fetchedAt")
    fresh = fetched is not None and now - fetched < poll_policy.SERVE_TTL_S
    attempt = (cache or {}).get("lastAttemptAt")
    failed = bool((cache or {}).get("lastError"))
    backing_off = (
        failed and attempt is not None and now - attempt < RETRY_AFTER_FAILURE_S
    )

    if cache is not None and (fresh or not fetch or backing_off):
        return _to_result(cache, now)

    if fetch:
        try:
            plan, usage = _fetch_live(login[0], login[1], now)
        except Exception as e:  # noqa: BLE001
            kind, _ = oauth._classify_usage_error(e)
            _logger.debug("Codex live usage failed: %s (%r)", kind, e)
            return _fallback(cache_path, cache, home, now, kind)
        new = {
            "plan": plan,
            "lastGood": usage,
            "fetchedAt": now,
            "source": "live",
            "lastAttemptAt": now,
            "lastError": None,
        }
        atomic_write_json(cache_path, new)
        return _to_result(new, now)

    return _fallback(cache_path, cache, home, now, None)


def _fallback(
    cache_path: Path, cache: dict | None, home: Path, now: float, error: str | None
) -> CodexUsage | None:
    """Pick the newer of cached data and the session log; record the error."""
    chosen = dict(cache) if cache else None
    log = _session_log_usage(home, now)
    if log is not None:
        plan, usage, stamp = log
        cached_at = (cache or {}).get("fetchedAt")
        if cached_at is None or stamp > cached_at:
            chosen = {
                "plan": plan or (cache or {}).get("plan") or "",
                "lastGood": usage,
                "fetchedAt": stamp,
                "source": "session-log",
            }
    if chosen is None:
        if error is None:
            return None
        chosen = {"plan": "", "lastGood": None, "fetchedAt": None, "source": "live"}
    if error is not None:
        chosen["lastAttemptAt"] = now
        chosen["lastError"] = error
        atomic_write_json(cache_path, chosen)
    else:
        chosen.setdefault("lastAttemptAt", None)
        chosen.setdefault("lastError", None)
    return _to_result(chosen, now)
