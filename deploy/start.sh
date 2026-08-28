#!/bin/sh
set -eu

python manage.py restore_pending_backup
python manage.py migrate --noinput

python manage.py run_backup_scheduler &
backup_scheduler_pid=$!
python manage.py run_challenge_scheduler &
challenge_scheduler_pid=$!
gunicorn_pid=""

shutdown_processes() {
    if [ -n "$gunicorn_pid" ]; then
        kill "$gunicorn_pid" 2>/dev/null || true
    fi
    kill "$backup_scheduler_pid" 2>/dev/null || true
    kill "$challenge_scheduler_pid" 2>/dev/null || true
    if [ -n "$gunicorn_pid" ]; then
        wait "$gunicorn_pid" 2>/dev/null || true
    fi
    wait "$backup_scheduler_pid" 2>/dev/null || true
    wait "$challenge_scheduler_pid" 2>/dev/null || true
}
trap shutdown_processes EXIT INT TERM

gunicorn reading_challenge.wsgi:application \
    --pid /tmp/northbound-gunicorn.pid \
    --bind "0.0.0.0:${PORT:-8000}" \
    --workers "${WEB_CONCURRENCY:-2}" \
    --access-logfile - \
    --error-logfile - &
gunicorn_pid=$!
wait "$gunicorn_pid"
