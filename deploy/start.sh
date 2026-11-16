#!/bin/sh
set -eu

python manage.py restore_pending_backup
python manage.py migrate --noinput
python manage.py safeguard_restored_hardcover_sync

exec python deploy/supervise.py
