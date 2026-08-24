import os
from pathlib import Path

from django.conf import settings
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from .backups import list_stored_backups, pending_restore_path
from .maintenance_lock import maintenance_lock
from .models import AuditEvent


AUDIT_RETENTION_YEARS = (1, 2, 3)
DISPOSABLE_CACHE_DIRECTORY = ".northbound-cache"


def disposable_cache_root():
    return Path(settings.MEDIA_ROOT) / DISPOSABLE_CACHE_DIRECTORY


def _safe_files(root):
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        return []
    files = []
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        directory_names[:] = [
            name for name in directory_names if not (directory_path / name).is_symlink()
        ]
        for filename in file_names:
            path = directory_path / filename
            if path.is_file() and not path.is_symlink():
                files.append(path)
    return files


def file_usage(root):
    count = 0
    size = 0
    for path in _safe_files(root):
        try:
            size += path.stat().st_size
        except OSError:
            continue
        count += 1
    return {"count": count, "size": size}


def disposable_cache_usage():
    return file_usage(disposable_cache_root())


def _restore_is_pending():
    return connection.vendor == "sqlite" and pending_restore_path().exists()


def storage_overview():
    is_sqlite = connection.vendor == "sqlite"
    database_path = Path(connection.settings_dict["NAME"]) if is_sqlite else None
    database_size = database_path.stat().st_size if database_path and database_path.is_file() else None
    media_usage = file_usage(settings.MEDIA_ROOT)
    cache_usage = disposable_cache_usage()
    stored_backups = list_stored_backups() if is_sqlite else []
    backup_size = 0
    for path in stored_backups:
        try:
            backup_size += path.stat().st_size
        except OSError:
            continue
    audit_events = AuditEvent.objects.order_by("created_at")
    return {
        "is_sqlite": is_sqlite,
        "database_size": database_size,
        "media_usage": media_usage,
        "cache_usage": cache_usage,
        "stored_backup_count": len(stored_backups),
        "stored_backup_size": backup_size,
        "audit_event_count": audit_events.count(),
        "oldest_audit_event": audit_events.first(),
        "restore_pending": _restore_is_pending(),
    }


def cleanup_disposable_cache(*, actor):
    with maintenance_lock():
        if _restore_is_pending():
            raise RuntimeError("A restore is staged. Complete or cancel it before running maintenance.")
        removed_count = 0
        removed_size = 0
        failed_count = 0
        root = disposable_cache_root()
        for path in _safe_files(root):
            try:
                size = path.stat().st_size
                path.unlink()
            except OSError:
                failed_count += 1
            else:
                removed_count += 1
                removed_size += size
        if root.is_dir() and not root.is_symlink():
            directories = sorted(
                (path for path in root.rglob("*") if path.is_dir() and not path.is_symlink()),
                key=lambda path: len(path.parts),
                reverse=True,
            )
            for directory in directories:
                try:
                    directory.rmdir()
                except OSError:
                    pass
        try:
            AuditEvent.objects.create(
                actor=actor,
                action="platform.disposable_cache_cleaned",
                object_type="DisposableCache",
                summary=(
                    f"Removed {removed_count} disposable cache file(s), reclaiming {removed_size} bytes; "
                    f"{failed_count} file(s) could not be removed."
                ),
            )
        except DatabaseError:
            raise RuntimeError(
                "Cache cleanup completed, but Northbound could not record its audit event. Review database availability."
            )
        return {"count": removed_count, "size": removed_size, "failed_count": failed_count}


def audit_cutoff(years, now=None):
    if years not in AUDIT_RETENTION_YEARS:
        raise ValueError("Choose a supported audit retention period.")
    current = now or timezone.now()
    try:
        return current.replace(year=current.year - years)
    except ValueError:
        return current.replace(year=current.year - years, day=28)


def audit_prune_preview(years, now=None):
    cutoff = audit_cutoff(years, now=now)
    return {
        "years": years,
        "cutoff": cutoff,
        "affected_count": AuditEvent.objects.filter(created_at__lt=cutoff).count(),
    }


def prune_audit_history(*, years, actor):
    cutoff = audit_cutoff(years)
    with maintenance_lock():
        if _restore_is_pending():
            raise RuntimeError("A restore is staged. Complete or cancel it before running maintenance.")
        try:
            with transaction.atomic():
                removed_count, _ = AuditEvent.objects.filter(created_at__lt=cutoff).delete()
                AuditEvent.objects.create(
                    actor=actor,
                    action="platform.audit_history_pruned",
                    object_type="AuditEvent",
                    summary=(
                        f"Pruned {removed_count} audit event(s) older than {years} year(s); "
                        f"cutoff {cutoff.isoformat()}. Deleted event content was not retained."
                    ),
                )
        except DatabaseError:
            raise RuntimeError(
                "Audit pruning could not complete. No audit history was intentionally removed; review database availability."
            )
    return {"count": removed_count, "cutoff": cutoff}


def optimize_sqlite_database(*, actor):
    if connection.vendor != "sqlite":
        raise ValueError("Northbound database optimization is available only for SQLite.")
    if _restore_is_pending():
        raise RuntimeError("A restore is staged. Complete or cancel it before running maintenance.")
    database_path = Path(connection.settings_dict["NAME"])
    before_size = database_path.stat().st_size if database_path.is_file() else 0
    with maintenance_lock():
        if _restore_is_pending():
            raise RuntimeError("A restore is staged. Complete or cancel it before running maintenance.")
        try:
            with connection.cursor() as cursor:
                cursor.execute("VACUUM")
            after_size = database_path.stat().st_size if database_path.is_file() else 0
            reclaimed = max(0, before_size - after_size)
            AuditEvent.objects.create(
                actor=actor,
                action="platform.sqlite_optimized",
                object_type="Database",
                summary=(
                    f"Optimized the SQLite database; size before {before_size} bytes, "
                    f"size after {after_size} bytes, reclaimed {reclaimed} bytes."
                ),
            )
        except DatabaseError:
            raise RuntimeError(
                "Database optimization could not complete. Northbound remains operational; try again when no other database work is active."
            )
    return {"before_size": before_size, "after_size": after_size, "reclaimed": reclaimed}
