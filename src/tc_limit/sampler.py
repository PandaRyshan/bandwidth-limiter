"""Network bandwidth sampling via /sys/class/net counters and a ring buffer."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from tc_limit.config import MBIT_TO_BPS

logger = logging.getLogger(__name__)

SYS_NET = Path("/sys/class/net")


# ── Ring Buffer ────────────────────────────────────────────────────────────


class RingBuffer:
    """Fixed-size ring buffer for sliding-window bandwidth calculation.

    Each slot stores the delta bytes for one sampling interval.
    """

    def __init__(self, size: int) -> None:
        if size < 1:
            raise ValueError(f"RingBuffer size must be >= 1, got {size}")
        self._buf: list[int] = [0] * size
        self._idx: int = 0
        self._filled: int = 0
        self._size: int = size

    # ── properties ──

    @property
    def size(self) -> int:
        return self._size

    @property
    def filled(self) -> int:
        return self._filled

    def is_full(self) -> bool:
        return self._filled >= self._size

    # ── mutation ──

    def push(self, delta: int) -> None:
        """Push a delta-bytes sample into the buffer."""
        self._buf[self._idx] = delta
        self._idx = (self._idx + 1) % self._size
        if self._filled < self._size:
            self._filled += 1

    def sum(self) -> int:
        """Return the sum of all bytes currently in the buffer."""
        return sum(self._buf)

    def average_mbps(self, interval_seconds: int) -> float:
        """Compute average bandwidth in Mbps over the filled slots.

        Returns 0.0 when the buffer is empty.
        """
        if self._filled == 0:
            return 0.0
        return self.sum() / (self._filled * interval_seconds) / MBIT_TO_BPS

    def clear(self) -> None:
        """Reset the buffer to all zeros."""
        for i in range(self._size):
            self._buf[i] = 0
        self._idx = 0
        self._filled = 0

    def __repr__(self) -> str:
        return f"RingBuffer(size={self._size}, filled={self._filled})"


# ── Counter Reading ────────────────────────────────────────────────────────


def read_counters(iface: str) -> int:
    """Read total bytes (tx + rx) from /sys counters for *iface*.

    Returns:
        Sum of tx_bytes and rx_bytes.

    Raises:
        FileNotFoundError: The interface's statistics directory is missing.
        OSError: Another I/O error reading counters.
    """
    base = SYS_NET / iface / "statistics"
    tx_path = base / "tx_bytes"
    rx_path = base / "rx_bytes"

    try:
        tx = int(tx_path.read_text().strip())
        rx = int(rx_path.read_text().strip())
    except FileNotFoundError:
        raise FileNotFoundError(f"Cannot read counters for interface '{iface}' (missing /sys/class/net/{iface}/statistics)")
    except ValueError as exc:
        raise OSError(f"Invalid counter value for interface '{iface}': {exc}")

    return tx + rx


def read_counters_split(iface: str) -> tuple[int, int]:
    """Read tx_bytes and rx_bytes separately from /sys counters.

    Returns:
        (tx_bytes, rx_bytes) tuple.
    """
    base = SYS_NET / iface / "statistics"
    tx = int((base / "tx_bytes").read_text().strip())
    rx = int((base / "rx_bytes").read_text().strip())
    return tx, rx


def detect_interface() -> str:
    """Auto-detect the default network interface via `ip route`.

    Returns:
        Interface name (e.g. "eth0").

    Raises:
        RuntimeError: Cannot determine the default interface.
    """
    try:
        result = subprocess.run(
            ["ip", "route", "get", "1.1.1.1"],
            capture_output=True, text=True, timeout=5,
        )
    except FileNotFoundError:
        raise RuntimeError("'ip' command not found — cannot detect interface")
    except subprocess.TimeoutExpired:
        raise RuntimeError("'ip route get' timed out")

    if result.returncode != 0:
        raise RuntimeError(f"'ip route get' failed: {result.stderr.strip()}")

    # Parse "1.1.1.1 via X.X.X.X dev eth0 ..."
    parts = result.stdout.split()
    for i, part in enumerate(parts):
        if part == "dev" and i + 1 < len(parts):
            return parts[i + 1]

    raise RuntimeError(f"Could not parse interface from: {result.stdout.strip()}")
