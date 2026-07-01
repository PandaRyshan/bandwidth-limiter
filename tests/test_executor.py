"""Tests for tc_limit.executor — mocked tc calls."""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from tc_limit.executor import (
    tc_init, tc_change_rate, tc_cleanup, tc_show, IFB_DEVICE,
)


# ── Helpers ────────────────────────────────────────────────────────────────


def _run_side_effect(whitelist=None):
    """Create a side-effect that only allows commands in *whitelist* to succeed."""
    whitelist = set(whitelist or [])

    def side_effect(cmd, **kwargs):
        check = kwargs.get("check", True)
        cmd_str = " ".join(cmd)
        if not check:
            return mock.MagicMock(
                returncode=0, stdout="ok\n", stderr="",
                spec=subprocess.CompletedProcess,
            )
        if cmd_str in whitelist:
            return mock.MagicMock(
                returncode=0, stdout="", stderr="",
                spec=subprocess.CompletedProcess,
            )
        raise subprocess.CalledProcessError(
            1, cmd, output="", stderr="mock failure",
        )

    return side_effect


# ── Tests ──────────────────────────────────────────────────────────────────


class TestTcInit:
    def test_dry_run(self):
        """Dry run should not call subprocess at all."""
        with mock.patch("subprocess.run") as m_run:
            tc_init("eth0", 150, dry_run=True)
            m_run.assert_not_called()

    def test_init_success(self):
        """All commands succeed."""
        all_cmds = {
            "tc qdisc del dev eth0 root",
            "tc qdisc del dev eth0 ingress",
            "tc qdisc del dev ifb0 root",
            "ip link set ifb0 down",
            "modprobe ifb numifbs=1",
            "ip link add ifb0 type ifb",
            "ip link set ifb0 up",
            "tc qdisc add dev eth0 root handle 1: htb default 10",
            "tc class add dev eth0 parent 1: classid 1:10 htb rate 150mbit ceil 150mbit burst 16kbit cburst 16kbit",
            "tc qdisc add dev eth0 handle ffff: ingress",
            "tc filter add dev eth0 parent ffff: protocol all u32 match u32 0 0 action mirred egress redirect dev ifb0",
            "tc qdisc add dev ifb0 root handle 2: htb default 20",
            "tc class add dev ifb0 parent 2: classid 2:20 htb rate 150mbit ceil 150mbit burst 16kbit cburst 16kbit",
        }
        with mock.patch("subprocess.run", side_effect=_run_side_effect(all_cmds)) as m_run:
            tc_init("eth0", 150)
            # Number of calls: 4 cleanup (ignored) + 3 IFB + 2 egress + 1 ingress + 1 filter + 2 IFB limit = 13
            assert m_run.call_count == 13

    def test_init_egress_root_fails(self):
        """HTB root add fails — should raise RuntimeError."""
        def side_effect(cmd, **kwargs):
            cmd_str = " ".join(cmd)
            if "tc qdisc add dev eth0 root" in cmd_str:
                raise subprocess.CalledProcessError(1, cmd, output="", stderr="busy")
            return mock.MagicMock(returncode=0, stdout="", stderr="")
        with mock.patch("subprocess.run", side_effect=side_effect):
            with pytest.raises(RuntimeError, match="Cannot add HTB root qdisc"):
                tc_init("eth0", 150)


class TestTcChangeRate:
    def test_dry_run(self):
        with mock.patch("subprocess.run") as m_run:
            tc_change_rate("eth0", 110, dry_run=True)
            m_run.assert_not_called()

    def test_change_rate_ok(self):
        """Successfully change rate."""
        with mock.patch("subprocess.run", side_effect=_run_side_effect({
            "tc class change dev eth0 parent 1: classid 1:10 htb rate 110mbit ceil 110mbit burst 16kbit cburst 16kbit",
            "tc class change dev ifb0 parent 2: classid 2:20 htb rate 110mbit ceil 110mbit burst 16kbit cburst 16kbit",
        })) as m_run:
            tc_change_rate("eth0", 110)
            assert m_run.call_count == 2

    def test_change_rate_ignores_failure(self):
        """Failure should be logged, not raised."""
        def side_effect(cmd, **kwargs):
            if not kwargs.get("check"):
                return mock.MagicMock(
                    returncode=0, stdout="", stderr="",
                    spec=subprocess.CompletedProcess,
                )
            raise subprocess.CalledProcessError(1, cmd, output="", stderr="err")

        with mock.patch("subprocess.run", side_effect=side_effect):
            # Should not raise
            tc_change_rate("eth0", 110)


class TestTcCleanup:
    def test_cleanup(self):
        """Cleanup runs the expected commands."""
        with mock.patch("subprocess.run", side_effect=_run_side_effect({
            "ip link set ifb0 down",
        })) as m_run:
            tc_cleanup("eth0")
            # 4 cleanup steps (3 qdisc del + 1 ip link down), all ignore failures
            # The mock always returns success for non-check runs, so all 4 succeed
            assert m_run.call_count == 4

    def test_cleanup_ignores_failures(self):
        """Cleanup should never raise, even if all commands fail."""
        with mock.patch("subprocess.run") as m_run:
            m_run.side_effect = subprocess.CalledProcessError(1, [], output="", stderr="")
            # Should not raise
            tc_cleanup("eth0")


class TestTcShow:
    def test_show(self):
        def side_effect(cmd, **kwargs):
            if "-s" in cmd:
                return mock.MagicMock(
                    returncode=0,
                    stdout="class htb 1:10 rate 150Mbit\n",
                    stderr="",
                )
            return mock.MagicMock(returncode=0, stdout="", stderr="")
        with mock.patch("subprocess.run", side_effect=side_effect):
            result = tc_show("eth0")
            assert "class htb" in result
            assert "── tc egress" in result
            assert "── tc ingress" in result
