"""Configuration loading, validation, and hot-reload.

Config priority: CLI args > config file > built-in defaults.
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

DEFAULT_CONFIG_PATH = "/etc/tc_limit/config.yaml"
MBIT_TO_BPS = 125_000  # 1 Mbps = 125,000 B/s

# Built-in defaults (lowest priority)
DEFAULTS: Dict[str, Any] = {
    "limits": {"higher": 150, "lower": 110, "threshold": 120},
    "window": {"duration": 5, "interval": 10},
    "cooldown": 3,
    "network": {"interface": "", "burst_kbit": 16},
    "runtime": {
        "dry_run": False,
        "log_level": "INFO",
        "state_file": "/run/tc_limit/state.json",
        "pid_file": "/run/tc_limit/daemon.pid",
    },
}


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
    higher: int = 150
    lower: int = 110
    threshold: int = 120


@dataclass
class WindowConfig:
    duration: int = 5   # minutes
    interval: int = 10  # seconds


@dataclass
class NetworkConfig:
    interface: str = ""
    burst_kbit: int = 16


@dataclass
class RuntimeConfig:
    dry_run: bool = False
    log_level: LogLevel = LogLevel.INFO
    state_file: str = "/run/tc_limit/state.json"
    pid_file: str = "/run/tc_limit/daemon.pid"


@dataclass
class Config:
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    window: WindowConfig = field(default_factory=WindowConfig)
    cooldown: int = 3  # minutes
    network: NetworkConfig = field(default_factory=NetworkConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)

    # Derived (populated after validation)
    buf_size: int = 0
    threshold_bps: int = 0
    window_seconds: int = 0
    cooldown_seconds: int = 0

    # Path the config was loaded from
    _source_path: Optional[str] = field(default=None, repr=False)


# ── Merging ───────────────────────────────────────────────────────────────


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Merge *override* into *base*, returning a new dict."""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _apply_cli_overrides(raw: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    """Apply CLI-provided flat-key overrides on top of raw config dict."""
    for key, val in overrides.items():
        if val is None:
            continue
        # Map flat keys back to nested paths
        if key == "higher_limit":
            raw.setdefault("limits", {})
            raw["limits"]["higher"] = val
        elif key == "lower_limit":
            raw.setdefault("limits", {})
            raw["limits"]["lower"] = val
        elif key == "threshold":
            raw.setdefault("limits", {})
            raw["limits"]["threshold"] = val
        elif key == "window_duration":
            raw.setdefault("window", {})
            raw["window"]["duration"] = val
        elif key == "window_interval":
            raw.setdefault("window", {})
            raw["window"]["interval"] = val
        elif key == "cooldown":
            raw["cooldown"] = val
        elif key == "interface":
            raw.setdefault("network", {})
            raw["network"]["interface"] = val
        elif key == "burst_kbit":
            raw.setdefault("network", {})
            raw["network"]["burst_kbit"] = val
        elif key == "dry_run":
            raw.setdefault("runtime", {})
            raw["runtime"]["dry_run"] = val
        elif key == "log_level":
            raw.setdefault("runtime", {})
            raw["runtime"]["log_level"] = val
        elif key == "state_file":
            raw.setdefault("runtime", {})
            raw["runtime"]["state_file"] = val
        elif key == "pid_file":
            raw.setdefault("runtime", {})
            raw["runtime"]["pid_file"] = val
    return raw


# ── Validation ────────────────────────────────────────────────────────────


def _validate_positive_int(value: Any, name: str) -> int:
    """Ensure *value* is a positive integer.  Raises ValueError on failure."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer, got {type(value).__name__}")
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")
    return value


def _validate_and_derive(raw: Dict[str, Any], source: Optional[str]) -> Config:
    """Build a validated Config from a merged raw dict."""
    # Limits
    limits_raw = raw.get("limits", {})
    higher = _validate_positive_int(limits_raw.get("higher", 150), "limits.higher")
    lower = _validate_positive_int(limits_raw.get("lower", 110), "limits.lower")
    threshold = _validate_positive_int(limits_raw.get("threshold", 120), "limits.threshold")

    if higher <= threshold:
        raise ValueError(f"limits.higher ({higher}) must be > limits.threshold ({threshold})")
    if threshold <= lower:
        raise ValueError(f"limits.threshold ({threshold}) must be > limits.lower ({lower})")

    # Window
    window_raw = raw.get("window", {})
    win_duration = _validate_positive_int(window_raw.get("duration", 5), "window.duration")
    win_interval = _validate_positive_int(window_raw.get("interval", 10), "window.interval")
    if win_interval < 1:
        raise ValueError(f"window.interval must be >= 1, got {win_interval}")

    # Cooldown
    cooldown = _validate_positive_int(raw.get("cooldown", 3), "cooldown")

    # Network
    net_raw = raw.get("network", {})
    iface = net_raw.get("interface", "")
    if not isinstance(iface, str):
        raise ValueError(f"network.interface must be a string, got {type(iface).__name__}")
    burst_kbit = _validate_positive_int(net_raw.get("burst_kbit", 16), "network.burst_kbit")

    # Runtime
    runtime_raw = raw.get("runtime", {})
    dry_run = bool(runtime_raw.get("dry_run", False))
    log_level = LogLevel.from_string(str(runtime_raw.get("log_level", "INFO")))
    state_file = str(runtime_raw.get("state_file", "/run/tc_limit/state.json"))
    pid_file = str(runtime_raw.get("pid_file", "/run/tc_limit/daemon.pid"))

    # Build
    cfg = Config(
        limits=LimitsConfig(higher=higher, lower=lower, threshold=threshold),
        window=WindowConfig(duration=win_duration, interval=win_interval),
        cooldown=cooldown,
        network=NetworkConfig(interface=iface, burst_kbit=burst_kbit),
        runtime=RuntimeConfig(dry_run=dry_run, log_level=log_level, state_file=state_file, pid_file=pid_file),
        _source_path=source,
    )

    # Derived values
    cfg.buf_size = win_duration * 60 // win_interval
    cfg.threshold_bps = threshold * MBIT_TO_BPS
    cfg.window_seconds = win_duration * 60
    cfg.cooldown_seconds = cooldown * 60

    return cfg


# ── Public API ────────────────────────────────────────────────────────────


def load_config(
    config_path: Optional[str] = None,
    cli_overrides: Optional[Dict[str, Any]] = None,
) -> Config:
    """Load and validate configuration.

    Priority: CLI overrides > config file > built-in defaults.

    Args:
        config_path: Optional path to YAML file.
        cli_overrides: Flat dict of CLI arguments (e.g. {"higher_limit": 200}).

    Returns:
        Validated Config.

    Raises:
        ValueError: Configuration validation failed.
        FileNotFoundError: Explicit config_path does not exist.
    """
    raw: Dict[str, Any] = dict(DEFAULTS)

    # Layer 2: config file
    path = config_path or DEFAULT_CONFIG_PATH
    if path:
        try:
            with open(path, "r") as fh:
                file_raw = yaml.safe_load(fh) or {}
            raw = _deep_merge(raw, file_raw)
        except FileNotFoundError:
            if config_path is not None:
                raise
            # Default path missing → use defaults only, no error.
            path = "<defaults>"
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML in {path}: {exc}") from exc

    # Layer 3: CLI overrides
    if cli_overrides:
        raw = _apply_cli_overrides(raw, cli_overrides)

    return _validate_and_derive(raw, source=path)


def reload_config(previous: Config, config_path: Optional[str] = None) -> Config:
    """Reload configuration from disk for hot-reload (SIGHUP).

    Non-hot-reloadable params (window.*, network.interface, runtime.log_level
    for derived values) are carried forward from *previous*.
    """
    new = load_config(config_path=config_path or previous._source_path)

    # Preserve non-hot-reloadable derived values
    if new.window.duration != previous.window.duration:
        logger.warning(
            "window.duration changed (%d→%d) — requires restart; keeping old value",
            previous.window.duration, new.window.duration,
        )
        new.window.duration = previous.window.duration
        new.window_seconds = previous.window_seconds
        new.buf_size = previous.buf_size
    if new.window.interval != previous.window.interval:
        logger.warning(
            "window.interval changed (%d→%d) — requires restart; keeping old value",
            previous.window.interval, new.window.interval,
        )
        new.window.interval = previous.window.interval
        new.buf_size = previous.buf_size

    # Recompute derived (in case hot-reloadable params changed)
    new.threshold_bps = new.limits.threshold * MBIT_TO_BPS
    new.cooldown_seconds = new.cooldown * 60
    new.buf_size = new.window.duration * 60 // new.window.interval
    new.window_seconds = new.window.duration * 60

    logger.info(
        "Config reloaded: higher=%dM lower=%dM threshold=%dM cooldown=%dm",
        new.limits.higher, new.limits.lower, new.limits.threshold, new.cooldown,
    )
    return new
