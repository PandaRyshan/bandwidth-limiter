# tc_limit Python Rewrite — Design Spec

**Date:** 2026-07-01  
**Status:** Draft (awaiting user review)  
**Target:** Phase 1 (migration) + Phase 2 (data collection & analysis)

---

## 1. Overview

Rewrite `src/old_scripts/tc_limit.sh` (705-line bash daemon) as a modular Python
package with systemd integration. Phase 1 preserves all existing functionality.
Phase 2 adds SQLite-backed data collection and statistical analysis.

**Core behavior (unchanged):** monitor network bandwidth via `/sys/class/net/`
byte counters using a sliding window ring buffer, adjust `tc` HTB limits
proactively to avoid triggering cloud provider penalty policies.

---

## 2. Architecture

### Project Structure

```
vps-controller/
├── pyproject.toml
├── setup.sh
├── config.example.yaml
├── README.md
├── Dockerfile
│
├── src/
│   ├── old_scripts/               # preserved, unchanged
│   │   ├── tc_limit.sh
│   │   ├── tc-limit.service
│   │   └── tc_limit.conf
│   │
│   └── tc_limit/                  # new Python package
│       ├── __init__.py
│       ├── cli.py                 # CLI entry point (argparse)
│       ├── config.py              # YAML loading, validation, hot-reload
│       ├── daemon.py              # main loop, signal handling, state machine
│       ├── executor.py            # tc operations (init/change/cleanup)
│       ├── sampler.py             # /sys counter reading + ring buffer
│       │
│       ├── storage/               # Phase 2
│       │   ├── __init__.py
│       │   ├── models.py          # schema definitions + migrations
│       │   └── queries.py         # aggregation queries
│       │
│       └── analyzer/              # Phase 2
│           ├── __init__.py
│           ├── volume.py          # traffic volume analysis
│           ├── bandwidth.py       # bandwidth rate analysis
│           └── report.py          # report composition
│
└── tests/
    ├── __init__.py
    ├── test_config.py
    ├── test_sampler.py
    ├── test_executor.py
    ├── test_daemon.py
    └── test_storage.py            # Phase 2
```

### Dependency Graph

```
cli ──→ daemon
          ├──→ config     (YAML load + validation)
          ├──→ executor   (tc_init, tc_change_rate, cleanup)
          ├──→ sampler    (read counters, ring buffer)
          └──→ storage    (Phase 2, optional)
```

### Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Config format | YAML | Human-readable, comment-friendly, simple hot-reload |
| Delivery | venv + setup.sh + symlink | Zero build deps, auditable, Debian-native |
| systemd Type | `notify` | sd_notify READY=1 signals startup complete |
| Concurrency model | Single-thread, `while True` + `sleep` | Same as current bash, predictable, simple |
| Testing | pytest unit + integration (mock) | No e2e; real-world testing done on actual VPS |

---

## 3. Configuration

### Schema (`/etc/tc-limit/config.yaml`)

```yaml
# ── Bandwidth Limits ──
limits:
  higher:    150       # Mbps — normal operating rate
  lower:     110       # Mbps — limited rate
  threshold: 120       # Mbps — alert line

# ── Sampling Window ──
window:
  duration:  5         # minutes — sliding window size
  interval:  10        # seconds — sampling interval

cooldown:    3         # minutes — cooldown after entering LIMITED

# ── Network ──
network:
  interface: ""        # empty = auto-detect default route interface
  burst_kbit: 16       # kbit — tc HTB token bucket burst

# ── Runtime ──
runtime:
  dry_run:   false
  log_level: "INFO"    # DEBUG | INFO | WARN | ERROR
  state_file: "/run/tc-limit/state.json"
  pid_file:   "/run/tc-limit/daemon.pid"

# ── Phase 2: Storage (reserved) ──
# storage:
#   enabled: true
#   path: "/var/lib/tc-limit/metrics.db"
#   commit_interval: 60
#   retention_days: 90
```

### Config Priority

```
CLI args  >  --config file  >  /etc/tc-limit/config.yaml  >  built-in defaults
```

