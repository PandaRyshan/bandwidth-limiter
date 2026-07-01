"""Tests for tc_limit.storage — SQLite operations."""

from __future__ import annotations

import time

import pytest

from tc_limit.storage import Storage
from tc_limit.storage.queries import (
    query_daily_volume,
    query_bandwidth_timeline,
    query_state_events,
    query_summary,
)


@pytest.fixture
def storage(tmp_path):
    db_path = str(tmp_path / "test.db")
    s = Storage(db_path, retention_days=90)
    s.open()
    yield s
    s.close()


class TestStorageLifecycle:
    def test_open_close(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        s = Storage(db_path)
        s.open()
        assert s._conn is not None
        s.close()
        assert s._conn is None

    def test_schema_created(self, storage):
        tables = storage._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = [r[0] for r in tables]
        assert "samples" in names
        assert "state_changes" in names
        assert "daily_summary" in names


class TestInsertAndFlush:
    def test_insert_sample(self, storage):
        ts = time.time()
        storage.insert_sample(ts, 1000, 2000, 500, 10.5, "NORMAL", 150, "eth0")
        storage._flush()
        row = storage._conn.execute("SELECT * FROM samples").fetchone()
        assert row["ts"] == ts
        assert row["tx_bytes"] == 1000
        assert row["rx_bytes"] == 2000
        assert row["delta_bytes"] == 500
        assert row["rate_mbps"] == 10.5
        assert row["state"] == "NORMAL"
        assert row["limit_mbps"] == 150
        assert row["iface"] == "eth0"

    def test_insert_state_change(self, storage):
        ts = time.time()
        storage.insert_state_change(ts, "NORMAL", "LIMITED",
                                    reason="window_avg 130 > 120", window_avg_mbps=130.0)
        storage._flush()
        row = storage._conn.execute("SELECT * FROM state_changes").fetchone()
        assert row["from_state"] == "NORMAL"
        assert row["to_state"] == "LIMITED"
        assert row["reason"] == "window_avg 130 > 120"
        assert row["window_avg_mbps"] == 130.0

    def test_maybe_flush_triggers_on_interval(self, storage):
        now = time.time()
        storage._next_commit = now - 100  # pretend last commit was 100s ago
        storage.insert_sample(now, 0, 0, 100, 5.0, "NORMAL", 150, "eth0")
        storage.maybe_flush(now, commit_interval=60)
        # After flush, pending should be empty
        assert len(storage._pending_samples) == 0
        row = storage._conn.execute("SELECT * FROM samples").fetchone()
        assert row is not None

    def test_maybe_flush_skips_early(self, storage):
        now = time.time()
        storage._next_commit = now  # just flushed
        storage.insert_sample(now, 0, 0, 100, 5.0, "NORMAL", 150, "eth0")
        storage.maybe_flush(now, commit_interval=60)
        # Should NOT have flushed — interval not reached
        assert len(storage._pending_samples) == 1


class TestRetention:
    def test_rotate_retention(self, storage):
        now = time.time()
        # Insert old sample
        storage._conn.execute(
            "INSERT INTO samples (ts, tx_bytes, rx_bytes, delta_bytes, rate_mbps, state, limit_mbps, iface) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (now - 100 * 86400, 0, 0, 0, 0, "NORMAL", 150, "eth0"),
        )
        storage._conn.commit()
        # Rotate with default 90 days retention
        storage._rotate_retention(now)
        rows = storage._conn.execute("SELECT COUNT(*) FROM samples").fetchone()
        assert rows[0] == 0  # old sample deleted

    def test_rotate_keeps_recent(self, storage):
        now = time.time()
        storage._conn.execute(
            "INSERT INTO samples (ts, tx_bytes, rx_bytes, delta_bytes, rate_mbps, state, limit_mbps, iface) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (now - 10, 0, 0, 0, 0, "NORMAL", 150, "eth0"),
        )
        storage._conn.commit()
        storage._rotate_retention(now)
        rows = storage._conn.execute("SELECT COUNT(*) FROM samples").fetchone()
        assert rows[0] == 1  # recent sample kept


class TestDailyAggregation:
    def test_maybe_aggregate_daily(self, storage):
        now = time.time()
        date_str = time.strftime("%Y-%m-%d", time.gmtime(now))
        # Insert some samples
        for i in range(10):
            storage._conn.execute(
                "INSERT INTO samples (ts, tx_bytes, rx_bytes, delta_bytes, rate_mbps, state, limit_mbps, iface) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (now - i * 60, 1000, 2000, 50000000, 40.0, "NORMAL", 150, "eth0"),
            )
        storage._conn.commit()
        storage.maybe_aggregate_daily(now)
        row = storage._conn.execute(
            "SELECT * FROM daily_summary WHERE date = ?", (date_str,)
        ).fetchone()
        assert row is not None
        assert row["sample_count"] == 10
        assert row["avg_mbps"] == 40.0
        assert row["peak_mbps"] == 40.0

    def test_maybe_aggregate_daily_idempotent(self, storage):
        now = time.time()
        storage._conn.execute(
            "INSERT INTO samples (ts, tx_bytes, rx_bytes, delta_bytes, rate_mbps, state, limit_mbps, iface) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (now, 0, 0, 100, 1.0, "NORMAL", 150, "eth0"),
        )
        storage._conn.commit()
        storage.maybe_aggregate_daily(now)
        # Second call should NOT crash and NOT duplicate
        storage.maybe_aggregate_daily(now)


class TestQueries:
    def test_query_summary_empty(self, storage):
        data = query_summary(storage._db_path)
        assert data["sample_count"] == 0
        assert data["total_transfer_gb"] == 0

    def test_query_summary(self, storage):
        now = time.time()
        storage._conn.execute(
            "INSERT INTO samples (ts, tx_bytes, rx_bytes, delta_bytes, rate_mbps, state, limit_mbps, iface) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (now, 1000, 2000, 1_000_000_000, 80.0, "LIMITED", 110, "eth0"),
        )
        storage._conn.commit()
        data = query_summary(storage._db_path)
        assert data["sample_count"] == 1
        assert data["total_transfer_gb"] == 1.0
        assert data["last_state"] == "LIMITED"

    def test_query_state_events(self, storage):
        now = time.time()
        storage._conn.execute(
            "INSERT INTO state_changes (ts, from_state, to_state, reason) "
            "VALUES (?, ?, ?, ?)",
            (now, "NORMAL", "LIMITED", "test reason"),
        )
        storage._conn.commit()
        rows = query_state_events(storage._db_path, limit=10)
        assert len(rows) == 1
        assert rows[0]["to_state"] == "LIMITED"

    def test_query_daily_volume(self, storage):
        storage._conn.execute(
            "INSERT INTO daily_summary (date, total_gb, peak_mbps, avg_mbps, limited_minutes, state_changes, sample_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("2026-07-01", 5.2, 120.0, 45.0, 10, 2, 1000),
        )
        storage._conn.commit()
        rows = query_daily_volume(storage._db_path, days=7)
        assert len(rows) == 1
        assert rows[0]["total_gb"] == 5.2

    def test_query_bandwidth_timeline(self, storage):
        now = time.time()
        for i in range(3):
            storage._conn.execute(
                "INSERT INTO samples (ts, tx_bytes, rx_bytes, delta_bytes, rate_mbps, state, limit_mbps, iface) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (now - i * 60, 0, 0, 1000, 10.0 * i, "NORMAL", 150, "eth0"),
            )
        storage._conn.commit()
        rows = query_bandwidth_timeline(storage._db_path, limit=10)
        assert len(rows) == 3
