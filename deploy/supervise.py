"""Supervise existing commands; scheduling and delivery remain in Django."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time


def enabled(value):
    return str(value).lower() in {"1", "true", "yes", "on"}


def commands(env, *, sqlite=True):
    """One process per enabled role; command defaults retain their intervals."""
    result = [("gunicorn", [
        "gunicorn", "reading_challenge.wsgi:application",
        "--pid", "/tmp/northbound-gunicorn.pid",
        "--bind", "0.0.0.0:" + env.get("PORT", "8000"),
        "--workers", env.get("WEB_CONCURRENCY", "2"),
        "--access-logfile", "-",
        "--access-logformat", '%(h)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s',
        "--error-logfile", "-",
    ])]
    roles = [
        ("backup", "NORTHBOUND_BACKUP_SCHEDULER_ENABLED", "1", "run_backup_scheduler", []),
        ("challenge", "NORTHBOUND_CHALLENGE_SCHEDULER_ENABLED", "1", "run_challenge_scheduler", []),
        ("hardcover", "NORTHBOUND_HARDCOVER_WRITEBACK_ENABLED", "1", "run_hardcover_sync_worker", []),
        ("discord-events", "NORTHBOUND_DISCORD_EVENTS_ENABLED", "0", "run_discord_events", ["--interval", "30"]),
        ("discord-scheduler", "NORTHBOUND_DISCORD_SCHEDULER_ENABLED", "0", "run_discord_scheduler", ["--interval", "30"]),
    ]
    for name, gate, default, command, args in roles:
        # The backup command intentionally exits on PostgreSQL. It is not a
        # required daemon there; avoid introducing a restart loop.
        if name == "backup" and not sqlite:
            continue
        if enabled(env.get(gate, default)):
            result.append((name, [sys.executable, "manage.py", command, *args]))
    return result


def _signal_group(process, signum):
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


def supervise(specs, *, shutdown_timeout=30.0):
    """Fail the service if any required child exits, even with status zero."""
    stopping = threading.Event()
    requested_signal = signal.SIGTERM
    children = []

    def request_stop(signum, _frame):
        nonlocal requested_signal
        requested_signal = signum
        stopping.set()

    previous = {s: signal.signal(s, request_stop) for s in (signal.SIGTERM, signal.SIGINT)}
    result = 0
    try:
        for name, argv in specs:
            if stopping.is_set():
                break
            process = subprocess.Popen(argv, start_new_session=True)
            children.append((name, process))
            print(f"Northbound supervisor: started {name}.", flush=True)
        while not stopping.is_set():
            for name, process in children:
                code = process.poll()
                if code is not None:
                    print(f"Northbound supervisor: {name} exited ({code}); stopping service.", file=sys.stderr, flush=True)
                    result = 1
                    stopping.set()
                    break
            stopping.wait(0.1)
    except Exception:
        # No argv, environment or exception values (which could contain secrets).
        print("Northbound supervisor: process startup/monitor failed; stopping service.", file=sys.stderr, flush=True)
        result = 1
    finally:
        deadline = time.monotonic() + shutdown_timeout
        for _, process in children:
            _signal_group(process, requested_signal)
        for _, process in children:
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
        # Also reach surviving descendants of a failed parent. Docker init
        # reaps orphaned grandchildren; direct children are waited here.
        for _, process in children:
            _signal_group(process, signal.SIGKILL)
        for _, process in children:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                print("Northbound supervisor: child exceeded shutdown deadline.", file=sys.stderr, flush=True)
                result = 1
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return result


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "reading_challenge.settings")
    try:
        from django.conf import settings
        sqlite = settings.DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3"
        timeout = float(os.environ.get("NORTHBOUND_SHUTDOWN_TIMEOUT", "30"))
        if not 0 < timeout <= 300:
            raise ValueError
    except Exception:
        print("Northbound supervisor: invalid runtime configuration.", file=sys.stderr, flush=True)
        return 1
    return supervise(commands(os.environ, sqlite=sqlite), shutdown_timeout=timeout)


if __name__ == "__main__":
    sys.exit(main())
