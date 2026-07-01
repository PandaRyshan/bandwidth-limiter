"""Tests for tc_limit.config."""

from __future__ import annotations

import pytest
import yaml

from tc_limit.config import (
    Config, LimitsConfig, WindowConfig, NetworkConfig, RuntimeConfig,
    LogLevel, MBIT_TO_BPS, load_config, reload_config, _deep_merge,
    _validate_positive_int,
)


# ── LogLevel ───────────────────────────────────────────────────────────────

class TestLogLevel:
    def test_from_string_valid(self):
        assert LogLevel.from_string("DEBUG") == LogLevel.DEBUG
        assert LogLevel.from_string("info") == LogLevel.INFO
        assert LogLevel.from_string("WARN") == LogLevel.WARN
        assert LogLevel.from_string("ERROR") == LogLevel.ERROR

    def test_from_string_invalid_falls_back(self):
        assert LogLevel.from_string("TRACE") == LogLevel.INFO
        assert LogLevel.from_string("") == LogLevel.INFO


# ── Validation helpers ─────────────────────────────────────────────────────

class TestValidatePositiveInt:
    def test_valid(self):
        assert _validate_positive_int(42, "x") == 42
        assert _validate_positive_int(1, "x") == 1

    def test_negative(self):
        with pytest.raises(ValueError, match="must be positive"):
            _validate_positive_int(-1, "x")

    def test_zero(self):
        with pytest.raises(ValueError, match="must be positive"):
            _validate_positive_int(0, "x")

    def test_bool_fails(self):
        with pytest.raises(ValueError, match="must be an integer"):
            _validate_positive_int(True, "x")

    def test_float_fails(self):
        with pytest.raises(ValueError, match="must be an integer"):
            _validate_positive_int(3.5, "x")


# ── Deep merge ─────────────────────────────────────────────────────────────

class TestDeepMerge:
    def test_flat_override(self):
        base = {"a": 1, "b": 2}
        ovr = {"a": 99}
        assert _deep_merge(base, ovr) == {"a": 99, "b": 2}

    def test_nested_override(self):
        base = {"limits": {"higher": 150, "lower": 110}}
        ovr = {"limits": {"higher": 200}}
        assert _deep_merge(base, ovr) == {"limits": {"higher": 200, "lower": 110}}

    def test_new_key(self):
        base = {"a": 1}
        ovr = {"b": 2}
        assert _deep_merge(base, ovr) == {"a": 1, "b": 2}


# ── load_config ────────────────────────────────────────────────────────────

