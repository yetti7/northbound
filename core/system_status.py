import os
import shutil
from pathlib import Path

from django.conf import settings
from django.db import DatabaseError, connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.exceptions import CircularDependencyError, InconsistentMigrationHistory, MigrationSchemaMissing, NodeNotFoundError

from .backups import next_scheduled_backup, pending_restore_path
from .models import PlatformBackupSettings
from .platform_config import get_platform_settings


LOW_STORAGE_BYTES = 1024 ** 3
LOW_STORAGE_RATIO = 0.10


def _warning(title, message):
    return {"title": title, "message": message}


def _nearest_existing_directory(path):
    candidate = Path(path)
    if candidate.exists() and candidate.is_file():
        candidate = candidate.parent
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _storage_status(label, path, warnings):
    display_path = Path(path).resolve()
    probe = _nearest_existing_directory(display_path)
    try:
        usage = shutil.disk_usage(probe)
    except OSError:
        warnings.append(_warning(
            f"{label} storage unavailable",
            f"Northbound could not inspect storage for {display_path}. Check that the configured path is mounted and readable.",
        ))
        return {"label": label, "path": str(display_path), "available": False}

    writable_target = display_path if display_path.exists() else probe
    writable = os.access(writable_target, os.W_OK)
    free_ratio = usage.free / usage.total if usage.total else 0
    if not writable:
        warnings.append(_warning(
            f"{label} storage is not writable",
            f"Northbound cannot write to {display_path}. Check the mount and filesystem permissions.",
        ))
    if usage.free < LOW_STORAGE_BYTES or free_ratio < LOW_STORAGE_RATIO:
        warnings.append(_warning(
            f"{label} storage is running low",
            f"Less than 10% or 1 GB remains for {display_path}. Free space before backups or uploads fail.",
        ))
    return {
        "label": label,
        "path": str(display_path),
        "available": True,
        "writable": writable,
        "free": usage.free,
        "total": usage.total,
        "used_percent": round((usage.used / usage.total) * 100) if usage.total else 0,
    }


def _migration_status(warnings):
    try:
        executor = MigrationExecutor(connection)
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
    except (DatabaseError, CircularDependencyError, InconsistentMigrationHistory, MigrationSchemaMissing, NodeNotFoundError):
        warnings.append(_warning(
            "Migration state could not be checked",
            "Northbound could not read the database migration state. Review the application logs and database availability.",
        ))
        return {"label": "Unavailable", "pending_count": None}
    if pending:
        warnings.append(_warning(
            "Database migrations are pending",
            f"{len(pending)} migration(s) have not been applied. Complete the documented Northbound upgrade process.",
        ))
        return {"label": f"{len(pending)} pending", "pending_count": len(pending)}
    return {"label": "Up to date", "pending_count": 0}