### Hot-Reload

Triggered via `SIGHUP` (through `systemctl reload tc-limit` or `tc-limit reload` CLI).

| Parameter | Hot-reloadable |
|-----------|:---:|
| `limits.higher` | ✅ |
| `limits.lower` | ✅ |
| `limits.threshold` | ✅ |
| `cooldown` | ✅ |
| `window.duration` | ❌ (requires restart) |
| `window.interval` | ❌ (requires restart) |
| `network.*` | ❌ (requires restart) |
| `runtime.log_level` | ✅ |

---

## 4. State Machine

```
 ┌──────────┐     window_avg > threshold      ┌──────────┐
 │  NORMAL  │ ────────────────────────────────→│ LIMITED  │
 │          │                                   │          │
 │ 150 Mbps │ ←──────────────────────────────── │ 110 Mbps │
 └──────────┘    cooldown expires (3 min)      └──────────┘
                     buffer cleared
```

**NORMAL:**
- Buffer fills; once full, evaluate on every sample.
- If `window_sum > threshold_bps * window_seconds` → transition to LIMITED.

**LIMITED:**
- Every sample, check `now - cooldown_start >= cooldown_seconds`.
- If cooldown expired → transition to NORMAL, clear buffer.

---

## 5. Daemon Lifecycle

### Sampling Loop (every `window.interval` seconds)

```
sleep(interval)
  │
sampler.read()           → cur = tx + rx from /sys counters
  │
delta = cur - prev
  │
delta < 0?  ──yes──→ counter_wrap → clear buffer → continue
  │
sampler.push(delta)
  │
state machine evaluate
  │
every 60s: save_state() + summary log
  │
every commit_interval: storage.insert() (Phase 2)
```

### Signal Handling

| Signal | Action |
|--------|--------|
| `SIGTERM` / `SIGINT` | executor.cleanup() → release lock → exit 0 |
| `SIGHUP` | config.reload() → validate → apply hot-reloadable params |
| `SIGUSR1` | dump status to stderr (state, rate, window_avg) |

### Runtime Files

| File | Path | Purpose |
|------|------|---------|
| PID file | `/run/tc-limit/daemon.pid` | Process lock + daemon discovery |
| State file | `/run/tc-limit/state.json` | Persist state across restarts |
| Lock | Via PID file `flock` / `fcntl` | Single-instance enforcement |

### State Persistence Format (`state.json`)

```json
{
  "state": "NORMAL",
  "current_rate_mbps": 150,
  "threshold_mbps": 120,
  "window_avg_mbps": 98.3,
  "cooldown_seconds": 180,
  "cooldown_start": null,
  "sample_count": 8472,
  "updated_at": 1751395200.0
}
```

On daemon restart: if state was `LIMITED` and remaining cooldown > 0, resume in
`LIMITED` with remaining time.

---

## 6. CLI Interface

```
tc-limit daemon [--config PATH]          Start daemon (foreground)
tc-limit stop   [--config PATH]          Send SIGTERM to running daemon
tc-limit status [--config PATH]          Print daemon + tc status
tc-limit reload [--config PATH]          Send SIGHUP for config hot-reload
tc-limit report [--since DATE] [...]     Generate analysis report (Phase 2)
tc-limit --help
```

Backward compatibility: the old `--on / --off / --status / --reload` style can
be added as deprecated aliases if desired.

---

## 7. Setup & Deployment

### Filesystem Layout

```
/opt/tc-limit/
├── src/            # Python package (read-only)
├── venv/           # virtualenv with dependencies
│
/etc/tc-limit/
└── config.yaml     # user-editable configuration
│
/run/tc-limit/      # runtime files (tmpfs)
├── daemon.pid
└── state.json
│
/var/lib/tc-limit/  # Phase 2
└── metrics.db      # SQLite database
│
/usr/local/bin/
└── tc-limit  → /opt/tc-limit/venv/bin/tc-limit   # symlink

/etc/systemd/system/
└── tc-limit.service
```

### setup.sh

