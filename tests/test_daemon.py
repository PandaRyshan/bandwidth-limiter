"""Tests for tc_limit.daemon state machine and persistence."""

from __future__ import annotations

import json
import os
import signal
import time
from unittest import mock

import pytest

from tc_limit.config import Config
from tc_limit.daemon import (
    Daemon, save_state, load_state, acquire_lock, release_lock,
    STATE_NORMAL, STATE_LIMITED,
)
from tc_limit.sampler import RingBuffer


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_config(**overrides) -> Config:
    """Build a Config with default test values, overridable by keyword."""
    from tc_limit.config import load_config
    cfg = load_config()
    for key, val in overrides.items():
        parts = key.split(".")
        target = cfg
        for part in parts[:-1]:
            target = getattr(target, part)
        setattr(target, parts[-1], val)
    # Recompute derived
    cfg.buf_size = cfg.window.duration * 60 // cfg.window.interval
    cfg.threshold_bps = cfg.limits.threshold * 125_000
    cfg.window_seconds = cfg.window.duration * 60
    cfg.cooldown_seconds = cfg.cooldown * 60
    return cfg


# ── State Persistence ──────────────────────────────────────────────────────


class TestSaveState:
    def test_save_and_load(self, tmp_path):
        sf = str(tmp_path / "state.json")
        save_state(sf, STATE_NORMAL, 150, 120, 42.5, 180, None, 100)
        data = load_state(sf)
        assert data["state"] == STATE_NORMAL
        assert data["current_rate_mbps"] == 150
        assert data["threshold_mbps"] == 120
        assert data["window_avg_mbps"] == 42.5
        assert data["cooldown_start"] is None
        assert data["sample_count"] == 100

    def test_save_and_load_limited(self, tmp_path):
        sf = str(tmp_path / "state.json")
        now = time.time()
        save_state(sf, STATE_LIMITED, 110, 120, 130.0, 180, now, 500)
        data = load_state(sf)
        # Cooldown started recently → still LIMITED
        assert data["state"] == STATE_LIMITED
        assert data["cooldown_start"] == now

    def test_load_expired_cooldown(self, tmp_path):
        sf = str(tmp_path / "state.json")
        # Cooldown started 4 minutes ago, cooldown=3 min → expired
        past = time.time() - 240
        save_state(sf, STATE_LIMITED, 110, 120, 130.0, 180, past, 500)
        data = load_state(sf)
        assert data["state"] == STATE_NORMAL
        assert data["cooldown_start"] is None

    def test_load_missing_file(self, tmp_path):
        data = load_state(str(tmp_path / "nonexistent.json"))
        assert data == {}


# ── Lock ───────────────────────────────────────────────────────────────────


class TestLock:
    def test_acquire_and_release(self, tmp_path):
        pf = str(tmp_path / "daemon.pid")
        fd = acquire_lock(pf)
        assert os.path.exists(pf)
        with open(pf, "r") as fh:
            assert int(fh.read().strip()) == os.getpid()
        release_lock(fd, pf)
        assert not os.path.exists(pf)

    def test_acquire_double_fails(self, tmp_path):
        pf = str(tmp_path / "daemon.pid")
        fd = acquire_lock(pf)
        try:
            with pytest.raises(SystemExit):
                acquire_lock(pf)
        finally:
            release_lock(fd, pf)


# ── Daemon State Machine ──────────────────────────────────────────────────


