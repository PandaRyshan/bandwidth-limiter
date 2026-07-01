"""Query functions for storage — exported for the analyzer and CLI."""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def query_daily_volume(db_path: str, days: int = 7) -> List[Dict[str, Any]]:
    """Return daily traffic volume for the last *days* days."""
    conn = _connect(db_path)
    rows = conn.execute("""
        SELECT date, total_gb, avg_mbps, peak_mbps
        FROM daily_summary
        ORDER BY date DESC
        LIMIT ?
    """, (days,)).fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]


def query_bandwidth_timeline(
    db_path: str, since: Optional[str] = None, limit: int = 500,
) -> List[Dict[str, Any]]:
    """Return bandwidth samples (aggregated per commit interval)."""
    conn = _connect(db_path)
    if since:
        rows = conn.execute("""
            SELECT ts, rate_mbps, state, limit_mbps
            FROM samples
            WHERE ts >= ?
            ORDER BY ts ASC
            LIMIT ?
        """, (since, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT ts, rate_mbps, state, limit_mbps
            FROM samples
            ORDER BY ts DESC
            LIMIT ?
        """, (limit,)).fetchall()
    conn.close()
    result = [dict(r) for r in rows]
    return list(reversed(result)) if not since else result


def query_state_events(db_path: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Return recent state change events."""
    conn = _connect(db_path)
    rows = conn.execute("""
        SELECT ts, from_state, to_state, reason, window_avg_mbps
        FROM state_changes
        ORDER BY ts DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def query_summary(db_path: str) -> Dict[str, Any]:
    """Return aggregate summary stats."""
    conn = _connect(db_path)
    total = conn.execute(
        "SELECT COUNT(*) AS cnt, COALESCE(SUM(delta_bytes),0) AS total_bytes FROM samples"
    ).fetchone()
    last = conn.execute(
        "SELECT ts, rate_mbps, state FROM samples ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    limited_minutes = conn.execute(
        "SELECT COALESCE(SUM(limited_minutes), 0) FROM daily_summary"
    ).fetchone()
    state_changes = conn.execute(
        "SELECT COUNT(*) FROM state_changes"
    ).fetchone()
    conn.close()
    return {
        "sample_count": total["cnt"],
        "total_transfer_gb": round(total["total_bytes"] / 1e9, 2),
        "last_sample_ts": last["ts"] if last else None,
        "last_rate_mbps": last["rate_mbps"] if last else None,
        "last_state": last["state"] if last else None,
        "total_limited_minutes": round(limited_minutes[0], 1),
        "total_state_changes": state_changes[0],
    }