class TestLoadConfig:
    """Test config loading with defaults only (no file required)."""

    def test_defaults_only(self, tmp_path):
        # Use an empty config file to get default values for all fields
        yaml_path = tmp_path / "defaults.yaml"
        yaml_path.write_text("{}")
        cfg = load_config(config_path=str(yaml_path))
        assert cfg.limits.higher == 150
        assert cfg.limits.lower == 110
        assert cfg.limits.threshold == 120
        assert cfg.window.duration == 5
        assert cfg.window.interval == 10
        assert cfg.cooldown == 3
        assert cfg.network.interface == ""
        assert cfg.network.burst_kbit == 16
        assert cfg.runtime.dry_run is False
        assert cfg.runtime.log_level == LogLevel.INFO

    def test_derived_values(self, tmp_path):
        yaml_path = tmp_path / "defaults.yaml"
        yaml_path.write_text("{}")
        cfg = load_config(config_path=str(yaml_path))
        # duration=5min, interval=10s → buf_size = 5*60/10 = 30
        assert cfg.buf_size == 30
        # threshold=120Mbps → 120 * 125000 = 15_000_000 bps
        assert cfg.threshold_bps == 15_000_000
        assert cfg.window_seconds == 300
        assert cfg.cooldown_seconds == 180

    def test_from_yaml_file(self, tmp_path):
        yaml_path = tmp_path / "test.yaml"
        yaml_path.write_text(yaml.dump({
            "limits": {"higher": 200, "lower": 100, "threshold": 150},
            "window": {"duration": 10, "interval": 5},
            "cooldown": 5,
            "network": {"interface": "eth0", "burst_kbit": 32},
            "runtime": {"dry_run": True, "log_level": "DEBUG"},
        }))
        cfg = load_config(config_path=str(yaml_path))
        assert cfg.limits.higher == 200
        assert cfg.limits.lower == 100
        assert cfg.limits.threshold == 150
        assert cfg.window.duration == 10
        assert cfg.window.interval == 5
        assert cfg.cooldown == 5
        assert cfg.network.interface == "eth0"
        assert cfg.network.burst_kbit == 32
        assert cfg.runtime.dry_run is True
        assert cfg.runtime.log_level == LogLevel.DEBUG
        assert cfg.buf_size == 10 * 60 // 5  # 120
        assert cfg.cooldown_seconds == 300

    def test_cli_overrides(self, tmp_path):
        yaml_path = tmp_path / "test.yaml"
        yaml_path.write_text(yaml.dump({
            "limits": {"higher": 200, "lower": 100, "threshold": 150},
        }))
        cli = {"higher_limit": 250, "dry_run": True}
        cfg = load_config(config_path=str(yaml_path), cli_overrides=cli)
        assert cfg.limits.higher == 250  # CLI overrides file
        assert cfg.limits.lower == 100   # unchanged
        assert cfg.runtime.dry_run is True

    def test_validation_higher_le_threshold(self, tmp_path):
        yaml_path = tmp_path / "test.yaml"
        yaml_path.write_text(yaml.dump({
            "limits": {"higher": 100, "lower": 50, "threshold": 150},
        }))
        with pytest.raises(ValueError, match="higher.*must be >"):
            load_config(config_path=str(yaml_path))

    def test_validation_threshold_le_lower(self, tmp_path):
        yaml_path = tmp_path / "test.yaml"
        yaml_path.write_text(yaml.dump({
            "limits": {"higher": 200, "lower": 150, "threshold": 150},
        }))
        with pytest.raises(ValueError, match="threshold.*must be >"):
            load_config(config_path=str(yaml_path))

    def test_explicit_path_missing_falls_back(self, tmp_path):
        """Missing explicit path should fall back to defaults, not crash."""
        cfg = load_config(config_path=str(tmp_path / "missing.yaml"))
        # Should use built-in defaults
        assert cfg.limits.higher == 150
        assert cfg.limits.threshold == 120


# ── reload_config ──────────────────────────────────────────────────────────

class TestReloadConfig:
    def test_reload_preserves_window_on_change(self, tmp_path):
        yaml_path = tmp_path / "test.yaml"
        yaml_path.write_text(yaml.dump({
            "window": {"duration": 5, "interval": 10},
        }))
        cfg = load_config(config_path=str(yaml_path))
        assert cfg.buf_size == 30

        # Change window duration in file
        yaml_path.write_text(yaml.dump({
            "limits": {"higher": 200, "lower": 100, "threshold": 150},
            "window": {"duration": 10, "interval": 5},
        }))
        new = reload_config(cfg, config_path=str(yaml_path))
        # Window should NOT have changed (non-hot-reloadable)
        assert new.window.duration == 5
        assert new.window.interval == 10
        assert new.buf_size == 30
        # Hot-reloadable params SHOULD have changed
        assert new.limits.higher == 200

    def test_reload_hot_params_changed(self, tmp_path):
        yaml_path = tmp_path / "test.yaml"
        yaml_path.write_text(yaml.dump({
            "limits": {"higher": 150, "lower": 110, "threshold": 120},
            "cooldown": 3,
        }))
        cfg = load_config(config_path=str(yaml_path))

        yaml_path.write_text(yaml.dump({
            "limits": {"higher": 180, "lower": 130, "threshold": 140},
            "cooldown": 7,
        }))
        new = reload_config(cfg, config_path=str(yaml_path))
        assert new.limits.higher == 180
        assert new.limits.threshold == 140
        assert new.cooldown == 7
        assert new.cooldown_seconds == 420
        assert new.threshold_bps == 140 * MBIT_TO_BPS
