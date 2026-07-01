"""SQLite storage for bandwidth metrics (Phase 2)."""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── Schema ──────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS samples (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    tx_bytes     INTEGER NOT NULL,
    rx_bytes     INTEGER NOT NULL,
    delta_bytes  INTEGER NOT NULL,
    rate_mbps    REAL NOT NULL,
    state        TEXT NOT NULL,
    limit_mbps   INTEGER NOT NULL,
    iface        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);

CREATE TABLE IF NOT EXISTS state_changes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               REAL NOT NULL,
    from_state       TEXT NOT NULL,
    to_state         TEXT NOT NULL,
    reason           TEXT,
    window_avg_mbps  REAL
);

CREATE INDEX IF NOT EXISTS idx_state_changes_ts ON state_changes(ts);

CREATE TABLE IF NOT EXISTS daily_summary (
    date             TEXT PRIMARY KEY,
    total_gb         REAL NOT NULL,
    peak_mbps        REAL NOT NULL,
    avg_mbps         REAL NOT NULL,
    limited_minutes  INTEGER NOT NULL,
    state_changes    INTEGER NOT NULL,
    sample_count     INTEGER NOT NULL
);
"""


# ── Database ────────────────────────────────────────────────────────────────


class Storage:
    """SQLite-backed storage for bandwidth metrics."""

    def __init__(self, db_path: str, retention_days: int = 90) -> None:
        self._db_path = db_path
        self._retention_days = retention_days
        self._conn: Optional[sqlite3.Connection] = None
        self._next_commit: float = 0.0
        self._pending_samples: list[tuple] = []
        self._pending_changes: list[tuple] = []
        self._last_rotation_ts: float = 0.0
        # Track state for daily summary aggregation
        self._sample_count: int = 0
        self._total_bytes: int = 0
        self._peak_mbps: float = 0.0
        self._avg_mbps_sum: float = 0.0
        self._limited_minutes: float = 0.0
        self._state_changes_count: int = 0
        self._last_aggregation_ts: float = 0.0

    # ── Lifecycle ──

    def open(self) -> None:
        """Open the database and run migrations."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()
        self._next_commit = time.time()
        self._last_rotation_ts = time.time()
        logger.info("Storage opened: %s (retention=%dd)", self._db_path, self._retention_days)

    def close(self) -> None:
        """Flush pending data and close the database."""
        if self._conn:
            self._flush()
            self._conn.close()
            self._conn = None
            logger.info("Storage closed.")

    # ── Insert ──

    def insert_sample(
        self, ts: float, tx_bytes: int, rx_bytes: int,
        delta_bytes: int, rate_mbps: float,
        state: str, limit_mbps: int, iface: str,
    ) -> None:
        """Queue a sample row for batch commit."""
        self._pending_samples.append((
            ts, tx_bytes, rx_bytes, delta_bytes, rate_mbps,
            state, limit_mbps, iface,
        ))
        # Track for daily aggregation
        self._sample_count += 1
        self._total_bytes += delta_bytes
        if rate_mbps > self._peak_mbps:
            self._peak_mbps = rate_mbps
        self._avg_mbps_sum += rate_mbps

    def insert_state_change(
        self, ts: float, from_state: str, to_state: str,
        reason: str = "", window_avg_mbps: Optional[float] = None,
    ) -> None:
        """Queue a state change row."""
        self._pending_changes.append((
            ts, from_state, to_state, reason, window_avg_mbps,
        ))
        self._state_changes_count += 1

    # ── Flush ──

    def maybe_flush(self, now: float, commit_interval: int) -> None:
        """Flush pending data if *commit_interval* seconds have passed."""
        if (now - self._next_commit) >= commit_interval:
            self._flush()
            self._next_commit = now
        # Also rotate retention daily
        if (now - self._last_rotation_ts) >= 86400:
            self._rotate_retention(now)
            self._last_rotation_ts = now

    def _flush(self) -> None:
        """Write all pending rows to the database."""
        if not self._conn:
            return
        if self._pending_samples:
            self._conn.executemany(
                "INSERT INTO samples (ts, tx_bytes, rx_bytes, delta_bytes, rate_mbps, state, limit_mbps, iface) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                self._pending_samples,
            )
            self._pending_samples.clear()
        if self._pending_changes:
            self._conn.executemany(
                "INSERT INTO state_changes (ts, from_state, to_state, reason, window_avg_mbps) "
                "VALUES (?, ?, ?, ?, ?)",
                self._pending_changes,
            )
            self._pending_changes.clear()
        self._conn.commit()

    # ── Retention ──

    def _rotate_retention(self, now: float) -> None:
        """Delete samples older than *retention_days*."""
        if not self._conn:
            return
        cutoff = now - (self._retention_days * 86400)
        self._conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        self._conn.commit()
        logger.debug("Retention rotated: deleted samples older than %.0f days ago",
                     self._retention_days)

    # ── Daily aggregation ──

    def maybe_aggregate_daily(self, now: float) -> None:
        """Aggregate today's samples into `daily_summary` if not yet done."""
        if not self._conn:
            return
        date_str = time.strftime("%Y-%m-%d", time.gmtime(now))
        row = self._conn.execute(
            "SELECT 1 FROM daily_summary WHERE date = ?", (date_str,)
        ).fetchone()
        if row:
            return  # already aggregated today

        # Aggregate today's samples
        # Compute start-of-day as Unix timestamp (UTC)
        tm = time.gmtime(now)
        today_start = time.mktime((tm.tm_year, tm.tm_mon, tm.tm_mday, 0, 0, 0, 0, 0, 0))
        agg = self._conn.execute("""
            SELECT
                COUNT(*) AS sample_count,
                COALESCE(SUM(delta_bytes), 0) AS total_bytes,
                COALESCE(MAX(rate_mbps), 0) AS peak_mbps,
                COALESCE(AVG(rate_mbps), 0) AS avg_mbps,
                COALESCE(SUM(CASE WHEN state = 'LIMITED' THEN 1 ELSE 0 END), 0) AS limited_samples
            FROM samples
            WHERE ts >= ?
        """, (today_start,)).fetchone()

        if not agg or agg[0] == 0:
            return  # no samples today

        # Count state changes today
        sc = self._conn.execute(
            "SELECT COUNT(*) FROM state_changes WHERE ts >= ?", (today_start,)
        ).fetchone()
        state_changes = sc[0] if sc else 0

        # limited_samples → minutes: each sample = commit_interval seconds
        limited_minutes = (agg[4] * 60) / 3600  # rough: samples * commit_interval / 60

        self._conn.execute(
            "INSERT OR REPLACE INTO daily_summary "
            "(date, total_gb, peak_mbps, avg_mbps, limited_minutes, state_changes, sample_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                date_str,
                agg[1] / 1e9,   # bytes → GB
                agg[2],
                agg[3],
                round(limited_minutes, 1),
                state_changes,
                agg[0],
            ),
        )
        self._conn.commit()
        logger.debug("Daily summary aggregated for %s", date_str)
