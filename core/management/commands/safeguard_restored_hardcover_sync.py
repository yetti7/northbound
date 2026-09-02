import json

from django.core.management.base import BaseCommand
from django.db import connection, transaction

from core.backups import data_root
from core.hardcover_sync import record_sync_attempt
from core.models import HardcoverSyncOutbox, HardcoverSyncProvenance, ReaderHardcoverConnection


class Command(BaseCommand):
    help = "Quarantine unfinished Hardcover writes after a restored database so external mutations are not replayed blindly."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Quarantine after an offline external database restore (including PostgreSQL), before starting Northbound.")

    def handle(self, *args, **options):
        if options.get("force"):
            self._quarantine()
            return
        if connection.vendor != "sqlite":
            return  # PostgreSQL restore is operator-managed, not the SQLite ZIP path.
        root = data_root()
        restore_record = root / "last-restore.json"
        safeguard_record = root / "last-hardcover-restore-safeguard.json"
        if not restore_record.exists():
            return
        restore_data = json.loads(restore_record.read_text())
        restored_at = restore_data.get("restored_at")
        if not restored_at:
            return
        if safeguard_record.exists():
            safeguarded = json.loads(safeguard_record.read_text())
            if safeguarded.get("restored_at") == restored_at and safeguarded.get("version") == 2:
                return
        count = self._quarantine()
        safeguard_record.write_text(json.dumps({"version": 2, "restored_at": restored_at, "quarantined_events": count}, indent=2))

    @transaction.atomic
    def _quarantine(self):
        # Backed-up refresh tokens may already be spent at the provider. Never
        # attempt them, even when the restored access token has not expired.
        ReaderHardcoverConnection.objects.filter(connection_method="oauth").update(
            is_valid=False, reconnect_required=True, encrypted_refresh_token="",
            last_error="Reconnect Hardcover after restoring a backup. Saved authorization may have been rotated since this backup.",
        )
        unfinished = list(HardcoverSyncOutbox.objects.filter(status__in=[
            HardcoverSyncOutbox.Status.PENDING,
            HardcoverSyncOutbox.Status.RETRYABLE,
            HardcoverSyncOutbox.Status.PROCESSING,
            HardcoverSyncOutbox.Status.BLOCKED,
        ]).exclude(error_classification="restore_reconciliation_required").order_by("created_at", "pk"))
        for outbox in unfinished:
            record_sync_attempt(
                outbox=outbox,
                outcome=HardcoverSyncProvenance.Outcome.BLOCKED,
                result_detail="restore_reconciliation_required",
                occurrence_result_detail=outbox.occurrence_result_detail,
                error_classification="restore_reconciliation_required",
                error_message="A restored database cannot prove whether this external synchronization operation already ran.",
            )
        if unfinished:
            self.stdout.write(self.style.WARNING(f"Quarantined {len(unfinished)} restored Hardcover synchronization event(s) for safe reconciliation."))
        return len(unfinished)