def _backup_status(warnings):
    if connection.vendor != "sqlite":
        return {
            "scheduler": "Not applicable for PostgreSQL",
            "schedule": "Use PostgreSQL-native backup tooling",
            "next_run": None,
            "last_result": "Managed outside Northbound",
            "last_result_at": None,
        }
    try:
        backup_settings = PlatformBackupSettings.objects.filter(pk=1).first() or PlatformBackupSettings()
    except DatabaseError:
        warnings.append(_warning(
            "Backup scheduler state unavailable",
            "Northbound could not read backup scheduler settings. Complete pending migrations and review the application logs.",
        ))
        return {
            "scheduler": "Unavailable",
            "schedule": "Unavailable",
            "next_run": None,
            "last_result": "Unavailable",
            "last_result_at": None,
        }

    weekday_names = dict(PlatformBackupSettings.Weekday.choices)
    selected_days = [weekday_names[day] for day in backup_settings.weekdays if day in weekday_names]
    if backup_settings.enabled:
        scheduler = "Enabled"
        schedule_time = backup_settings.backup_time.strftime("%I:%M %p").lstrip("0")
        schedule = f"{', '.join(selected_days)} at {schedule_time}"
    else:
        scheduler = "Disabled"
        schedule = "No automatic backups scheduled"
        warnings.append(_warning(
            "Automatic backups are disabled",
            "Create backups manually or enable an automatic schedule from Platform Administration → Backups.",
        ))

    failure_is_latest = bool(
        backup_settings.last_failure_at
        and (
            not backup_settings.last_success_at
            or backup_settings.last_failure_at > backup_settings.last_success_at
        )
    )
    if failure_is_latest:
        last_result = "Failed — review Platform Administration → Backups"
        last_result_at = backup_settings.last_failure_at
        warnings.append(_warning(
            "The latest automatic backup failed",
            "Review the failure details in Platform Administration → Backups and confirm the backup location is writable with sufficient free space.",
        ))
    elif backup_settings.last_success_at:
        last_result = "Successful"
        last_result_at = backup_settings.last_success_at
    else:
        last_result = "No automatic backup has completed"
        last_result_at = None
        if backup_settings.enabled:
            warnings.append(_warning(
                "No successful automatic backup is recorded",
                "Confirm the configured schedule and create a manual backup while waiting for the first scheduled run.",
            ))

    if pending_restore_path().exists():
        warnings.append(_warning(
            "A restore is staged",
            "A validated restore is waiting to be applied or canceled. Review Platform Administration → Backups before restarting Northbound.",
        ))
    return {
        "scheduler": scheduler,
        "schedule": schedule,
        "next_run": next_scheduled_backup(backup_settings),
        "last_result": last_result,
        "last_result_at": last_result_at,
    }


def build_system_status():
    warnings = []
    platform_settings = get_platform_settings()
    version = settings.NORTHBOUND_VERSION
    if version.lower() in {"development", "unknown", "unversioned"}:
        warnings.append(_warning(
            "Development build",
            "This build does not contain a release version identifier. Published Northbound images include one automatically.",
        ))

    public_url = settings.NORTHBOUND_URL or "Not configured"
    proxy_enabled = settings.NORTHBOUND_TRUST_PROXY_HEADERS
    if not settings.NORTHBOUND_URL:
        warnings.append(_warning(
            "Public URL is not configured",
            "Set NORTHBOUND_URL in the deployment environment to the address people use to reach Northbound.",
        ))
    elif settings.NORTHBOUND_URL.startswith("https://") and not proxy_enabled:
        warnings.append(_warning(
            "HTTPS proxy headers are not trusted",
            "Northbound uses an HTTPS public URL but trusted proxy headers are disabled. Review the reverse-proxy documentation.",
        ))
    if settings.DEBUG:
        warnings.append(_warning(
            "Debug mode is enabled",
            "Set DJANGO_DEBUG=0 before treating this installation as production.",
        ))

    database_config = connection.settings_dict
    if connection.vendor == "sqlite":
        database_backend = "SQLite"
        database_location = str(Path(database_config["NAME"]).resolve())
        database_storage_path = Path(database_location).parent
    elif connection.vendor == "postgresql":
        database_backend = "PostgreSQL"
        host = database_config.get("HOST") or "managed database service"
        port = database_config.get("PORT")
        name = database_config.get("NAME") or "configured database"
        database_location = f"{host}{f':{port}' if port else ''} / {name}"
        database_storage_path = None
    else:
        database_backend = connection.vendor.replace("_", " ").title()
        database_location = "Externally managed database"
        database_storage_path = None

    media_location = str(Path(settings.MEDIA_ROOT).resolve())
    storage = []
    if database_storage_path is not None:
        storage.append(_storage_status("Database", database_storage_path, warnings))
    storage.append(_storage_status("Media", settings.MEDIA_ROOT, warnings))

    return {
        "version": version,
        "database_backend": database_backend,
        "database_location": database_location,
        "timezone": platform_settings.timezone,
        "public_url": public_url,
        "proxy_state": "Trusted proxy headers enabled" if proxy_enabled else "Trusted proxy headers disabled",
        "media_location": media_location,
        "storage": storage,
        "migrations": _migration_status(warnings),
        "backups": _backup_status(warnings),
        "warnings": warnings,
    }
