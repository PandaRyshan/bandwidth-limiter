"""Linux tc (traffic control) operations for bandwidth limiting.

Manages HTB qdisc on egress and IFB-based ingress limiting.
"""

from __future__ import annotations

import logging
import subprocess
from typing import List

logger = logging.getLogger(__name__)

IFB_DEVICE = "ifb0"


# ── Helpers ────────────────────────────────────────────────────────────────


def _run(cmd: List[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a command, log it at DEBUG, and optionally check its return code."""
    logger.debug("  $ %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _run_ok(cmd: List[str]) -> bool:
    """Run a command; return True on success, False with a warning on failure."""
    try:
        _run(cmd, check=True)
        return True
    except subprocess.CalledProcessError as exc:
        logger.warning("Command failed (ignored): %s — %s", " ".join(cmd), exc.stderr.strip())
        return False


# ── Public API ─────────────────────────────────────────────────────────────


def tc_init(
    iface: str, rate_mbps: int, burst_kbit: int = 16, dry_run: bool = False,
) -> None:
    """Initialise tc HTB qdisc on *iface* for egress + ingress limiting.

    Sets up:
      - HTB root qdisc on *iface* (egress)
      - IFB device for ingress redirect
      - HTB root qdisc on *ifb0* (ingress)

    Args:
        iface: Network interface name.
        rate_mbps: Bandwidth limit in Mbps.
        burst_kbit: Token bucket burst size in kbit.
        dry_run: If True, log only — don't execute tc commands.

    Raises:
        RuntimeError: A critical tc setup step failed.
    """
    if dry_run:
        logger.info("[dry-run] Would set up tc: %d Mbps egress+ingress via IFB", rate_mbps)
        return

    # ── Clean slate ──
    _run_ok(["tc", "qdisc", "del", "dev", iface, "root"])
    _run_ok(["tc", "qdisc", "del", "dev", iface, "ingress"])
    _run_ok(["tc", "qdisc", "del", "dev", IFB_DEVICE, "root"])
    _run_ok(["ip", "link", "set", IFB_DEVICE, "down"])

    # ── IFB device ──
    _run_ok(["modprobe", "ifb", "numifbs=1"])
    _run_ok(["ip", "link", "add", IFB_DEVICE, "type", "ifb"])
    _run_ok(["ip", "link", "set", IFB_DEVICE, "up"])

    burst = f"{burst_kbit}kbit"
    rate = f"{rate_mbps}mbit"

    # ── Egress HTB ──
    try:
        _run(["tc", "qdisc", "add", "dev", iface, "root",
              "handle", "1:", "htb", "default", "10"])
    except subprocess.CalledProcessError as exc:
        existing = ""
        try:
            r = _run(["tc", "qdisc", "show", "dev", iface], check=False)
            existing = r.stdout.split("\n")[0] if r.stdout else "(empty)"
        except Exception:
            pass
        raise RuntimeError(
            f"Cannot add HTB root qdisc on {iface} (existing: {existing})"
        ) from exc

    try:
        _run(["tc", "class", "add", "dev", iface, "parent", "1:",
              "classid", "1:10", "htb", "rate", rate, "ceil", rate,
              "burst", burst, "cburst", burst])
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Cannot add HTB class on {iface}") from exc

    # ── Ingress → IFB ──
    _run_ok(["tc", "qdisc", "add", "dev", iface, "handle", "ffff:", "ingress"])
    try:
        _run(["tc", "filter", "add", "dev", iface, "parent", "ffff:",
              "protocol", "all", "u32", "match", "u32", "0", "0",
              "action", "mirred", "egress", "redirect", "dev", IFB_DEVICE])
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Cannot add ingress redirect to {IFB_DEVICE}") from exc

    # ── IFB limit ──
    try:
        _run(["tc", "qdisc", "add", "dev", IFB_DEVICE, "root",
              "handle", "2:", "htb", "default", "20"])
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Cannot add HTB root qdisc on {IFB_DEVICE}") from exc

    try:
        _run(["tc", "class", "add", "dev", IFB_DEVICE, "parent", "2:",
              "classid", "2:20", "htb", "rate", rate, "ceil", rate,
              "burst", burst, "cburst", burst])
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Cannot add HTB class on {IFB_DEVICE}") from exc

    logger.info("tc initialised: %d Mbps (egress + ingress)", rate_mbps)


def tc_change_rate(iface: str, rate_mbps: int, burst_kbit: int = 16, dry_run: bool = False) -> None:
    """Change the bandwidth limit on existing tc rules.

    Args:
        iface: Network interface name.
        rate_mbps: New bandwidth limit in Mbps.
        burst_kbit: Token bucket burst size in kbit.
        dry_run: If True, log only.
    """
    if dry_run:
        logger.info("[dry-run] Would switch tc to %d Mbps", rate_mbps)
        return

    burst = f"{burst_kbit}kbit"
    rate = f"{rate_mbps}mbit"

    _run_ok(["tc", "class", "change", "dev", iface, "parent", "1:",
             "classid", "1:10", "htb", "rate", rate, "ceil", rate,
             "burst", burst, "cburst", burst])

    _run_ok(["tc", "class", "change", "dev", IFB_DEVICE, "parent", "2:",
             "classid", "2:20", "htb", "rate", rate, "ceil", rate,
             "burst", burst, "cburst", burst])


def tc_cleanup(iface: str) -> None:
    """Remove all tc rules and tear down the IFB device.

    Safe to call when tc rules don't exist.
    """
    logger.info("Cleaning up tc rules and IFB device…")

    _run_ok(["tc", "qdisc", "del", "dev", iface, "root"])
    _run_ok(["tc", "qdisc", "del", "dev", iface, "ingress"])
    _run_ok(["tc", "qdisc", "del", "dev", IFB_DEVICE, "root"])
    _run_ok(["ip", "link", "set", IFB_DEVICE, "down"])

    logger.info("Cleanup complete. Bandwidth limits removed.")


def tc_show(iface: str) -> str:
    """Return a human-readable summary of current tc rules on *iface*."""
    lines: list[str] = []

    # egress
    try:
        r = _run(["tc", "-s", "class", "show", "dev", iface], check=False)
        lines.append(f"── tc egress ({iface}) ──")
        lines.append(r.stdout.rstrip() or "(no rules)")
    except Exception:
        lines.append(f"── tc egress ({iface}) ──")
        lines.append("(error reading)")

    # ingress / IFB
    try:
        r = _run(["tc", "-s", "class", "show", "dev", IFB_DEVICE], check=False)
        lines.append("")
        lines.append(f"── tc ingress ({IFB_DEVICE}) ──")
        lines.append(r.stdout.rstrip() or "(no rules)")
    except Exception:
        lines.append("")
        lines.append(f"── tc ingress ({IFB_DEVICE}) ──")
        lines.append("(error reading)")

    return "\n".join(lines)
