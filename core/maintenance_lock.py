import fcntl
import tempfile
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from django.conf import settings
from django.db import connection


class MaintenanceBusyError(OSError):
    pass


def maintenance_root():
    if connection.vendor == "sqlite":
        database_name = str(connection.settings_dict["NAME"])
        if database_name == ":memory:" or database_name.startswith("file:"):
            return Path(tempfile.gettempdir()) / "northbound-maintenance"
        return Path(database_name).resolve().parent
    return Path(settings.MEDIA_ROOT).resolve().parent


@contextmanager
def maintenance_lock():
    root = maintenance_root()
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".northbound-maintenance.lock"
    with lock_path.open("a+b") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MaintenanceBusyError(
                "Another Northbound backup, restore, or maintenance operation is already running."
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def with_maintenance_lock(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with maintenance_lock():
            return function(*args, **kwargs)

    return wrapped
