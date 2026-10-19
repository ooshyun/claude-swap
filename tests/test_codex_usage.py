"""Read-only Codex usage: window mapping, cache TTL, session-log fallback."""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import codex_usage
from claude_swap.codex_usage import read_codex_usage

NOW = 1792700000.0
URL = "https://chatgpt.com/backend-api/wham/usage"


def _login(home: Path, mode: str = "chatgpt") -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(json.dumps({
        "auth_mode": mode,
        "OPENAI_API_KEY": None,
        "tokens": {"access_token": "tok", "account_id": "acct-1",
                   "refresh_token": "rt", "id_token": "id"},
    }))


def _body(primary=(27, 604800), secondary=None, plan="plus") -> bytes:
    def win(spec):
        if spec is None:
            return None
        pct, length = spec
        return {"used_percent": pct, "limit_window_seconds": length,
                "reset_after_seconds": 1000, "reset_at": int(NOW) + 1000}
    return json.dumps({"plan_type": plan, "rate_limit": {
        "allowed": True, "limit_reached": False,
        "primary_window": win(primary), "secondary_window": win(secondary),
    }}).encode()


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Net:
    """urlopen stand-in that records every request."""

    def __init__(self, body: bytes | None = None, status: int | None = None):
        self.body = body
        self.status = status
        self.requests: list = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if self.status is not None:
            raise urllib.error.HTTPError(
                req.full_url, self.status, "err", {}, io.BytesIO(b"")
            )
        return _Resp(self.body)


def _session_log(home: Path, stamp: str = "2026-10-17T14:35:22.416Z") -> None:
    day = datetime.fromtimestamp(NOW).date()
    folder = home / "sessions" / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
    folder.mkdir(parents=True)
    old = {"timestamp": "2026-10-17T10:00:00.000Z", "payload": {
        "type": "token_count", "rate_limits": {
            "primary": {"used_percent": 5.0, "window_minutes": 10080,
                        "resets_at": 1792710480}}}}
    new = {"timestamp": stamp, "payload": {
        "type": "token_count", "rate_limits": {
            "primary": {"used_percent": 31.0, "window_minutes": 10080,
                        "resets_at": 1792710480},
            "secondary": None, "plan_type": "unknown"}}}
    (folder / "rollout-a.jsonl").write_text(
        "\n".join(json.dumps(x) for x in (old, {"payload": {"type": "x"}}, new)) + "\n"
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "codex"
    _login(h)
    return h


def _read(tmp_path, home, net, **kw):
    kw.setdefault("now", NOW)
    with patch("urllib.request.urlopen", net):
        return read_codex_usage(tmp_path / "cache", codex_home=home, **kw)


def test_window_mapping(tmp_path, home):
    net = _Net(_body(primary=(27, 604800), secondary=(40, 18000)))
    usage = _read(tmp_path, home, net)
    assert usage.plan == "plus"
    assert usage.source == "live"
    good = usage.entry.last_good
    assert good["seven_day"]["pct"] == 27.0
    assert good["five_hour"]["pct"] == 40.0
    assert good["seven_day"]["resets_at"].endswith("Z")


def test_other_window_length_goes_to_scoped(tmp_path, home):
    net = _Net(_body(primary=(12, 3600 * 3), secondary=(8, 86400 * 2)))
    good = _read(tmp_path, home, net).entry.last_good
    assert [(w["name"], w["pct"]) for w in good["scoped"]] == [("3h", 12.0), ("2d", 8.0)]
    assert "five_hour" not in good


@pytest.mark.parametrize("mode", ["apikey", None])
def test_not_configured_returns_none(tmp_path, mode):
    h = tmp_path / "codex"
    if mode:
        _login(h, mode)
    net = _Net(_body())
    assert _read(tmp_path, h, net) is None
    assert net.requests == []


def test_live_success_writes_cache_and_second_call_skips_network(tmp_path, home):
    net = _Net(_body())
    first = _read(tmp_path, home, net)
    cached = json.loads((tmp_path / "cache" / "codex_usage.json").read_text())
    assert cached["plan"] == "plus"
    assert cached["fetchedAt"] == NOW
    second = _read(tmp_path, home, net, now=NOW + 100)
    assert len(net.requests) == 1
    assert second.entry.last_good == first.entry.last_good
    assert second.entry.age_s == 100
    _read(tmp_path, home, net, now=NOW + 200)
    assert len(net.requests) == 2


def test_401_falls_back_to_session_log_with_only_the_usage_get(tmp_path, home):
    _session_log(home)
    auth_before = (home / "auth.json").read_text()
    net = _Net(status=401)
    usage = _read(tmp_path, home, net)
    assert usage.source == "session-log"
    expected = datetime(2026, 10, 17, 14, 35, 22, 416000, tzinfo=timezone.utc).timestamp()
    assert usage.entry.fetched_at == expected
    assert usage.entry.last_error == "http-401"
    assert usage.entry.last_good["seven_day"]["pct"] == 31.0
    assert usage.plan == ""
    assert [(r.full_url, r.get_method()) for r in net.requests] == [(URL, "GET")]
    assert net.requests[0].get_header("Authorization") == "Bearer tok"
    assert net.requests[0].get_header("Chatgpt-account-id") == "acct-1"
    again = _read(tmp_path, home, net, now=NOW + 30)
    assert len(net.requests) == 1  # inside the 60 s retry gate
    assert again.source == "session-log"
    assert (home / "auth.json").read_text() == auth_before


def test_failure_keeps_newer_cached_live_data(tmp_path, home):
    _session_log(home)
    _read(tmp_path, home, _Net(_body(primary=(50, 604800))), now=NOW)
    net = _Net(status=500)
    usage = _read(tmp_path, home, net, now=NOW + 400)
    assert usage.source == "live"
    assert usage.entry.last_good["seven_day"]["pct"] == 50.0
    assert usage.entry.last_error == "http-500"


def test_fetch_false_never_calls_urlopen(tmp_path, home):
    net = _Net(_body())
    assert _read(tmp_path, home, net, fetch=False) is None
    assert net.requests == []
    _session_log(home)
    usage = _read(tmp_path, home, net, fetch=False)
    assert usage.source == "session-log"
    assert net.requests == []


def test_codex_home_env_is_respected(tmp_path, home, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(home))
    net = _Net(_body())
    with patch("urllib.request.urlopen", net):
        usage = read_codex_usage(tmp_path / "cache", now=NOW)
    assert usage is not None and usage.entry.last_good["seven_day"]["pct"] == 27.0


def test_never_raises(tmp_path, home):
    with patch.object(codex_usage, "_fetch_live", side_effect=RuntimeError("boom")):
        usage = read_codex_usage(tmp_path / "cache", codex_home=home, now=NOW)
    assert usage.entry.last_good is None
    assert usage.entry.last_error == "RuntimeError"
