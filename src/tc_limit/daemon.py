"""Daemon main loop: sampling, state machine, signal handling, persistence."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from tc_limit.config import Config, reload_config
from tc_limit.executor import tc_change_rate, tc_cleanup, tc_init, tc_show
from tc_limit.sampler import RingBuffer, detect_interface, read_counters

logger = logging.getLogger(__name__)


# ── Constants ──────────────────────────────────────────────────────────────

STATE_NORMAL = "NORMAL"
STATE_LIMITED = "LIMITED"


# ── Helpers ────────────────────────────────────────────────────────────────


def _sd_notify(message: str) -> None:
    """Send a notification to systemd via NOTIFY_SOCKET.

    No-op when not running under systemd.
    """
    sock = os.environ.get("NOTIFY_SOCKET")
    if not sock:
        return
    try:
        import socket
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.sendto(message.encode(), sock)
        s.close()
    except Exception:
        pass


# ── Lock ───────────────────────────────────────────────────────────────────


def acquire_lock(pid_file: str) -> int:
    """Acquire an exclusive lock via *pid_file*.

    Returns the file descriptor (kept open for the daemon's lifetime).

    Raises:
        SystemExit: Another instance is already running.
    """
    try:
        Path(pid_file).parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Under ProtectSystem=strict /run may be read-only;
        # systemd's RuntimeDirectory= handles directory creation.
        pass
    fd = os.open(pid_file, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        logger.error("Another instance is already running (lock on %s)", pid_file)
        sys.exit(1)

    # Write PID
    os.truncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, f"{os.getpid()}\n".encode())
    os.fsync(fd)
    return fd


def release_lock(fd: int, pid_file: str) -> None:
    """Release the lock and remove the PID file."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        os.close(fd)
    except Exception:
        pass
    try:
        os.unlink(pid_file)
    except FileNotFoundError:
        pass


# ── State Persistence ──────────────────────────────────────────────────────


def save_state(
    state_file: str,
    state: str,
    rate_mbps: int,
    threshold_mbps: int,
    window_avg_mbps: float,
    cooldown_seconds: int,
    cooldown_start: Optional[float],
    sample_count: int,
) -> None:
    """Persist current daemon state to a JSON file."""
    Path(state_file).parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "state": state,
        "current_rate_mbps": rate_mbps,
        "threshold_mbps": threshold_mbps,
        "window_avg_mbps": round(window_avg_mbps, 1) if window_avg_mbps is not None else None,
        "cooldown_seconds": cooldown_seconds,
        "cooldown_start": cooldown_start,
        "sample_count": sample_count,
        "updated_at": time.time(),
    }
    tmp = f"{state_file}.tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=2)
    os.rename(tmp, state_file)


def load_state(state_file: str) -> dict:
    """Load persisted daemon state.

    Returns a dict; defaults to NORMAL when the file is absent or unreadable.
    """
    try:
        with open(state_file, "r") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

    state = data.get("state", STATE_NORMAL)
    cooldown_start = data.get("cooldown_start")
    cooldown_seconds = data.get("cooldown_seconds", 0)

    # If LIMITED with remaining cooldown, resume; otherwise drop to NORMAL.
    if state == STATE_LIMITED and cooldown_start is not None:
        elapsed = time.time() - cooldown_start
        if elapsed < cooldown_seconds:
            logger.info("Resumed LIMITED state (%.0fs cooldown remaining)", cooldown_seconds - elapsed)
            return data
        else:
            logger.info("Stored cooldown expired, starting NORMAL")
            data["state"] = STATE_NORMAL
            data["cooldown_start"] = None
    else:
        data["state"] = STATE_NORMAL
        data["cooldown_start"] = None

    return data


# ── Daemon ─────────────────────────────────────────────────────────────────


