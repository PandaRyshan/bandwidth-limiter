"""CLI entry point for tc-limit."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from typing import Optional

from tc_limit.config import Config, LogLevel, load_config
from tc_limit.daemon import run_daemon
from tc_limit.executor import tc_show
from tc_limit.sampler import detect_interface
from tc_limit.storage.queries import (
    query_daily_volume, query_bandwidth_timeline,
    query_state_events, query_summary,
)

logger = logging.getLogger("tc_limit")


# ── Helpers ────────────────────────────────────────────────────────────────


def _setup_logging(level: LogLevel) -> None:
    """Configure root logger to write to stderr."""
    logger.setLevel(logging.DEBUG if level == LogLevel.DEBUG else logging.INFO)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)-5s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    # Map LogLevel to Python levels
    mapping = {
        LogLevel.ERROR: logging.ERROR,
        LogLevel.WARN: logging.WARNING,
        LogLevel.INFO: logging.INFO,
        LogLevel.DEBUG: logging.DEBUG,
    }
    handler.setLevel(mapping.get(level, logging.INFO))

    # Clear any existing handlers
    logger.handlers.clear()
    logger.addHandler(handler)

    # Also set package-level loggers
    for mod in ("tc_limit.config", "tc_limit.sampler", "tc_limit.executor",
                "tc_limit.daemon"):
        mod_logger = logging.getLogger(mod)
        mod_logger.handlers.clear()
        mod_logger.addHandler(handler)
        mod_logger.setLevel(mapping.get(level, logging.INFO))


def _build_config(args: argparse.Namespace) -> Config:
    """Build a Config from parsed CLI args."""
    cli_overrides: dict = {}
    for key, val in vars(args).items():
        if val is not None and key not in ("command", "config", "func"):
            cli_overrides[key] = val

    config_path = getattr(args, "config", None)
    return load_config(config_path=config_path, cli_overrides=cli_overrides if cli_overrides else None)


def _read_pid(pid_file: str) -> Optional[int]:
    """Read PID from *pid_file*, returning None if the file is absent."""
    try:
        with open(pid_file, "r") as fh:
            return int(fh.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def _process_running(pid: int) -> bool:
    """Check if a process with *pid* is running."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


# ── Subcommands ────────────────────────────────────────────────────────────


def cmd_daemon(args: argparse.Namespace) -> None:
    """Start the daemon (foreground)."""
    cfg = _build_config(args)
    _setup_logging(cfg.runtime.log_level)
    run_daemon(cfg)


def cmd_stop(args: argparse.Namespace) -> None:
    """Stop a running daemon."""
    cfg = _build_config(args)
    _setup_logging(cfg.runtime.log_level)

    pid = _read_pid(cfg.runtime.pid_file)
    if pid is None:
        print("Daemon not running (no PID file)", file=sys.stderr)
        sys.exit(1)

    if not _process_running(pid):
        print("Daemon not running (stale PID file)", file=sys.stderr)
        sys.exit(1)

    logger.info("Sending SIGTERM to daemon (PID %d)…", pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except PermissionError:
        logger.error("Permission denied sending signal to PID %d", pid)
        sys.exit(1)

    # Wait up to 5s for graceful exit
    import time
    for _ in range(50):
        if not _process_running(pid):
            print("Daemon stopped.")
            return
        time.sleep(0.1)

    logger.warning("Daemon did not exit within 5s, sending SIGKILL")
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    print("Daemon force-stopped.")


def cmd_status(args: argparse.Namespace) -> None:
    """Print daemon and tc status."""
    cfg = _build_config(args)
    _setup_logging(cfg.runtime.log_level)

    # Read state file
    try:
        with open(cfg.runtime.state_file, "r") as fh:
            state = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}

    iface = cfg.network.interface or detect_interface()

    # Daemon status
    pid = _read_pid(cfg.runtime.pid_file)
    if pid and _process_running(pid):
        print("Daemon: running")
        print(f"State:      {state.get('state', 'UNKNOWN')}")
        print(f"Rate:       {state.get('current_rate_mbps', '-')} Mbps")
        print(f"Threshold:  {state.get('threshold_mbps', '-')} Mbps")
        wa = state.get("window_avg_mbps")
        if wa is not None:
            print(f"Window:     {wa} Mbps avg")
        if state.get("state") == "LIMITED":
            cs = state.get("cooldown_start")
            cd = state.get("cooldown_seconds", 0)
            if cs is not None:
                import time
                remain = max(0, cd - (time.time() - cs))
                print(f"Recover:    {remain:.0f}s remaining")
        print(f"PID:        {pid}")
    else:
        print("Daemon: not running")

    # tc rules
    print()
    print(tc_show(iface))