**`sudo bash setup.sh install`:**
1. Copy source to `/opt/tc-limit/src/`
2. `python3 -m venv /opt/tc-limit/venv`
3. `pip install /opt/tc-limit/src/`
4. Create `/etc/tc-limit/`, copy `config.example.yaml` → `config.yaml` if absent
5. Write systemd unit to `/etc/systemd/system/tc-limit.service`
6. `ln -sf /opt/tc-limit/venv/bin/tc-limit /usr/local/bin/tc-limit`
7. `systemctl daemon-reload && systemctl enable tc-limit`
8. `systemctl start tc-limit` (skip with `--no-start`)

**`sudo bash setup.sh uninstall`:**
1. `systemctl stop tc-limit && systemctl disable tc-limit`
2. Remove systemd unit
3. Remove symlink
4. `rm -rf /opt/tc-limit/`
5. Prompt before removing `/etc/tc-limit/` and `/run/tc-limit/`

### systemd Unit

```ini
[Unit]
Description=Smart Bandwidth Limit Daemon (tc)
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStart=/usr/local/bin/tc-limit daemon --config /etc/tc-limit/config.yaml
ExecReload=/bin/kill -HUP $MAINPID
ExecStop=/usr/local/bin/tc-limit stop --config /etc/tc-limit/config.yaml
Restart=always
RestartSec=5

ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/run/tc-limit /etc/tc-limit
NoNewPrivileges=true

StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

---

## 8. Testing Strategy

| Layer | Scope | Tools |
|-------|-------|-------|
| **Unit** | Config parsing, ring buffer math, state transitions, Mbps conversions | `pytest` |
| **Integration** | Daemon loop with mocked `/sys` counters + `tc` subprocess, signal dispatch | `pytest` + `unittest.mock` |
| **End-to-end** | Real tc qdisc creation, real traffic, real systemd | Manual on actual VPS |

Test files live in `tests/`, mirror the package structure.

---

## 9. Phase 2: Data Collection & Analysis

### SQLite Schema

```sql
-- Per-sample records (aggregated, one row per commit_interval)
CREATE TABLE samples (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    tx_bytes     INTEGER NOT NULL,
    rx_bytes     INTEGER NOT NULL,
    delta_bytes  INTEGER NOT NULL,
    rate_mbps    REAL NOT NULL,
    state        TEXT NOT NULL,
    limit_mbps   INTEGER NOT NULL,
    iface        TEXT NOT NULL
);
CREATE INDEX idx_samples_ts ON samples(ts);

-- State transitions
CREATE TABLE state_changes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               REAL NOT NULL,
    from_state       TEXT NOT NULL,
    to_state         TEXT NOT NULL,
    reason           TEXT,
    window_avg_mbps  REAL
);
CREATE INDEX idx_state_changes_ts ON state_changes(ts);

-- Daily aggregates
CREATE TABLE daily_summary (
    date             TEXT PRIMARY KEY,
    total_gb         REAL NOT NULL,
    peak_mbps        REAL NOT NULL,
    avg_mbps         REAL NOT NULL,
    limited_minutes  INTEGER NOT NULL,
    state_changes    INTEGER NOT NULL,
    sample_count     INTEGER NOT NULL
);
```

### Retention

| Table | Retention | Cleanup |
|-------|-----------|---------|
| `samples` | Configurable (default 90 days) | Pruned on each commit |
| `state_changes` | Permanent | Negligible row count |
| `daily_summary` | Permanent | One row per day |

### Analyzer Capabilities (Phase 2)

- `tc-limit report volume --days 7` → daily traffic totals (GB)
- `tc-limit report bandwidth --since "2026-06-25"` → bandwidth timeline
- `tc-limit report events` → list all state change events
- `tc-limit report summary` → aggregate overview (peak, avg, limited time)

---

## 10. Open Questions / Future Work

- **RTT / packet loss collection:** schema supports it via `samples` extension; add columns when needed
- **Web dashboard:** analyzer could later serve a lightweight HTTP API
- **Alerting:** future `notifier.py` module for webhook/email on state transitions
- **Multiple interface support:** current design uses one interface; multi-iface requires schema update