class TestDaemonStateMachine:
    """Test the state machine logic in isolation (mock subprocess + /sys)."""

    def _make_daemon(self, tmp_path, buf_size=6, higher=150, lower=110, threshold=120,
                     window_duration=1, interval=10, cooldown=3):
        """Create a Daemon with mocked counters."""
        cfg = _make_config(
            **{
                "limits.higher": higher,
                "limits.lower": lower,
                "limits.threshold": threshold,
                "window.duration": window_duration,
                "window.interval": interval,
                "cooldown": cooldown,
                "runtime.state_file": str(tmp_path / "state.json"),
                "runtime.pid_file": str(tmp_path / "daemon.pid"),
                "runtime.dry_run": True,
                "network.interface": "eth0",
            }
        )
        daemon = Daemon(cfg)
        # Override interface detection
        daemon.iface = "eth0"
        return daemon

    def test_normal_to_limited(self, tmp_path):
        """When window avg exceeds threshold, transition to LIMITED."""
        d = self._make_daemon(tmp_path, buf_size=3, threshold=100, higher=150, lower=80)

        # Simulate: buffer filled, sum exceeds threshold
        # interval=10s, window=1min → buf_size=6, threshold_bps=100*125000=12,500,000
        # window_seconds=60, threshold_bytes=12,500,000*60=750,000,000? No wait...
        # Actually buf_size=3 with these params: duration=1, interval=10 → 60/10=6, but we overrode buf_size to 3
        # Let's recalculate with buf_size=3
        # threshold_bps = 100 * 125000 = 12,500,000
        # window_seconds = 1 * 60 = 60
        # threshold_bytes = 12,500,000 * 60 = 750,000,000

        # So we need ring sum > 750,000,000
        # But that's a huge number for 3 samples... set more realistic values.
        # Let's use a smaller window: duration=1, interval=10 → buf_size=6
        # threshold_bytes = 100*125000*60 = 750,000,000
        # Each sample needs > 125,000,000 bytes

        d = self._make_daemon(tmp_path, buf_size=6, threshold=100, higher=150, lower=80,
                              window_duration=1, interval=10, cooldown=3)

        # Fill buffer with high values: each sample = 200 MB worth of bytes
        # 200 Mbps for 10s = 200 * 125000 * 10 = 250,000,000 bytes
        huge_sample = 200 * 125_000 * 10  # 250,000,000
        for _ in range(6):
            d.buffer.push(huge_sample)

        assert d.state == STATE_NORMAL
        d._evaluate_state_machine(time.time())
        assert d.state == STATE_LIMITED
        assert d.cooldown_start is not None

    def test_limited_to_normal_after_cooldown(self, tmp_path):
        """After cooldown expires, transition back to NORMAL."""
        d = self._make_daemon(tmp_path, buf_size=3, cooldown=3)
        d.state = STATE_LIMITED
        d.cooldown_start = time.time() - 200  # cooldown=3 min=180s, expired
        d._evaluate_state_machine(time.time())
        assert d.state == STATE_NORMAL
        assert d.cooldown_start is None

    def test_limited_stays_during_cooldown(self, tmp_path):
        """During cooldown, stay in LIMITED."""
        d = self._make_daemon(tmp_path, buf_size=3, cooldown=3)
        d.state = STATE_LIMITED
        d.cooldown_start = time.time() - 10  # only 10s ago
        d._evaluate_state_machine(time.time())
        assert d.state == STATE_LIMITED

    def test_normal_stays_when_buffer_not_full(self, tmp_path):
        """Don't evaluate threshold until buffer is full."""
        d = self._make_daemon(tmp_path, buf_size=6)
        # Only 3 samples, buffer not full
        for _ in range(3):
            d.buffer.push(999_999_999_999)
        d._evaluate_state_machine(time.time())
        assert d.state == STATE_NORMAL

    def test_normal_stays_when_below_threshold(self, tmp_path):
        """Full buffer but below threshold → stay NORMAL."""
        d = self._make_daemon(tmp_path, buf_size=6, threshold=1000, higher=1500, lower=800,
                              window_duration=1, interval=10, cooldown=3)
        # Fill with tiny samples
        for _ in range(6):
            d.buffer.push(1)
        assert d.buffer.is_full()
        d._evaluate_state_machine(time.time())
        assert d.state == STATE_NORMAL

    def test_current_rate(self, tmp_path):
        d = self._make_daemon(tmp_path)
        assert d._current_rate_mbps() == 150
        d.state = STATE_LIMITED
        assert d._current_rate_mbps() == 110
