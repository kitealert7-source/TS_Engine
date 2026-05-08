"""
monitor_launcher.py — TS_Engine Parity Monitor Startup Launcher

Invoked by Windows Task Scheduler every 5 minutes (mirrors sidecar_launcher
pattern).  Brings the parity validator under the same operational discipline
as the engine it observes: monitor death is now self-healing within <= 5 min.

Responsibilities (in order):
  1. FX market hours gate — skip outside Mon 00:00 to Fri 22:00 UTC
     (no signals fire on the weekend, so a dead monitor can wait until Sun 22:00)
  2. Single-instance guard — skip if parity_monitor.monitor already running
     (WMIC cmdline scan, NOT a PID file — avoids stale-file false negatives)
  3. Liveness check on existing instance — if cmdline scan finds it but the
     monitor.log mtime is older than HEARTBEAT_STALE_S, declare it stuck and
     report DUPLICATE_MONITOR_DETECTED (operator must kill manually)
  4. Start monitor as detached process, stdout -> monitor.log, stderr -> monitor_err.log
  5. Write monitor PID to runtime_logs/monitor.pid on successful start

Why no MT5 dependency:
  The monitor reads only journal files (TS_Execution SignalJournal.jsonl and
  TS_Engine shadow_signal_journal.jsonl).  It has no MT5 connection, no
  network calls, no broker interaction.  The MT5 gate that sidecar_launcher
  uses is unnecessary here.

Why same FX gate anyway:
  When markets are closed nothing produces journal entries, so the monitor
  would just heartbeat over an unchanging dataset for 50 hours.  Letting it
  sleep weekends is cheaper and matches the sidecar's lifecycle.

Design:
  - Idempotent: safe to run every 5 minutes; exits in <2s if monitor is alive
  - Logs every decision to runtime_logs/monitor_launcher.log with UTC timestamps
  - Suppresses console windows (CREATE_NO_WINDOW) for all subprocesses
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
TS_ENGINE_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_LOGS   = TS_ENGINE_ROOT / "runtime_logs"
LOG_FILE       = RUNTIME_LOGS / "monitor_launcher.log"
MONITOR_PID    = RUNTIME_LOGS / "monitor.pid"
MONITOR_LOG    = RUNTIME_LOGS / "monitor.log"
MONITOR_ERR    = RUNTIME_LOGS / "monitor_err.log"

PYTHON_EXE = Path(sys.executable)         # same interpreter that runs this script

# Suppress console windows when spawning child processes from a windowless
# parent (pythonw.exe / Task Scheduler context).
_NO_WIN = subprocess.CREATE_NO_WINDOW

# Cmdline signature to identify the monitor process.
# Matches `python -m parity_monitor.monitor` (and the package form).
_MONITOR_SIGS = ("parity_monitor.monitor",)

# Heartbeat staleness threshold.  Monitor prints HB every 60s, so anything
# older than 5 minutes means the monitor has stopped responding even if
# its process is still in the tasklist.
_HEARTBEAT_STALE_S = 300


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def _log(msg: str) -> None:
    ts   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{ts} | MONITOR_LAUNCHER | {msg}"
    print(line, flush=True)
    try:
        RUNTIME_LOGS.mkdir(parents=True, exist_ok=True)
        try:
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > 2 * 1024 * 1024:
                lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
                LOG_FILE.write_text("\n".join(lines[-1000:]) + "\n", encoding="utf-8")
        except Exception:
            pass
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Step 1 — FX market hours gate
# ---------------------------------------------------------------------------
def _fx_market_open() -> bool:
    """FX market open: Mon 00:00 UTC through Fri 22:00 UTC."""
    now = datetime.now(timezone.utc)
    weekday = now.weekday()  # 0=Mon ... 6=Sun
    hour    = now.hour
    day     = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][weekday]

    if weekday == 4 and hour >= 22:
        _log(f"MARKET_GATE | day={day} | Allowed=NO (Fri after 22:00 UTC)")
        return False
    if weekday == 5:
        _log(f"MARKET_GATE | day={day} | Allowed=NO (Saturday)")
        return False
    if weekday == 6 and hour < 22:
        _log(f"MARKET_GATE | day={day} | Allowed=NO (Sun before 22:00 UTC)")
        return False

    _log(f"MARKET_GATE | day={day} | Allowed=YES")
    return True


# ---------------------------------------------------------------------------
# Step 2 — Single-instance guard (WMIC cmdline scan, with PID-file fallback)
# ---------------------------------------------------------------------------
def _pid_is_alive(pid: int) -> bool:
    """Tasklist liveness check with fail-safe ALIVE on error."""
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=10,
            creationflags=_NO_WIN,
        )
        if r.returncode == 0:
            return str(pid) in r.stdout
    except Exception:
        pass
    return True  # fail-safe: assume alive


def _find_monitor_pids() -> list[int]:
    """Return PIDs of running parity_monitor.monitor processes via WMIC scan.

    Scans both python.exe and pythonw.exe — the launcher uses pythonw under
    Task Scheduler, but a manually-started monitor may be under python.
    Excludes this process's own PID via os.getpid().
    """
    pids: list[int] = []
    try:
        # WMIC's where-clause cannot match multiple names directly; do two passes.
        for image in ("python.exe", "pythonw.exe"):
            r = subprocess.run(
                ["wmic", "process", "where", f"name='{image}'", "get",
                 "ProcessId,CommandLine", "/FORMAT:LIST"],
                capture_output=True, text=True, timeout=15,
                creationflags=_NO_WIN,
            )
            current_cmdline = ""
            current_pid: int | None = None
            for line in r.stdout.splitlines():
                line = line.strip()
                if line.startswith("CommandLine="):
                    current_cmdline = line[len("CommandLine="):]
                elif line.startswith("ProcessId="):
                    try:
                        current_pid = int(line[len("ProcessId="):])
                    except ValueError:
                        current_pid = None
                    if current_pid and current_pid != os.getpid():
                        cmdline_lower = current_cmdline.lower()
                        for sig in _MONITOR_SIGS:
                            if sig.lower() in cmdline_lower:
                                pids.append(current_pid)
                                break
                    current_cmdline = ""
                    current_pid = None
    except Exception as e:
        _log(f"CMDLINE_SCAN_ERROR | {type(e).__name__}: {e}")
    return pids


def _heartbeat_age_s() -> float | None:
    """Return seconds since last write to monitor.log, or None if unreadable."""
    try:
        if not MONITOR_LOG.exists():
            return None
        return time.time() - MONITOR_LOG.stat().st_mtime
    except Exception:
        return None


def _monitor_already_running() -> bool:
    """
    Return True if a single live monitor process is heartbeating and we
    should skip launch.

    Decision matrix:
      0 live + log fresh:    rare race (monitor died between scan and now);
                             treat as not-running, launch
      1 live + log fresh:    healthy — skip launch, refresh PID file
      1 live + log stale:    process exists but heartbeat dead (locked I/O,
                             deadlock, etc.).  Log MONITOR_STUCK; STILL skip
                             launch — operator must kill the stuck PID before
                             a fresh instance can take over.
      2+ live:               DUPLICATE_MONITOR_DETECTED, skip launch.
    """
    live_pids = _find_monitor_pids()
    hb_age    = _heartbeat_age_s()

    if len(live_pids) >= 2:
        _log(f"DUPLICATE_MONITOR_DETECTED | pids={live_pids} | refusing to spawn — manual kill required")
        return True

    if len(live_pids) == 1:
        pid = live_pids[0]
        if hb_age is not None and hb_age > _HEARTBEAT_STALE_S:
            _log(f"MONITOR_STUCK | pid={pid} | hb_age={hb_age:.0f}s > {_HEARTBEAT_STALE_S}s |"
                 f" process alive but heartbeat dead — manual kill required, NOT spawning replacement")
        else:
            _log(f"MONITOR_ALREADY_RUNNING | pid={pid} | hb_age={hb_age}")
        try:
            MONITOR_PID.write_text(str(pid), encoding="utf-8")
        except Exception:
            pass
        return True

    # WMIC found nothing.  Sanity-check PID file before launching.
    if MONITOR_PID.exists():
        try:
            pid = int(MONITOR_PID.read_text(encoding="utf-8").strip())
            if _pid_is_alive(pid):
                _log(f"MONITOR_PID_FILE_ALIVE | pid={pid} | scan missed it — skipping")
                return True
        except Exception:
            pass

    return False


# ---------------------------------------------------------------------------
# Step 3 — Launch monitor
# ---------------------------------------------------------------------------
def _launch_monitor() -> bool:
    """
    Start parity_monitor.monitor as a detached process from TS_ENGINE_ROOT.
    Redirects stdout -> monitor.log, stderr -> monitor_err.log.
    Writes PID to runtime_logs/monitor.pid on success.
    Returns True if launched successfully.
    """
    try:
        RUNTIME_LOGS.mkdir(parents=True, exist_ok=True)
        stdout_f = open(MONITOR_LOG, "a", encoding="utf-8")
        stderr_f = open(MONITOR_ERR, "a", encoding="utf-8")

        proc = subprocess.Popen(
            [str(PYTHON_EXE), "-u", "-m", "parity_monitor.monitor"],
            cwd=str(TS_ENGINE_ROOT),
            stdout=stdout_f,
            stderr=stderr_f,
            creationflags=subprocess.DETACHED_PROCESS
                          | subprocess.CREATE_NEW_PROCESS_GROUP
                          | _NO_WIN,
        )

        # Write PID file immediately so the next launcher run sees it
        MONITOR_PID.write_text(str(proc.pid), encoding="utf-8")

        _log(f"MONITOR_STARTED | pid={proc.pid} | cwd={TS_ENGINE_ROOT} | "
             f"stdout={MONITOR_LOG} | stderr={MONITOR_ERR}")
        return True

    except Exception as e:
        _log(f"MONITOR_LAUNCH_FAILED | {type(e).__name__}: {e}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    _log("LAUNCHER_START")

    if not _fx_market_open():
        _log("LAUNCHER_DONE | reason=market_closed")
        return 0

    if _monitor_already_running():
        _log("LAUNCHER_DONE | reason=monitor_running_or_stuck")
        return 0

    _log("MONITOR_NOT_RUNNING | launching...")
    ok = _launch_monitor()
    _log(f"LAUNCHER_DONE | reason={'monitor_started' if ok else 'launch_failed'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
