"""Tests for tc_limit.sampler."""

from __future__ import annotations

import math
import pytest

from tc_limit.sampler import RingBuffer


class TestRingBuffer:
    def test_init(self):
        rb = RingBuffer(5)
        assert rb.size == 5
        assert rb.filled == 0
        assert not rb.is_full()
        assert rb.sum() == 0
        assert rb.average_mbps(10) == 0.0

    def test_init_validation(self):
        with pytest.raises(ValueError, match=">= 1"):
            RingBuffer(0)
        with pytest.raises(ValueError, match=">= 1"):
            RingBuffer(-1)

    def test_push_and_fill(self):
        rb = RingBuffer(3)
        rb.push(100)
        assert rb.filled == 1
        assert not rb.is_full()
        rb.push(200)
        rb.push(300)
        assert rb.filled == 3
        assert rb.is_full()
        assert rb.sum() == 600

    def test_wraparound(self):
        rb = RingBuffer(3)
        rb.push(10)
        rb.push(20)
        rb.push(30)
        assert rb.is_full()
        # Overwrites oldest
        rb.push(40)
        assert rb.is_full()
        assert rb.sum() == 20 + 30 + 40  # 90

    def test_clear(self):
        rb = RingBuffer(3)
        rb.push(10)
        rb.push(20)
        rb.clear()
        assert rb.filled == 0
        assert rb.sum() == 0
        assert not rb.is_full()

    def test_average_mbps(self):
        # 1 Mbps = 125,000 B/s
        # For a 10s interval: 10 * 125000 = 1,250,000 bytes per sample → 1 Mbps
        rb = RingBuffer(2)
        # Push two samples that each represent exactly 1 Mbps
        bytes_per_sample = 10 * 125_000  # 1,250,000
        rb.push(bytes_per_sample)
        rb.push(bytes_per_sample)
        avg = rb.average_mbps(10)
        assert avg == pytest.approx(1.0)

    def test_average_mbps_empty(self):
        rb = RingBuffer(5)
        assert rb.average_mbps(10) == 0.0

    def test_average_mbps_partial(self):
        """Average over only filled slots, not the whole buffer."""
        rb = RingBuffer(5)
        # Only push one sample of 1 Mbps
        bytes_per_sample = 10 * 125_000
        rb.push(bytes_per_sample)
        avg = rb.average_mbps(10)
        assert avg == pytest.approx(1.0)

    def test_average_mbps_multiple_speeds(self):
        """Simulate 2 Mbps then 0.5 Mbps average."""
        rb = RingBuffer(2)
        interval = 10
        # 2 Mbps: 20 * 125000
        rb.push(20 * 125_000)
        # 0.5 Mbps: 5 * 125000
        rb.push(int(5 * 125_000))
        # Average: (20+5)/2 = 12.5 Mbps-equivalent bytes
        # 12.5 * 125000 / 2 / 10 / 125000 = 1.25 Mbps
        avg = rb.average_mbps(interval)
        assert avg == pytest.approx(1.25)

    def test_repr(self):
        rb = RingBuffer(3)
        rb.push(1)
        assert repr(rb) == "RingBuffer(size=3, filled=1)"
