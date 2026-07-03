"""Configuration loading, validation, and hot-reload.

Config priority: CLI args > config file > built-in defaults.

All time values are in seconds; traffic values in MB; bandwidth in Mbps.
Values may be bare integers or string expressions like "5 * 60".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────

DEFAULT_CONFIG_PATH = "/etc/tc-limit/config.yaml"
MBIT_TO_BPS = 125_000  # 1 Mbps = 125,000 B/s
MB_TO_BYTES = 1_000_000

# Built-in defaults (lowest priority) — all time values in seconds
DEFAULTS: Dict[str, Any] = {
    "limits": {"higher": 150, "lower": 110, "threshold": 120},
    "window": {"duration": 5 * 60, "interval": 5},
    "burst": {"enabled": False, "window": 3 * 60, "threshold_mb": 1024},
    "cooldown": 3 * 60,
    "recovery_steps": 1,
    "network": {"interface": "", "burst_kbit": 16},
    "runtime": {
        "dry_run": False,
        "log_level": "INFO",
        "state_file": "/run/tc-limit/state.json",
        "pid_file": "/run/tc-limit/daemon.pid",
    },
    "storage": {
        "enabled": False,
        "path": "/var/lib/tc-limit/metrics.db",
        "commit_interval": 60,
        "retention_days": 90,
    },
}


# ── Expression parser ────────────────────────────────────────────────────


def _parse_expr(value: Any) -> int:
    """Parse a config value that may be a bare int or a "*" expression string.

    >>> _parse_expr(120)
    120
    >>> _parse_expr("5 * 60")
    300
    >>> _parse_expr("1 * 1000")
    1000
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if not isinstance(value, str):
        raise ValueError(f"Expected a number or expression, got {type(value).__name__}")
    s = value.strip()
    if "*" not in s:
        return int(s)
    parts = s.split("*")
    if len(parts) != 2:
        raise ValueError(f"Expression must be 'a * b', got: {s!r}")
    return int(parts[0].strip()) * int(parts[1].strip())


# ── Types ─────────────────────────────────────────────────────────────────


class LogLevel(Enum):
    ERROR = 0
    WARN = 1
    INFO = 2
    DEBUG = 3

    @classmethod
    def from_string(cls, s: str) -> "LogLevel":
        try:
            return cls[s.upper()]
        except KeyError:
            logger.warning("Unknown log level %r, falling back to INFO", s)
            return cls.INFO


@dataclass
class LimitsConfig:
    higher: int = 150      # Mbps
    lower: int = 110       # Mbps
    threshold: int = 120   # Mbps


@dataclass
class WindowConfig:
    duration: int = 300    # seconds
    interval: int = 5      # seconds


@dataclass
class BurstConfig:
    enabled: bool = False
    window: int = 180          # seconds
    threshold_mb: int = 1024   # MB


@dataclass
class NetworkConfig:
    interface: str = ""
    burst_kbit: int = 16


@dataclass
class RuntimeConfig:
    dry_run: bool = False
    log_level: LogLevel = LogLevel.INFO
    state_file: str = "/run/tc-limit/state.json"
    pid_file: str = "/run/tc-limit/daemon.pid"


@dataclass
class StorageConfig:
    enabled: bool = False
    path: str = "/var/lib/tc-limit/metrics.db"
    commit_interval: int = 60
    retention_days: int = 90


@dataclass
class Config:
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    window: WindowConfig = field(default_factory=WindowConfig)
    burst: BurstConfig = field(default_factory=BurstConfig)
    cooldown: int = 180            # seconds
    recovery_steps: int = 1
    network: NetworkConfig = field(default_factory=NetworkConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)

    # Derived (populated after validation)
    buf_size: int = 0              # bandwidth ring buffer slots
    threshold_bps: int = 0
    burst_buf_size: int = 0        # burst ring buffer slots
    burst_threshold_bytes: int = 0  # burst threshold in bytes

    # Path the config was loaded from
    _source_path: Optional[str] = field(default=None, repr=False)