class Daemon:
    """Smart bandwidth limit daemon."""

    def __init__(self, config: Config) -> None:
        self.cfg = config

        # Interface
        iface = config.network.interface.strip()
        self.iface: str = iface or detect_interface()
        if not self.iface:
            raise RuntimeError("Cannot determine network interface")

        # Ring buffer
        self.buffer = RingBuffer(config.buf_size)

        # State
        self.state: str = STATE_NORMAL
        self.cooldown_start: Optional[float] = None
        self.sample_count: int = 0

        # Counter tracking
        self.prev_bytes: int = 0
        self.consecutive_failures: int = 0

        # Lock fd
        self._lock_fd: Optional[int] = None

        # Signal flags
        self._shutdown: bool = False
        self._reload_requested: bool = False

        # Storage (Phase 2)
        self._storage: Optional[object] = None

    # ── Public API ────────────────────────────────────────────────────────

    def run(self) -> None:
        """Start the daemon main loop.  Blocks until shutdown."""
        # Lock
        self._lock_fd = acquire_lock(self.cfg.runtime.pid_file)

        # Resumed state
        persisted = load_state(self.cfg.runtime.state_file)
        if persisted:
            self.state = persisted.get("state", STATE_NORMAL)
            self.cooldown_start = persisted.get("cooldown_start")

        # Signals
        signal.signal(signal.SIGTERM, self._on_terminate)
        signal.signal(signal.SIGINT, self._on_terminate)
        signal.signal(signal.SIGHUP, self._on_hup)
        signal.signal(signal.SIGUSR1, self._on_usr1)

        # Log start
        logger.info(
            "Daemon started: higher=%dM lower=%dM threshold=%dM "
            "window=%dm interval=%ds cooldown=%dm",
            self.cfg.limits.higher, self.cfg.limits.lower,
            self.cfg.limits.threshold, self.cfg.window.duration,
            self.cfg.window.interval, self.cfg.cooldown,
        )
        logger.info("Interface: %s", self.iface)

        # Init tc
        rate = self._current_rate_mbps()
        tc_init(self.iface, rate, self.cfg.network.burst_kbit, self.cfg.runtime.dry_run)

        # Seed byte counter
        self.prev_bytes = read_counters(self.iface)
        if self.prev_bytes < 0:
            raise RuntimeError(f"Cannot read /sys counters for {self.iface}")

        # Notify systemd we're ready
        _sd_notify("READY=1")
        self._save_state()

        # ── Main loop ─────────────────────────────────────────────────────
        interval = self.cfg.window.interval
        summary_interval = 60  # seconds between periodic summaries
        last_summary_time = time.time()

        while not self._shutdown:
            time.sleep(interval)

            # Handle hot-reload
            if self._reload_requested:
                self._do_reload()
                interval = self.cfg.window.interval
                self._reload_requested = False

            if self._shutdown:
                break

            # ── Sample ──
            try:
                cur_bytes = read_counters(self.iface)
            except (FileNotFoundError, OSError) as exc:
                self.consecutive_failures += 1
                logger.warning("Failed to read /sys counters (%d/3): %s",
                               self.consecutive_failures, exc)
                if self.consecutive_failures >= 3:
                    logger.error("3 consecutive reads failed, exiting")
                    self._shutdown = True
                    break
                continue
            self.consecutive_failures = 0

            delta = cur_bytes - self.prev_bytes
            self.prev_bytes = cur_bytes

            # Counter wrap guard
            if delta < 0:
                logger.warning("Counter wrap detected, resetting buffer")
                self.buffer.clear()
                self.prev_bytes = cur_bytes
                continue

            # ── Push to ring buffer ──
            self.buffer.push(delta)
            self.sample_count += 1

            logger.debug(
                "sample #%d: delta_bytes=%d window_filled=%d/%d",
                self.sample_count, delta, self.buffer.filled, self.buffer.size,
            )

            # ── State machine ──
            now = time.time()
            self._evaluate_state_machine(now)

            # ── Periodic summary ──
            if now - last_summary_time >= summary_interval:
                self._save_state()
                avg = self.buffer.average_mbps(interval)
                logger.info(
                    "summary: state=%s rate=%dM window_avg=%.1fMbps "
                    "samples=%d/%d",
                    self.state, self._current_rate_mbps(), avg,
                    self.buffer.filled, self.buffer.size,
                )
                last_summary_time = now

        # ── Cleanup ───────────────────────────────────────────────────────
        self._shutdown_handler()

    def _current_rate_mbps(self) -> int:
        """Return the tc rate for the current state."""
        return (self.cfg.limits.lower if self.state == STATE_LIMITED
                else self.cfg.limits.higher)

    # ── State machine ─────────────────────────────────────────────────────

    def _evaluate_state_machine(self, now: float) -> None:
        """Evaluate state transitions based on window average and cooldown."""
        if self.state == STATE_NORMAL:
            if not self.buffer.is_full():
                return
            window_sum = self.buffer.sum()
            threshold_bytes = self.cfg.threshold_bps * self.cfg.window_seconds
            if window_sum > threshold_bytes:
                self.state = STATE_LIMITED
                self.cooldown_start = now
                tc_change_rate(self.iface, self.cfg.limits.lower,
                               self.cfg.network.burst_kbit, self.cfg.runtime.dry_run)
                avg = self.buffer.average_mbps(self.cfg.window.interval)
                self._save_state()
                logger.info(
                    "→ LIMITED (window avg %.1fMbps > %dM threshold, cooldown %ds)",
                    avg, self.cfg.limits.threshold, self.cfg.cooldown_seconds,
                )

        elif self.state == STATE_LIMITED:
            assert self.cooldown_start is not None
            elapsed = now - self.cooldown_start
            if elapsed >= self.cfg.cooldown_seconds:
                self.state = STATE_NORMAL
                self.cooldown_start = None
                self.buffer.clear()
                tc_change_rate(self.iface, self.cfg.limits.higher,
                               self.cfg.network.burst_kbit, self.cfg.runtime.dry_run)
                self._save_state()
                logger.info("→ NORMAL (cooldown complete, rate restored to %dM)",
                            self.cfg.limits.higher)

    # ── Persistence ───────────────────────────────────────────────────────

    def _save_state(self) -> None:
        """Persist current daemon state to the state file."""
        save_state(
            self.cfg.runtime.state_file,
            state=self.state,
            rate_mbps=self._current_rate_mbps(),
            threshold_mbps=self.cfg.limits.threshold,
            window_avg_mbps=self.buffer.average_mbps(self.cfg.window.interval),
            cooldown_seconds=self.cfg.cooldown_seconds,
            cooldown_start=self.cooldown_start,
            sample_count=self.sample_count,
        )

    # ── Signal handlers ───────────────────────────────────────────────────

    def _on_terminate(self, signum: int, frame: object) -> None:
        """Handle SIGTERM / SIGINT."""
        sig_name = signal.Signals(signum).name
        logger.info("Received %s, shutting down…", sig_name)
        self._shutdown = True

    def _on_hup(self, signum: int, frame: object) -> None:
        """Handle SIGHUP — request config reload."""
        logger.info("Received SIGHUP, scheduling config reload…")
        self._reload_requested = True

    def _on_usr1(self, signum: int, frame: object) -> None:
        """Handle SIGUSR1 — dump status to stderr."""
        avg = self.buffer.average_mbps(self.cfg.window.interval)
        line = (
            f"[STATUS] state={self.state} rate={self._current_rate_mbps()}Mbps "
            f"samples={self.sample_count}"
        )
        if self.state == STATE_LIMITED and self.cooldown_start is not None:
            remain = max(0, self.cfg.cooldown_seconds - (time.time() - self.cooldown_start))
            line += f" cooldown={remain:.0f}s"
        if self.buffer.filled > 0:
            line += f" window_avg={avg:.1f}Mbps"
        logger.info(line)

    # ── Reload ────────────────────────────────────────────────────────────

    def _do_reload(self) -> None:
        """Perform hot-reload of configuration."""
        try:
            self.cfg = reload_config(self.cfg)
        except (ValueError, FileNotFoundError) as exc:
            logger.warning("Config reload failed: %s", exc)
            return

        # Apply new rate if in NORMAL
        if self.state == STATE_NORMAL:
            tc_change_rate(self.iface, self.cfg.limits.higher,
                           self.cfg.network.burst_kbit, self.cfg.runtime.dry_run)
        self._save_state()

    # ── Shutdown ──────────────────────────────────────────────────────────

    def _shutdown_handler(self) -> None:
        """Graceful shutdown: cleanup tc, release lock, remove state."""
        tc_cleanup(self.iface)
        if self._lock_fd is not None:
            release_lock(self._lock_fd, self.cfg.runtime.pid_file)
        try:
            os.unlink(self.cfg.runtime.state_file)
        except FileNotFoundError:
            pass
        logger.info("Daemon stopped.")


# ── Standalone execution ───────────────────────────────────────────────────


def run_daemon(config: Config) -> None:
    """Entry point: create and run the daemon with *config*."""
    daemon = Daemon(config)
    daemon.run()
