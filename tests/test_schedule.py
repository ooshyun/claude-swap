"""Tests for scheduled switches (schedule.py)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from claude_swap import schedule
from claude_swap.exceptions import ClaudeSwitchError

NOW = datetime(2026, 11, 17, 12, 0, 0)  # naive = local


def _local(*args) -> float:
    return datetime(*args).timestamp()


class TestParseWhen:
    def test_clock_time_still_ahead_is_today(self):
        assert schedule.parse_when("23:30", NOW) == _local(2026, 11, 17, 23, 30)

    def test_clock_time_already_passed_is_tomorrow(self):
        assert schedule.parse_when("09:15", NOW) == _local(2026, 11, 18, 9, 15)

    def test_clock_time_equal_to_now_is_tomorrow(self):
        assert schedule.parse_when("12:00", NOW) == _local(2026, 11, 18, 12, 0)

    def test_date_and_time(self):
        assert schedule.parse_when("2026-11-18 09:00", NOW) == _local(2026, 11, 18, 9, 0)

    def test_iso_naive_is_local(self):
        assert schedule.parse_when("2026-11-18T09:00", NOW) == _local(2026, 11, 18, 9, 0)

    def test_iso_with_offset(self):
        expected = datetime(2026, 11, 18, 0, 0, tzinfo=timezone.utc).timestamp()
        assert schedule.parse_when("2026-11-18T09:00+09:00", NOW) == expected

    @pytest.mark.parametrize(
        "text, seconds",
        [("+90m", 5400), ("+2h", 7200), ("+1h30m", 5400)],
    )
    def test_relative(self, text, seconds):
        assert schedule.parse_when(text, NOW) == NOW.timestamp() + seconds

    @pytest.mark.parametrize("text", ["2026-11-16 09:00", "2026-11-17T11:59"])
    def test_past_absolute_is_rejected(self, text):
        with pytest.raises(ClaudeSwitchError, match="past"):
            schedule.parse_when(text, NOW)

    @pytest.mark.parametrize(
        "text", ["", "tomorrow", "25:00", "12:60", "+", "+0m", "+h", "+90", "2026-13-01 09:00"]
    )
    def test_garbage_is_rejected(self, text):
        with pytest.raises(ClaudeSwitchError):
            schedule.parse_when(text, NOW)


class TestStore:
    def test_add_pending_cancel_round_trip(self, tmp_path):
        later = schedule.add(tmp_path, "2", "b@example.com", 2000.0, now=100.0)
        sooner = schedule.add(tmp_path, "3", "c@example.com", 1500.0, now=101.0)

        assert [e.id for e in schedule.pending(tmp_path)] == [sooner.id, later.id]
        assert later.number == "2" and later.email == "b@example.com"
        assert later.created_at == 100.0
        assert len(later.id) == 6

        assert schedule.cancel(tmp_path, sooner.id) is True
        assert [e.id for e in schedule.pending(tmp_path)] == [later.id]
        assert schedule.cancel(tmp_path, sooner.id) is False

    def test_file_shape(self, tmp_path):
        entry = schedule.add(tmp_path, "2", "b@example.com", 2000.0, now=100.0)
        raw = json.loads((tmp_path / "autoswitch_schedule.json").read_text())
        assert raw == {
            "schemaVersion": 1,
            "pending": [{
                "id": entry.id, "number": "2", "email": "b@example.com",
                "at": 2000.0, "createdAt": 100.0,
            }],
        }

    def test_claim_due_takes_earliest_due_only(self, tmp_path):
        first = schedule.add(tmp_path, "2", "b@example.com", 1000.0, now=1.0)
        second = schedule.add(tmp_path, "3", "c@example.com", 1100.0, now=1.0)
        schedule.add(tmp_path, "4", "d@example.com", 5000.0, now=1.0)

        assert schedule.claim_due(tmp_path, 1200.0) == first
        assert schedule.claim_due(tmp_path, 1200.0) == second
        assert schedule.claim_due(tmp_path, 1200.0) is None
        assert len(schedule.pending(tmp_path)) == 1

    def test_claim_due_with_nothing_due_or_no_file(self, tmp_path):
        assert schedule.claim_due(tmp_path, 10.0) is None
        schedule.add(tmp_path, "2", "b@example.com", 1000.0, now=1.0)
        assert schedule.claim_due(tmp_path, 999.0) is None

    def test_next_due_at(self, tmp_path):
        assert schedule.next_due_at(tmp_path) is None
        schedule.add(tmp_path, "2", "b@example.com", 3000.0, now=1.0)
        schedule.add(tmp_path, "3", "c@example.com", 2000.0, now=1.0)
        assert schedule.next_due_at(tmp_path) == 2000.0

    @pytest.mark.parametrize("content", ["{not json", "[]", '{"pending": 5}', "\xff"])
    def test_corrupt_file_is_empty(self, tmp_path, content):
        (tmp_path / "autoswitch_schedule.json").write_text(content, encoding="latin-1")
        assert schedule.pending(tmp_path) == []
        assert schedule.claim_due(tmp_path, 1e12) is None

    def test_malformed_entries_are_skipped(self, tmp_path):
        good = {"id": "aaaaaa", "number": "2", "email": "b@example.com",
                "at": 5.0, "createdAt": 1.0}
        (tmp_path / "autoswitch_schedule.json").write_text(json.dumps({
            "schemaVersion": 1,
            "pending": [
                good, "junk", {"id": "x"}, {**good, "id": "bbbbbb", "at": "soon"},
                {**good, "id": "cccccc", "number": ""},
            ],
        }))
        assert [e.id for e in schedule.pending(tmp_path)] == ["aaaaaa"]