# ── Merging ───────────────────────────────────────────────────────────────


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _apply_cli_overrides(raw: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    for key, val in overrides.items():
        if val is None:
            continue
        if key == "higher_limit":
            raw.setdefault("limits", {})["higher"] = val
        elif key == "lower_limit":
            raw.setdefault("limits", {})["lower"] = val
        elif key == "threshold":
            raw.setdefault("limits", {})["threshold"] = val
        elif key == "window_duration":
            raw.setdefault("window", {})["duration"] = val
        elif key == "window_interval":
            raw.setdefault("window", {})["interval"] = val
        elif key == "cooldown":
            raw["cooldown"] = val
        elif key == "recovery_steps":
            raw["recovery_steps"] = val
        elif key == "interface":
            raw.setdefault("network", {})["interface"] = val
        elif key == "burst_kbit":
            raw.setdefault("network", {})["burst_kbit"] = val
        elif key == "burst_enabled":
            raw.setdefault("burst", {})["enabled"] = val
        elif key == "burst_window":
            raw.setdefault("burst", {})["window"] = val
        elif key == "burst_threshold_mb":
            raw.setdefault("burst", {})["threshold_mb"] = val
        elif key == "dry_run":
            raw.setdefault("runtime", {})["dry_run"] = val
        elif key == "log_level":
            raw.setdefault("runtime", {})["log_level"] = val
        elif key == "state_file":
            raw.setdefault("runtime", {})["state_file"] = val
        elif key == "pid_file":
            raw.setdefault("runtime", {})["pid_file"] = val
    return raw


# ── Validation ────────────────────────────────────────────────────────────


def _validate_positive_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer, got {type(value).__name__}")
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")
    return value


def _validate_and_derive(raw: Dict[str, Any], source: Optional[str]) -> Config:
    # Limits
    limits_raw = raw.get("limits", {})
    higher = _validate_positive_int(
        _parse_expr(limits_raw.get("higher", 150)), "limits.higher")
    lower = _validate_positive_int(
        _parse_expr(limits_raw.get("lower", 110)), "limits.lower")
    threshold = _validate_positive_int(
        _parse_expr(limits_raw.get("threshold", 120)), "limits.threshold")

    if higher <= threshold:
        raise ValueError(f"limits.higher ({higher}) must be > limits.threshold ({threshold})")
    if threshold <= lower:
        raise ValueError(f"limits.threshold ({threshold}) must be > limits.lower ({lower})")

    # Window (seconds)
    window_raw = raw.get("window", {})
    win_duration = _validate_positive_int(
        _parse_expr(window_raw.get("duration", 300)), "window.duration")
    win_interval = _validate_positive_int(
        _parse_expr(window_raw.get("interval", 5)), "window.interval")
    if win_interval < 1:
        raise ValueError(f"window.interval must be >= 1, got {win_interval}")

    # Burst
    burst_raw = raw.get("burst", {})
    burst_enabled = bool(burst_raw.get("enabled", False))
    burst_window = _validate_positive_int(
        _parse_expr(burst_raw.get("window", 180)), "burst.window")
    burst_threshold_mb = _validate_positive_int(
        _parse_expr(burst_raw.get("threshold_mb", 1024)), "burst.threshold_mb")

    # Cooldown (seconds)
    cooldown = _validate_positive_int(
        _parse_expr(raw.get("cooldown", 180)), "cooldown")

    # Recovery steps
    recovery_steps = int(raw.get("recovery_steps", 1))
    if recovery_steps < 1:
        raise ValueError(f"recovery_steps must be >= 1, got {recovery_steps}")

    # Network
    net_raw = raw.get("network", {})
    iface = net_raw.get("interface", "")
    if not isinstance(iface, str):
        raise ValueError(f"network.interface must be a string, got {type(iface).__name__}")
    burst_kbit = _validate_positive_int(_parse_expr(net_raw.get("burst_kbit", 16)), "network.burst_kbit")

    # Runtime
    runtime_raw = raw.get("runtime", {})
    dry_run = bool(runtime_raw.get("dry_run", False))
    log_level = LogLevel.from_string(str(runtime_raw.get("log_level", "INFO")))
    state_file = str(runtime_raw.get("state_file", "/run/tc-limit/state.json"))
    pid_file = str(runtime_raw.get("pid_file", "/run/tc-limit/daemon.pid"))

    # Storage
    storage_raw = raw.get("storage", {})
    storage_enabled = bool(storage_raw.get("enabled", False))
    storage_path = str(storage_raw.get("path", "/var/lib/tc-limit/metrics.db"))
    storage_commit_interval = int(storage_raw.get("commit_interval", 60))
    storage_retention = int(storage_raw.get("retention_days", 90))

    # Build
    cfg = Config(
        limits=LimitsConfig(higher=higher, lower=lower, threshold=threshold),
        window=WindowConfig(duration=win_duration, interval=win_interval),
        burst=BurstConfig(enabled=burst_enabled, window=burst_window,
                          threshold_mb=burst_threshold_mb),
        cooldown=cooldown,
        recovery_steps=recovery_steps,
        network=NetworkConfig(interface=iface, burst_kbit=burst_kbit),
        runtime=RuntimeConfig(dry_run=dry_run, log_level=log_level,
                              state_file=state_file, pid_file=pid_file),
        storage=StorageConfig(enabled=storage_enabled, path=storage_path,
                              commit_interval=storage_commit_interval,
                              retention_days=storage_retention),
        _source_path=source,
    )

    # Derived values
    cfg.buf_size = win_duration // win_interval
    cfg.threshold_bps = threshold * MBIT_TO_BPS
    cfg.burst_buf_size = burst_window // win_interval
    cfg.burst_threshold_bytes = burst_threshold_mb * MB_TO_BYTES

    return cfg


# ── Public API ────────────────────────────────────────────────────────────


def load_config(
    config_path: Optional[str] = None,
    cli_overrides: Optional[Dict[str, Any]] = None,
) -> Config:
    """Load and validate configuration.

    Priority: CLI overrides > config file > built-in defaults.

    When *config_path* points to a non-existent file a warning is logged
    and built-in defaults are used instead.
    """
    raw: Dict[str, Any] = dict(DEFAULTS)

    path = config_path or DEFAULT_CONFIG_PATH
    if path:
        try:
            with open(path, "r") as fh:
                file_raw = yaml.safe_load(fh) or {}
            raw = _deep_merge(raw, file_raw)
        except FileNotFoundError:
            if config_path is not None:
                logger.warning("Config file %r not found, using built-in defaults", path)
            path = "<defaults>"
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML in {path}: {exc}") from exc

    if cli_overrides:
        raw = _apply_cli_overrides(raw, cli_overrides)

    return _validate_and_derive(raw, source=path)


def reload_config(previous: Config, config_path: Optional[str] = None) -> Config:
    """Reload configuration from disk for hot-reload (SIGHUP).

    Non-hot-reloadable params (window.*, burst.*, recovery_steps)
    are carried forward from *previous*.
    """
    new = load_config(config_path=config_path or previous._source_path)

    # Preserve non-hot-reloadable values
    for attr in ("duration", "interval"):
        old_val = getattr(previous.window, attr)
        new_val = getattr(new.window, attr)
        if new_val != old_val:
            logger.warning("window.%s changed (%d→%d) — requires restart; keeping old value",
                           attr, old_val, new_val)
            setattr(new.window, attr, old_val)

    if new.burst != previous.burst:
        logger.warning("burst config changed — requires restart; keeping old value")
        new.burst = previous.burst

    if new.recovery_steps != previous.recovery_steps:
        logger.warning("recovery_steps changed — requires restart; keeping old value")
        new.recovery_steps = previous.recovery_steps

    # Recompute derived
    new.buf_size = new.window.duration // new.window.interval
    new.threshold_bps = new.limits.threshold * MBIT_TO_BPS
    new.burst_buf_size = new.burst.window // new.window.interval
    new.burst_threshold_bytes = new.burst.threshold_mb * MB_TO_BYTES

    logger.info(
        "Config reloaded: higher=%dM lower=%dM threshold=%dM cooldown=%ds steps=%d",
        new.limits.higher, new.limits.lower, new.limits.threshold,
        new.cooldown, new.recovery_steps,
    )
    return new
