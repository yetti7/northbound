import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0055_reader_hardcover_oauth_credentials"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ReaderHardcoverSyncPreference",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sync_completed_books", models.BooleanField(default=False)),
                ("sync_completion_dates", models.BooleanField(default=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="reader_hardcover_sync_preference", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name="HardcoverSyncOutbox",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_key", models.CharField(max_length=255, unique=True)),
                ("source_type", models.CharField(max_length=80)),
                ("source_id", models.CharField(max_length=120)),
                ("action", models.CharField(choices=[("completed_book", "Sync completed book"), ("completion_date", "Sync completion date")], max_length=32)),
                ("effective_date", models.DateField(blank=True, null=True)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("processing", "Processing"), ("succeeded", "Succeeded"), ("retryable", "Retryable failure"), ("blocked", "Reader action required"), ("skipped", "Skipped"), ("failed_permanent", "Permanent failure")], default="pending", max_length=24)),
                ("attempt_count", models.PositiveIntegerField(default=0)),
                ("last_attempt_at", models.DateTimeField(blank=True, null=True)),
                ("next_attempt_at", models.DateTimeField(blank=True, null=True)),
                ("error_classification", models.CharField(blank=True, max_length=80)),
                ("error_message", models.CharField(blank=True, max_length=300)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="hardcover_sync_outbox", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["created_at", "pk"]},
        ),
        migrations.CreateModel(
            name="HardcoverSyncProvenance",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("attempt_number", models.PositiveIntegerField()),
                ("source_type", models.CharField(max_length=80)),
                ("source_id", models.CharField(max_length=120)),
                ("action", models.CharField(choices=[("completed_book", "Sync completed book"), ("completion_date", "Sync completion date")], max_length=32)),
                ("effective_date", models.DateField(blank=True, null=True)),
                ("outcome", models.CharField(choices=[("succeeded", "Succeeded"), ("retryable_failure", "Retryable failure"), ("blocked", "Reader action required"), ("skipped", "Skipped"), ("failed_permanent", "Permanent failure")], max_length=24)),
                ("provider_identifier", models.CharField(blank=True, max_length=255)),
                ("error_classification", models.CharField(blank=True, max_length=80)),
                ("error_message", models.CharField(blank=True, max_length=300)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("outbox", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="provenance", to="core.hardcoversyncoutbox")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="hardcover_sync_provenance", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["created_at", "pk"]},
        ),
        migrations.AddConstraint(
            model_name="readerhardcoversyncpreference",
            constraint=models.CheckConstraint(condition=models.Q(("sync_completed_books", True), ("sync_completion_dates", False), _connector="OR"), name="hardcover_date_sync_requires_book_sync"),
        ),
        migrations.AddConstraint(
            model_name="hardcoversyncoutbox",
            constraint=models.CheckConstraint(condition=models.Q(("attempt_count__gte", 0)), name="hardcover_sync_attempt_count_nonnegative"),
        ),
        migrations.AddConstraint(
            model_name="hardcoversyncprovenance",
            constraint=models.UniqueConstraint(fields=("outbox", "attempt_number"), name="unique_hardcover_sync_attempt"),
        ),
        migrations.AddConstraint(
            model_name="hardcoversyncprovenance",
            constraint=models.CheckConstraint(condition=models.Q(("attempt_number__gt", 0)), name="hardcover_sync_attempt_number_positive"),
        ),
    ]
