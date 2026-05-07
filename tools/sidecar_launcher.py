"""
sidecar_launcher.py — TS_Engine Sidecar Startup Launcher

Invoked by Windows Task Scheduler every 5 minutes (mirrors TS_Watchdog_Guard pattern).

Responsibilities (in order):
  1. FX market hours gate — skip outside Mon 00:00 to Fri 22:00 UTC
  2. MT5 process guard — skip if terminal64.exe not running
  3. Single-instance guard — skip if live_runtime.runner already running
     (WMIC cmdline scan, NOT a PID file — avoids stale-file false negatives)
  4. Start sidecar as detached process, stdout/stderr redirected to log files
  5. Write sidecar PID to runtime_logs/sidecar.pid on successful start

Design:
  - Idempotent: safe to run every 5 minutes; exits in <2s if sidecar is alive
  - No MT5 API probe (read-only sidecar attaches to existing terminal — no login)
  - Logs every decision to runtime_logs/sidecar_launcher.log with UTC timestamps
  - Suppresses console windows (CREATE_NO_WINDOW) for all subprocesses
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
TS_ENGINE_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_LOGS   = TS_ENGINE_ROOT / "runtime_logs"
LOG_FILE       = RUNTIME_LOGS / "sidecar_launcher.log"
SIDECAR_PID    = RUNTIME_LOGS / "sidecar.pid"
SIDECAR_LOG    = RUNTIME_LOGS / "sidecar.log"
SIDECAR_ERR    = RUNTIME_LOGS / "sidecar_err.log"

PYTHON_EXE = Path(sys.executable)         # same interpreter that runs this script
MT5_PROCESS = "terminal64.exe"

# Suppress console windows when spawning child processes from a windowless
# parent (pythonw.exe / Task Scheduler context).
_NO_WIN = subprocess.CREATE_NO_WINDOW

# Cmdline signatures to identify the sidecar process.
# Matches both `python -m live_runtime.runner` and the package form.
_SIDECAR_SIGS = ("live_runtime.runner",)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def _log(msg: str) -> None:
    ts   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{ts} | SIDECAR_LAUNCHER | {msg}"
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
    weekday = now.weekday()  # 0=Mon … 6=Sun
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
# Step 2 — MT5 process check
# ---------------------------------------------------------------------------
def _mt5_running() -> bool:
    """Return True if terminal64.exe is in the process list."""
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {MT5_PROCESS}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=10,
            creationflags=_NO_WIN,
        )
        if MT5_PROCESS.lower() in r.stdout.lower():
            _log(f"MT5_PROC_FOUND | {MT5_PROCESS} is running")
            return True
    except Exception as e:
        _log(f"MT5_PROC_CHECK_ERROR | {type(e).__name__}: {e}")
        return True  # fail-safe: don't block launch on scan error
    _log(f"MT5_PROC_ABSENT | {MT5_PROCESS} not running — sidecar requires MT5 | skipping")
    return False


# ---------------------------------------------------------------------------
# Step 3 — Single-instance guard
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


def _find_sidecar_pids() -> list[int]:
    """Return PIDs of running live_runtime.runner processes via WMIC cmdline scan."""
    pids: list[int] = []
    try:
        r = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'", "get",
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
                    for sig in _SIDECAR_SIGS:
                        if sig.lower() in cmdline_lower:
                            pids.append(current_pid)
                            break
                current_cmdline = ""
                current_pid = None
    except Exception as e:
        _log(f"CMDLINE_SCAN_ERROR | {type(e).__name__}: {e}")
    return pids


def _sidecar_already_running() -> bool:
    """
    Return True if exactly one sidecar process is alive and we should skip launch.
    Log DUPLICATE_SIDECAR_DETECTED if more than one is found (manual kill needed).
    Falls back to PID file if WMIC scan finds nothing.
    """
    live_pids = _find_sidecar_pids()

    if len(live_pids) >= 2:
        _log(f"DUPLICATE_SIDECAR_DETECTED | pids={live_pids} | refusing to spawn more — manual kill required")
        return True

    if len(live_pids) == 1:
        pid = live_pids[0]
        _log(f"SIDECAR_ALREADY_RUNNING | pid={pid} | skipping")
        # Keep PID file current
        try:
            SIDECAR_PID.write_text(str(pid), encoding="utf-8")
        except Exception:
            pass
        return True

    # WMIC found nothing. Sanity-check PID file before launching.
    if SIDECAR_PID.exists():
        try:
            pid = int(SIDECAR_PID.read_text(encoding="utf-8").strip())
            if _pid_is_alive(pid):
                _log(f"SIDECAR_PID_FILE_ALIVE | pid={pid} | scan missed it — skipping")
                return True
        except Exception:
            pass

    return False


# ---------------------------------------------------------------------------
# Step 4 — Launch sidecar
# ---------------------------------------------------------------------------
def _launch_sidecar() -> bool:
    """
    Start live_runtime.runner as a detached process from TS_ENGINE_ROOT.
    Redirects stdout → sidecar.log, stderr → sidecar_err.log.
    Writes PID to runtime_logs/sidecar.pid on success.
    Returns True if launched successfully.
    """
    try:
        RUNTIME_LOGS.mkdir(parents=True, exist_ok=True)
        stdout_f = open(SIDECAR_LOG, "a", encoding="utf-8")
        stderr_f = open(SIDECAR_ERR, "a", encoding="utf-8")

        proc = subprocess.Popen(
            [str(PYTHON_EXE), "-m", "live_runtime.runner"],
            cwd=str(TS_ENGINE_ROOT),
            stdout=stdout_f,
            stderr=stderr_f,
            creationflags=subprocess.DETACHED_PROCESS
                          | subprocess.CREATE_NEW_PROCESS_GROUP
                          | _NO_WIN,
        )

        # Write PID file immediately so the next launcher run sees it
        SIDECAR_PID.write_text(str(proc.pid), encoding="utf-8")

        _log(f"SIDECAR_STARTED | pid={proc.pid} | cwd={TS_ENGINE_ROOT} | "
             f"stdout={SIDECAR_LOG} | stderr={SIDECAR_ERR}")
        return True

    except Exception as e:
        _log(f"SIDECAR_LAUNCH_FAILED | {type(e).__name__}: {e}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    _log("LAUNCHER_START")

    if not _fx_market_open():
        _log("LAUNCHER_DONE | reason=market_closed")
        return 0

    if not _mt5_running():
        _log("LAUNCHER_DONE | reason=mt5_absent")
        return 0

    if _sidecar_already_running():
        _log("LAUNCHER_DONE | reason=sidecar_running")
        return 0

    _log("SIDECAR_NOT_RUNNING | launching...")
    ok = _launch_sidecar()
    _log(f"LAUNCHER_DONE | reason={'sidecar_started' if ok else 'launch_failed'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