def cmd_reload(args: argparse.Namespace) -> None:
    """Send SIGHUP to a running daemon for config hot-reload."""
    cfg = _build_config(args)
    _setup_logging(cfg.runtime.log_level)

    pid = _read_pid(cfg.runtime.pid_file)
    if pid is None:
        print("Daemon not running (no PID file)", file=sys.stderr)
        sys.exit(1)

    if not _process_running(pid):
        print("Daemon not running (stale PID file)", file=sys.stderr)
        sys.exit(1)

    logger.info("Sending SIGHUP to daemon (PID %d)…", pid)
    os.kill(pid, signal.SIGHUP)
    print("Reload signal sent.")


# ── Parser ─────────────────────────────────────────────────────────────────


def cmd_report(args: argparse.Namespace) -> None:
    """Generate analysis reports from stored metrics."""
    cfg = _build_config(args)
    db_path = cfg.storage.path

    # Check DB exists
    import os as _os
    if not _os.path.exists(db_path):
        print(f"No data yet — database not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    sub = args.report_subcommand

    if sub == "volume":
        days = getattr(args, "days", 7)
        rows = query_daily_volume(db_path, days=days)
        if not rows:
            print("No daily summary data yet.")
            return
        print(f"{'Date':<12} {'Total GB':>10} {'Avg Mbps':>10} {'Peak Mbps':>10}")
        print("-" * 46)
        for r in rows:
            print(f"{r['date']:<12} {r['total_gb']:>10.2f} {r['avg_mbps']:>10.1f} {r['peak_mbps']:>10.1f}")

    elif sub == "bandwidth":
        since = getattr(args, "since", None)
        limit = getattr(args, "limit", 500)
        rows = query_bandwidth_timeline(db_path, since=since, limit=limit)
        if not rows:
            print("No bandwidth samples yet.")
            return
        print(f"{'Timestamp':<22} {'Rate Mbps':>10} {'State':>10} {'Limit':>8}")
        print("-" * 54)
        for r in rows:
            ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r["ts"])) if r["ts"] else "-"
            print(f"{ts_str:<22} {r['rate_mbps']:>10.1f} {r['state']:>10} {r['limit_mbps']:>8}")

    elif sub == "events":
        limit = getattr(args, "limit", 50)
        rows = query_state_events(db_path, limit=limit)
        if not rows:
            print("No state change events yet.")
            return
        print(f"{'Timestamp':<22} {'From':>10} {'To':>10} {'Reason':>50}")
        print("-" * 96)
        for r in rows:
            ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r["ts"])) if r["ts"] else "-"
            reason = (r["reason"] or "")[:48]
            print(f"{ts_str:<22} {r['from_state']:>10} {r['to_state']:>10} {reason:<50}")

    elif sub == "summary":
        data = query_summary(db_path)
        if data["sample_count"] == 0:
            print("No data collected yet.")
            return
        print(f"Total samples:       {data['sample_count']}")
        print(f"Total transfer:      {data['total_transfer_gb']} GB")
        print(f"Last rate:           {data['last_rate_mbps']} Mbps ({data['last_state']})")
        if data["last_sample_ts"]:
            ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(data["last_sample_ts"]))
            print(f"Last sample:         {ts_str}")
        print(f"Limited time (tot):  {data['total_limited_minutes']} min")
        print(f"State changes:       {data['total_state_changes']}")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="tc-limit",
        description="Smart bandwidth limit daemon using Linux tc.",
    )
    sub = parser.add_subparsers(dest="command", required=True, title="commands")

    # ── daemon ──
    p_daemon = sub.add_parser("daemon", help="Start the daemon (foreground)")
    p_daemon.add_argument("-c", "--config", help="Path to config YAML")
    _add_limit_args(p_daemon)
    p_daemon.add_argument("--iface", dest="interface", help="Network interface")
    p_daemon.add_argument("--dry-run", action="store_true", help="Monitor only, no tc changes")
    p_daemon.add_argument("--log-level", choices=["DEBUG", "INFO", "WARN", "ERROR"])
    p_daemon.add_argument("--state-file", help="State file path")
    p_daemon.add_argument("--pid-file", help="PID file path")
    p_daemon.set_defaults(func=cmd_daemon)

    # ── stop ──
    p_stop = sub.add_parser("stop", help="Stop the running daemon")
    p_stop.add_argument("-c", "--config", help="Path to config YAML")
    p_stop.set_defaults(func=cmd_stop)

    # ── status ──
    p_status = sub.add_parser("status", help="Show daemon and tc status")
    p_status.add_argument("-c", "--config", help="Path to config YAML")
    p_status.set_defaults(func=cmd_status)

    # ── reload ──
    p_reload = sub.add_parser("reload", help="Hot-reload config (sends SIGHUP)")
    p_reload.add_argument("-c", "--config", help="Path to config YAML")
    p_reload.set_defaults(func=cmd_reload)

    # ── report (Phase 2) ──
    p_report = sub.add_parser("report", help="Generate analysis reports")
    report_subs = p_report.add_subparsers(dest="report_subcommand", required=True)

    p_vol = report_subs.add_parser("volume", help="Daily traffic volume")
    p_vol.add_argument("-c", "--config", help="Path to config YAML")
    p_vol.add_argument("-d", "--days", type=int, default=7, help="Days to show (default: 7)")
    p_vol.set_defaults(func=cmd_report)

    p_bw = report_subs.add_parser("bandwidth", help="Bandwidth timeline")
    p_bw.add_argument("-c", "--config", help="Path to config YAML")
    p_bw.add_argument("--since", help="ISO date string (e.g. 2026-07-01)")
    p_bw.add_argument("-n", "--limit", type=int, default=500, help="Max rows (default: 500)")
    p_bw.set_defaults(func=cmd_report)

    p_ev = report_subs.add_parser("events", help="State change events")
    p_ev.add_argument("-c", "--config", help="Path to config YAML")
    p_ev.add_argument("-n", "--limit", type=int, default=50, help="Max rows (default: 50)")
    p_ev.set_defaults(func=cmd_report)

    p_sum = report_subs.add_parser("summary", help="Aggregate summary")
    p_sum.add_argument("-c", "--config", help="Path to config YAML")
    p_sum.set_defaults(func=cmd_report)

    return parser


def _add_limit_args(parser: argparse.ArgumentParser) -> None:
    """Add bandwidth-related CLI arguments to *parser*."""
    parser.add_argument("-H", "--higher-limit", dest="higher_limit",
                        type=int, help="Normal rate (Mbps)")
    parser.add_argument("-L", "--lower-limit", dest="lower_limit",
                        type=int, help="Limited rate (Mbps)")
    parser.add_argument("-T", "--threshold", type=int,
                        help="Alert threshold (Mbps)")
    parser.add_argument("-W", "--window-duration", dest="window_duration",
                        type=int, help="Window size (minutes)")
    parser.add_argument("-I", "--window-interval", dest="window_interval",
                        type=int, help="Sampling interval (seconds)")
    parser.add_argument("-C", "--cooldown", type=int,
                        help="Cooldown (minutes)")


# ── Entry point ────────────────────────────────────────────────────────────


def main(argv: Optional[list] = None) -> None:
    """Main CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
