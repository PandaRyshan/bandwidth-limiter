# tc_limit Phase 3 — Burst Detection, Recovery Steps, Templates, Unified Units

**Date:** 2026-07-03
**Status:** Draft

---

## 1. Overview

Three improvements to the Phase 1–2 Python daemon:

1. **Burst detection** — second trigger condition: if a configurable byte total passes
   through a short sliding window, enter LIMITED.  OR relationship with existing
   bandwidth-average trigger.

2. **Recovery steps** — when leaving LIMITED, optionally ramp back to `higher` in
   N equal steps instead of jumping directly.

3. **Configuration templates** — `default` and `light` presets for `setup.sh`.

4. **Unified units + expression parser** — all time values in seconds, all
   traffic values in MB, with optional `*` expressions in the YAML config.

---

## 2. Unified Units & Expression Parser

### Unit Convention

| Category   | Unit    | Examples                   |
|------------|---------|----------------------------|
| Time       | seconds | `5`, `3 * 60`, `180`      |
| Bandwidth  | Mbps    | `150`, `100`               |
| Traffic    | MB      | `1024`, `1 * 1000`         |
| Burst      | kbit    | `16` (unchanged)           |

### Expression Parser

A value in YAML can be a bare integer (`120`) or a string expression (`"5 * 60"`).

- Only `*` is supported (no `+`, `-`, `/`).
- Whitespace around `*` is optional.
- `_parse_expr(value) → int` handles both cases.

```python
def _parse_expr(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, str) and "*" in value:
        parts = value.split("*")
        if len(parts) != 2:
            raise ValueError(...)
        return int(parts[0].strip()) * int(parts[1].strip())
    return int(value)
```

### Config Schema (after changes)

```yaml
limits:
  higher:     150           # Mbps
  lower:      110           # Mbps
  threshold:  120           # Mbps

window:
  duration:  "5 * 60"       # seconds (was minutes)
  interval:   5             # seconds

burst:
  enabled:     false
  window:      "3 * 60"     # seconds
  threshold_mb: 1024        # MB  (= 1 GB)

cooldown:      "3 * 60"     # seconds (was minutes)
recovery_steps: 1           # 1 = single-step (backward compatible)

network:
  interface:   ""
  burst_kbit:  16
```

---

## 3. Burst Detection

### Mechanism

A second `RingBuffer` runs alongside the existing bandwidth ring buffer.
Both share the same sampling interval (e.g. 5 s).

- Burst window: `burst.window` seconds → `slots = burst.window / interval`
- Each sample: `burst_buffer.push(delta_bytes)`
- Trigger: `burst_buffer.sum() > burst.threshold_mb * 1_000_000`

### State Machine (OR of two conditions)

```
NORMAL:
  if (bandwidth_avg > threshold_mbps) OR (burst_bytes > threshold_mb_MB):
    → LIMITED(lower, current_step=0)
```

`current_step` tracks the recovery step (0 = fully limited).

---

## 4. Recovery Steps

### Configuration

- `recovery_steps: 1` → single step (backward compatible, no ramp).
- `recovery_steps: N` → ramp from `lower` to `higher` in N equal steps.

### Step Size

```
step_size = (higher - lower) / recovery_steps
step_cooldown = cooldown_seconds / recovery_steps
```

### Behavior

- On entering LIMITED: `current_step = 0`, rate = `lower`.
- After each `step_cooldown` expires without re-trigger:
  - `current_step += 1`
  - new rate = `lower + current_step * step_size`
  - if `current_step == recovery_steps` → NORMAL (full recovery)
- If any trigger fires during ramp → `current_step = 0`, rate = `lower`, restart cooldown.

### Calculation Example

```
higher = 150, lower = 100, recovery_steps = 2
step_size = 25, step_cooldown = cooldown / 2

Enter LIMITED → rate=100
After cooldown/2   → rate=125 (step 1)
After cooldown/2   → rate=150 → NORMAL
```

---

## 5. Configuration Templates

### File: `src/tc_limit/templates.py`

```python
TEMPLATES = {
    "default": {
        "higher": 150, "lower": 110, "threshold": 120,
        "window_duration": 5 * 60, "interval": 5,
        "cooldown": 3 * 60,
        "burst_enabled": False, "burst_window": 3 * 60, "burst_threshold_mb": 1024,
        "recovery_steps": 1,
    },
    "light": {
        "higher": 75, "lower": 55, "threshold": 60,
        "window_duration": 3 * 60, "interval": 10,
        "cooldown": 2 * 60,
        "burst_enabled": False, "burst_window": 3 * 60, "burst_threshold_mb": 512,
        "recovery_steps": 1,
    },
}
```

### `setup.sh` Integration

```bash
sudo bash setup.sh install --template light
```

setup.sh reads `templates.py`, invokes a small Python helper to generate a
`config.yaml` from the selected template, and writes it to `/etc/tc-limit/`.

---

## 6. Implementation Plan

1. `config.py` — expression parser, BurstConfig, recovery_steps, unit conversion
2. `templates.py` — template dicts
3. `daemon.py` — burst ring buffer, burst evaluation, recovery steps
4. `setup.sh` — `--template` flag
5. `config.example.yaml` — updated schema
6. Tests — expression parser, burst state machine, recovery steps
