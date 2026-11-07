#!/bin/sh
set -eu

python manage.py restore_pending_backup
python manage.py migrate --noinput
python manage.py safeguard_restored_hardcover_sync

backup_scheduler_pid=""
challenge_scheduler_pid=""
hardcover_sync_worker_pid=""
gunicorn_pid=""

enabled() {
    case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

shutdown_processes() {
    for process_pid in "$gunicorn_pid" "$backup_scheduler_pid" "$challenge_scheduler_pid" "$hardcover_sync_worker_pid"; do
        if [ -n "$process_pid" ]; then
            kill "$process_pid" 2>/dev/null || true
        fi
    done
    for process_pid in "$gunicorn_pid" "$backup_scheduler_pid" "$challenge_scheduler_pid" "$hardcover_sync_worker_pid"; do
        if [ -n "$process_pid" ]; then
            wait "$process_pid" 2>/dev/null || true
        fi
    done
}
trap shutdown_processes EXIT INT TERM

if enabled "${NORTHBOUND_BACKUP_SCHEDULER_ENABLED:-1}"; then
    python manage.py run_backup_scheduler &
    backup_scheduler_pid=$!
fi
if enabled "${NORTHBOUND_CHALLENGE_SCHEDULER_ENABLED:-1}"; then
    python manage.py run_challenge_scheduler &
    challenge_scheduler_pid=$!
fi
if enabled "${NORTHBOUND_HARDCOVER_WRITEBACK_ENABLED:-1}"; then
    python manage.py run_hardcover_sync_worker &
    hardcover_sync_worker_pid=$!
fi

gunicorn reading_challenge.wsgi:application \
    --pid /tmp/northbound-gunicorn.pid \
    --bind "0.0.0.0:${PORT:-8000}" \
    --workers "${WEB_CONCURRENCY:-2}" \
    --access-logfile - \
    --access-logformat '%(h)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s' \
    --error-logfile - &
gunicorn_pid=$!
wait "$gunicorn_pid"
