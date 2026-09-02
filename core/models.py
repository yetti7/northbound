from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import F, Q
from django.urls import reverse
from django.templatetags.static import static
from django.utils import timezone
from datetime import timedelta
from datetime import time as datetime_time
from django.core.validators import MaxValueValidator, MinValueValidator
from zoneinfo import ZoneInfo
import hashlib
import re
import secrets
import unicodedata
import uuid

from .catalog_links import catalog_hardcover_url, safe_hardcover_url


def profile_picture_path(instance, filename):
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    return f"profile-pictures/user-{instance.user_id}.{extension}"


class UserProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="northbound_profile")
    profile_picture = models.ImageField(upload_to=profile_picture_path, blank=True)
    selected_avatar = models.CharField(max_length=100, blank=True)
    discord_username = models.CharField(max_length=100, blank=True)
    discord_username_is_public = models.BooleanField(default=False)
    must_change_password = models.BooleanField(default=False)

    @property
    def avatar_url(self):
        if self.profile_picture:
            return self.profile_picture.url
        if self.selected_avatar:
            return static(f"avatars/{self.selected_avatar}")
        return ""

    def __str__(self):
        return f"Profile for {self.user.username}"


def hash_platform_owner_invitation_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class PlatformOwnerInvitation(models.Model):
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="platform_owner_invitations_created")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    redeemed_at = models.DateTimeField(null=True, blank=True)
    redeemed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="platform_owner_invitation_redeemed")
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="platform_owner_invitations_revoked")

    class Meta:
        ordering = ["-created_at"]

    @classmethod
    def issue(cls, created_by):
        token = secrets.token_urlsafe(32)
        invitation = cls.objects.create(
            token_hash=hash_platform_owner_invitation_token(token),
            created_by=created_by,
            expires_at=timezone.now() + timedelta(days=7),
        )
        return invitation, token

    @property
    def is_valid(self):
        return not self.redeemed_at and not self.revoked_at and self.expires_at > timezone.now()

    @property
    def status(self):
        if self.redeemed_at:
            return "Redeemed"
        if self.revoked_at:
            return "Revoked"
        if self.expires_at <= timezone.now():
            return "Expired"
        return "Active"


def default_backup_weekdays():
    return [0]


def default_platform_timezone():
    return settings.TIME_ZONE


class PlatformSettings(models.Model):
    display_name = models.CharField(max_length=120, default="My Northbound")
    timezone = models.CharField(max_length=64, default=default_platform_timezone)
    allow_public_registration = models.BooleanField(default=True)
    allow_user_group_creation = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def load(cls):
        settings_object, _ = cls.objects.get_or_create(pk=1)
        return settings_object


def default_hardcover_oauth_scopes():
    return ["read:catalog", "read:library", "write:library"]


class HardcoverOAuthStateUse(models.Model):
    """Durable, secret-free replay fence across concurrent session snapshots."""
    state_hash = models.CharField(max_length=64, primary_key=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_or_cancelled = models.BooleanField(default=False)


class HardcoverOAuthApplication(models.Model):
    singleton_key = models.BooleanField(default=True, unique=True, editable=False)
    enabled = models.BooleanField(default=False)
    client_id = models.CharField(max_length=255, blank=True)
    encrypted_client_secret = models.TextField(blank=True)
    configured_scopes = models.JSONField(default=default_hardcover_oauth_scopes, editable=False)
    configured_website_url = models.URLField(max_length=500, blank=True)
    configured_redirect_uri = models.URLField(max_length=600, blank=True)
    configured_at = models.DateTimeField(null=True, blank=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)
    last_tested_at = models.DateTimeField(null=True, blank=True, editable=False)
    is_valid = models.BooleanField(default=False, editable=False)
    last_error = models.CharField(max_length=300, blank=True, editable=False)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(singleton_key=True),
                name="hardcover_oauth_application_singleton_true",
            ),
        ]

    def __str__(self):
        return "Hardcover OAuth application configuration"


class PlatformBackupSettings(models.Model):
    class Weekday(models.IntegerChoices):
        MONDAY = 0, "Monday"
        TUESDAY = 1, "Tuesday"
        WEDNESDAY = 2, "Wednesday"
        THURSDAY = 3, "Thursday"
        FRIDAY = 4, "Friday"
        SATURDAY = 5, "Saturday"
        SUNDAY = 6, "Sunday"

    enabled = models.BooleanField(default=True)
    weekdays = models.JSONField(default=default_backup_weekdays)
    backup_time = models.TimeField(default=datetime_time(1, 0))
    retention_count = models.PositiveSmallIntegerField(default=5, validators=[MinValueValidator(1), MaxValueValidator(100)])
    last_run_date = models.DateField(null=True, blank=True, editable=False)
    last_success_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_failure_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_error = models.TextField(blank=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def load(cls):
        settings_object, _ = cls.objects.get_or_create(pk=1)
        return settings_object


ACCESS_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_group_access_code():
    while True:
        code = "".join(secrets.choice(ACCESS_CODE_ALPHABET) for _ in range(6))
        if not ReadingGroup.objects.filter(join_code=code).exists():
            return code


class ReadingGroup(models.Model):
    class AccessCodeVisibility(models.TextChoices):
        OWNER = "owner", "Group owners only"
        STAFF = "staff", "Owners and moderators"
        MEMBERS = "members", "All group members"

    name = models.CharField(max_length=120)
    slug = models.SlugField(unique=True)
    timezone = models.CharField(max_length=64, default="America/New_York")
    announcement_enabled = models.BooleanField(default=False)
    announcement = models.TextField(blank=True, help_text="Optional message displayed on the group page.")
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)
    join_code_hash = models.CharField(max_length=256, blank=True)
    join_code_hint = models.CharField(max_length=4, blank=True)
    join_code = models.CharField(max_length=6, null=True, blank=True, unique=True)
    access_code_visibility = models.CharField(max_length=10, choices=AccessCodeVisibility.choices, default=AccessCodeVisibility.OWNER)

    def __str__(self):
        return self.name

    def regenerate_access_code(self):
        code = generate_group_access_code()
        self.join_code = code
        self.join_code_hash = make_password(code)
        self.join_code_hint = code[-4:]
        return code

    def save(self, *args, **kwargs):
        if not self.join_code:
            self.regenerate_access_code()
        super().save(*args, **kwargs)


class HardcoverConnection(models.Model):
    group = models.OneToOneField(ReadingGroup, on_delete=models.CASCADE, related_name="hardcover_connection")
    encrypted_token = models.TextField()
    token_hint = models.CharField(max_length=4, blank=True)
    connected_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    tested_at = models.DateTimeField(null=True, blank=True)
    is_valid = models.BooleanField(default=False)
    last_error = models.CharField(max_length=300, blank=True)

    def __str__(self):
        return f"Hardcover catalog connection for {self.group.name}"


class ReaderHardcoverConnection(models.Model):
    class ConnectionMethod(models.TextChoices):
        API_KEY = "api_key", "Scoped API key"
        OAUTH = "oauth", "OAuth"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reader_hardcover_connection")
    connection_method = models.CharField(max_length=20, choices=ConnectionMethod.choices, default=ConnectionMethod.API_KEY)
    encrypted_token = models.TextField()
    encrypted_refresh_token = models.TextField(blank=True)
    token_hint = models.CharField(max_length=4, blank=True)
    access_expires_at = models.DateTimeField(null=True, blank=True)
    granted_scopes = models.JSONField(default=list, blank=True)
    refreshed_at = models.DateTimeField(null=True, blank=True)
    reconnect_required = models.BooleanField(default=False)
    connected_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    tested_at = models.DateTimeField(null=True, blank=True)
    is_valid = models.BooleanField(default=False)
    last_error = models.CharField(max_length=300, blank=True)

    def __str__(self):
        return f"Personal Hardcover connection for {self.user.username}"


class ReaderHardcoverSyncPreference(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="reader_hardcover_sync_preference",
    )
    sync_completed_books = models.BooleanField(default=False)
    sync_completion_dates = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(sync_completed_books=True) | Q(sync_completion_dates=False),
                name="hardcover_date_sync_requires_book_sync",
            ),
        ]

    def clean(self):
        if self.sync_completion_dates and not self.sync_completed_books:
            raise ValidationError("Completion-date synchronization requires completed-book synchronization.")

    def save(self, *args, **kwargs):
        if not self.sync_completed_books:
            self.sync_completion_dates = False
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"Hardcover synchronization preferences for {self.user.username}"


class HardcoverSyncOutbox(models.Model):
    class Action(models.TextChoices):
        COMPLETED_BOOK = "completed_book", "Sync completed book"
        COMPLETION_DATE = "completion_date", "Sync completion date"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        SUCCEEDED = "succeeded", "Succeeded"
        RETRYABLE = "retryable", "Retryable failure"
        BLOCKED = "blocked", "Reader action required"
        SKIPPED = "skipped", "Skipped"
        FAILED_PERMANENT = "failed_permanent", "Permanent failure"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="hardcover_sync_outbox",
    )
    event_key = models.CharField(max_length=255, unique=True)
    source_type = models.CharField(max_length=80)
    source_id = models.CharField(max_length=120)
    action = models.CharField(max_length=32, choices=Action.choices)
    effective_date = models.DateField(null=True, blank=True)
    provider_book_id = models.CharField(max_length=100, blank=True)
    provider_user_book_id = models.CharField(max_length=100, blank=True)
    provider_read_id = models.CharField(max_length=100, blank=True)
    library_result_detail = models.CharField(max_length=40, blank=True)
    occurrence_result_detail = models.CharField(max_length=40, blank=True)
    result_detail = models.CharField(max_length=40, blank=True)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.PENDING)
    attempt_count = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    error_classification = models.CharField(max_length=80, blank=True)
    error_message = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=Q(attempt_count__gte=0),
                name="hardcover_sync_attempt_count_nonnegative",
            ),
        ]

    def __str__(self):
        return f"{self.event_key} — {self.get_status_display()}"


class HardcoverSyncProvenance(models.Model):
    class Outcome(models.TextChoices):
        SUCCEEDED = "succeeded", "Succeeded"
        RETRYABLE_FAILURE = "retryable_failure", "Retryable failure"
        BLOCKED = "blocked", "Reader action required"
        SKIPPED = "skipped", "Skipped"
        FAILED_PERMANENT = "failed_permanent", "Permanent failure"

    outbox = models.ForeignKey(
        HardcoverSyncOutbox,
        on_delete=models.PROTECT,
        related_name="provenance",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="hardcover_sync_provenance",
    )
    attempt_number = models.PositiveIntegerField()
    source_type = models.CharField(max_length=80)
    source_id = models.CharField(max_length=120)
    action = models.CharField(max_length=32, choices=HardcoverSyncOutbox.Action.choices)
    effective_date = models.DateField(null=True, blank=True)
    outcome = models.CharField(max_length=24, choices=Outcome.choices)
    provider_identifier = models.CharField(max_length=255, blank=True)
    provider_book_id = models.CharField(max_length=100, blank=True)
    provider_read_id = models.CharField(max_length=100, blank=True)
    library_result_detail = models.CharField(max_length=40, blank=True)
    occurrence_result_detail = models.CharField(max_length=40, blank=True)
    result_detail = models.CharField(max_length=40, blank=True)
    error_classification = models.CharField(max_length=80, blank=True)
    error_message = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["outbox", "attempt_number"],
                name="unique_hardcover_sync_attempt",
            ),
            models.CheckConstraint(
                condition=Q(attempt_number__gt=0),
                name="hardcover_sync_attempt_number_positive",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Hardcover synchronization provenance is append-only.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Hardcover synchronization provenance cannot be deleted.")

    def __str__(self):
        return f"{self.outbox.event_key} attempt {self.attempt_number} — {self.get_outcome_display()}"


class Membership(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", "Group owner"
        MODERATOR = "moderator", "Moderator"
        MEMBER = "member", "Member"

    group = models.ForeignKey(ReadingGroup, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reading_memberships")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.MEMBER)
    display_name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)
    joined_at = models.DateTimeField(auto_now_add=True)
    permission_overrides = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["group", "user"], name="unique_group_user_membership")]
        ordering = ["display_name"]

    def __str__(self):
        return f"{self.display_name} — {self.group}"


class ChallengeMonth(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        UPCOMING = "upcoming", "Upcoming"
        ACTIVE = "active", "Active"
        FINALIZING = "finalizing", "Finalizing"
        COMPLETED = "completed", "Completed"
        ARCHIVED = "archived", "Archived"

    class CompetitionVisibility(models.TextChoices):
        HOSTS = "hosts", "Hosts only"
        HOSTS_FLOATERS = "hosts_floaters", "Hosts + Floaters"
        HOSTS_TEAM_LEADERS = "hosts_leaders", "Hosts + Team Leaders"
        TEAM_MEMBERS = "team_members", "Team Members"
        EVERYBODY = "everybody", "Everybody"

    class AnnouncementMode(models.TextChoices):
        INHERIT = "inherit", "Use Group Announcement"
        CUSTOM = "custom", "Custom Announcement"
        NONE = "none", "No Announcement"

    class RegistrationAnswerEditingPolicy(models.TextChoices):
        NONE = "none", "No editing after registration"
        TIMED = "timed", "Editing allowed for a set period"
        UNTIL_CLOSE = "until_close", "Editing allowed until registration closes"

    group = models.ForeignKey(ReadingGroup, on_delete=models.CASCADE, related_name="challenge_months")
    name = models.CharField(max_length=80)
    description = models.TextField(blank=True)
    starts_on = models.DateField(null=True, blank=True)
    ends_on = models.DateField(null=True, blank=True)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    registration_opens_at = models.DateTimeField(null=True, blank=True)
    registration_closes_at = models.DateTimeField(null=True, blank=True)
    final_announcement_at = models.DateTimeField(null=True, blank=True)
    auto_open_registration = models.BooleanField(default=True)
    auto_close_registration = models.BooleanField(default=True)
    auto_start_challenge = models.BooleanField(default=True)
    auto_end_challenge = models.BooleanField(default=True)
    auto_complete_challenge = models.BooleanField(default=False)
    registration_is_open = models.BooleanField(default=False)
    registration_answer_editing_policy = models.CharField(
        max_length=12,
        choices=RegistrationAnswerEditingPolicy.choices,
        default=RegistrationAnswerEditingPolicy.TIMED,
    )
    registration_answer_editing_hours = models.PositiveSmallIntegerField(
        default=24,
        validators=[MinValueValidator(1), MaxValueValidator(720)],
    )
    late_entry_deadline = models.DateField(null=True, blank=True, editable=False)
    status = models.CharField(max_length=17, choices=Status.choices, default=Status.DRAFT)
    team_standings_visibility = models.CharField(
        max_length=14,
        choices=CompetitionVisibility.choices,
        default=CompetitionVisibility.HOSTS,
    )
    reader_scores_visibility = models.CharField(
        max_length=14,
        choices=CompetitionVisibility.choices,
        default=CompetitionVisibility.HOSTS,
    )
    games_enabled = models.BooleanField(default=False)
    botm_enabled = models.BooleanField(default=False)
    botm_completion_bonus_pages = models.PositiveIntegerField(default=0)
    tbr_enabled = models.BooleanField(default=False)
    tbr_book_bonus_pages = models.PositiveIntegerField(default=0)
    tbr_completion_bonus_pages = models.PositiveIntegerField(default=0)
    announcement_mode = models.CharField(max_length=10, choices=AnnouncementMode.choices, default=AnnouncementMode.INHERIT)
    announcement = models.TextField(blank=True, help_text="Message displayed when Custom Announcement is selected.")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["group", "name"], name="unique_group_challenge_month"),
            models.CheckConstraint(condition=Q(tbr_book_bonus_pages__gte=0), name="tbr_book_bonus_nonnegative"),
            models.CheckConstraint(condition=Q(tbr_completion_bonus_pages__gte=0), name="tbr_completion_bonus_nonnegative"),
        ]
        ordering = ["-starts_on"]

    def clean(self):
        if self.starts_on and self.ends_on and self.ends_on < self.starts_on:
            raise ValidationError("The end date must be on or after the start date.")
        if (
            self.registration_opens_at
            and self.registration_closes_at
            and self.registration_closes_at < self.registration_opens_at
        ):
            raise ValidationError({"registration_closes_at": "Registration closing date/time cannot precede registration opening date/time."})
        if self.starts_at and self.ends_at and self.ends_at < self.starts_at:
            raise ValidationError({"ends_at": "Challenge ending date/time cannot precede Challenge starting date/time."})
        if self.announcement_mode == self.AnnouncementMode.CUSTOM and not self.announcement.strip():
            raise ValidationError({"announcement": "Enter an announcement or select a different announcement option."})
        if self.pk and self.enrollments.exists():
            persisted_policy = type(self).objects.filter(pk=self.pk).values(
                "registration_answer_editing_policy",
                "registration_answer_editing_hours",
            ).first()
            if persisted_policy and (
                persisted_policy["registration_answer_editing_policy"] != self.registration_answer_editing_policy
                or persisted_policy["registration_answer_editing_hours"] != self.registration_answer_editing_hours
            ):
                raise ValidationError({"registration_answer_editing_policy": "Reader answer editing is locked after the first registration."})

    @classmethod
    def lifecycle_order(cls):
        return (
            cls.Status.DRAFT,
            cls.Status.UPCOMING,
            cls.Status.ACTIVE,
            cls.Status.FINALIZING,
            cls.Status.COMPLETED,
            cls.Status.ARCHIVED,
        )

    def validate_lifecycle_transition(self, target_status, *, confirm_reversal=False, confirm_completed_recovery=False):
        if target_status not in self.Status.values:
            raise ValidationError({"status": "Select a valid Challenge lifecycle state."})
        if target_status == self.status:
            return
        if self.status == self.Status.ARCHIVED:
            raise ValidationError({"status": "Archived Challenges cannot move backward through the normal lifecycle."})
        order = self.lifecycle_order()
        current_index = order.index(self.status)
        target_index = order.index(target_status)
        if abs(target_index - current_index) != 1:
            raise ValidationError({"status": "Challenges may move only one adjacent lifecycle stage at a time."})
        if target_index < current_index and not confirm_reversal:
            raise ValidationError({"status": "Moving a Challenge backward requires explicit confirmation."})
        if (
            self.status == self.Status.COMPLETED
            and target_status == self.Status.FINALIZING
            and not confirm_completed_recovery
        ):
            raise ValidationError({"status": "Reopening a Completed Challenge requires explicit recovery confirmation."})

    def transition_to(self, target_status, *, confirm_reversal=False, confirm_completed_recovery=False):
        if not self.pk:
            raise ValidationError({"status": "Save the Challenge before changing its lifecycle state."})
        with transaction.atomic():
            current = type(self).objects.select_for_update().get(pk=self.pk)
            current.validate_lifecycle_transition(
                target_status,
                confirm_reversal=confirm_reversal,
                confirm_completed_recovery=confirm_completed_recovery,
            )
            if target_status == current.status:
                return self
            current.status = target_status
            current._allow_lifecycle_transition = True
            current.save(update_fields=["status"])
        self.status = target_status
        return self

    def apply_scheduled_actions(self, *, now=None):
        current_time = now or timezone.now()
        actions = []
        original_status = self.status
        lifecycle_target = None

        if (
            self.auto_open_registration
            and self.registration_opens_at
            and current_time >= self.registration_opens_at
            and original_status in {self.Status.DRAFT, self.Status.UPCOMING, self.Status.ACTIVE}
        ):
            if not self.registration_is_open:
                self.registration_is_open = True
                actions.append("registration_opened")
            if original_status == self.Status.DRAFT:
                lifecycle_target = self.Status.UPCOMING

        if (
            self.auto_close_registration
            and self.registration_closes_at
            and current_time >= self.registration_closes_at
            and self.registration_is_open
        ):
            self.registration_is_open = False
            actions.append("registration_closed")

        if (
            self.auto_start_challenge
            and self.starts_at
            and current_time >= self.starts_at
            and original_status == self.Status.UPCOMING
        ):
            lifecycle_target = self.Status.ACTIVE
        elif (
            self.auto_end_challenge
            and self.ends_at
            and current_time >= self.ends_at
            and original_status == self.Status.ACTIVE
        ):
            lifecycle_target = self.Status.FINALIZING
        elif (
            self.auto_complete_challenge
            and self.final_announcement_at
            and current_time >= self.final_announcement_at
            and original_status == self.Status.FINALIZING
        ):
            lifecycle_target = self.Status.COMPLETED

        registration_changed = "registration_opened" in actions or "registration_closed" in actions
        if registration_changed:
            self.save(update_fields=["registration_is_open"])
        if lifecycle_target:
            self.transition_to(lifecycle_target)
            actions.append(f"lifecycle_{lifecycle_target}")
        return actions

    def save(self, *args, **kwargs):
        if self.group_id:
            group_timezone = ZoneInfo(self.group.timezone)
            if self.starts_at:
                self.starts_on = timezone.localtime(self.starts_at, group_timezone).date()
            if self.ends_at:
                self.ends_on = timezone.localtime(self.ends_at, group_timezone).date()
        if self.pk and not getattr(self, "_allow_lifecycle_transition", False):
            persisted_status = type(self).objects.filter(pk=self.pk).values_list("status", flat=True).first()
            if persisted_status is not None and persisted_status != self.status:
                raise ValidationError({"status": "Use the authoritative Challenge lifecycle transition mechanism."})
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.group}: {self.name}"

    def get_absolute_url(self):
        return reverse("month-detail", kwargs={"group_slug": self.group.slug, "pk": self.pk})

    @property
    def signup_schema_is_locked(self):
        return self.enrollments.exists()


class ChallengeSignupQuestion(models.Model):
    class QuestionType(models.TextChoices):
        SHORT_TEXT = "short_text", "Short Text"
        NUMBER = "number", "Number"
        SINGLE_CHOICE = "single_choice", "Single Choice"
        MULTIPLE_CHOICE = "multiple_choice", "Multiple Choice"

    month = models.ForeignKey(ChallengeMonth, on_delete=models.CASCADE, related_name="signup_questions")
    wording = models.CharField(max_length=240)
    question_type = models.CharField(max_length=20, choices=QuestionType.choices)
    is_required = models.BooleanField(default=False)
    choices = models.JSONField(default=list, blank=True)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["month", "position"], name="unique_signup_question_position"),
        ]

    def clean(self):
        if self.month_id and not self.pk and self.month.signup_questions.count() >= 10:
            raise ValidationError("A Challenge may have at most ten signup questions.")
        if self.pk and self.month.enrollments.exists():
            persisted = type(self).objects.filter(pk=self.pk).values(
                "wording", "question_type", "is_required", "choices", "position"
            ).first()
            current = {
                "wording": self.wording,
                "question_type": self.question_type,
                "is_required": self.is_required,
                "choices": self.choices,
                "position": self.position,
            }
            if persisted and persisted != current:
                raise ValidationError("Signup questions are locked after the first registration.")
        normalized_choices = [str(choice).strip() for choice in self.choices if str(choice).strip()]
        if len(normalized_choices) > 20:
            raise ValidationError({"choices": "A signup question may have at most 20 choices."})
        if any(len(choice) > 120 for choice in normalized_choices):
            raise ValidationError({"choices": "Each signup-question choice must be 120 characters or fewer."})
        choice_type = self.question_type in {self.QuestionType.SINGLE_CHOICE, self.QuestionType.MULTIPLE_CHOICE}
        if choice_type and len(normalized_choices) < 2:
            raise ValidationError({"choices": "Single Choice and Multiple Choice questions require at least two choices."})
        if not choice_type and normalized_choices:
            raise ValidationError({"choices": "Choices are available only for Single Choice and Multiple Choice questions."})
        self.choices = normalized_choices

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.month.enrollments.exists():
            raise ValidationError("Signup questions are locked after the first registration.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return self.wording


class ChallengeStaffAssignment(models.Model):
    class Role(models.TextChoices):
        HOST = "host", "Host"
        TEAM_LEADER = "team_leader", "Team Leader"
        FLOATER = "floater", "Floater"

    month = models.ForeignKey(ChallengeMonth, on_delete=models.CASCADE, related_name="staff_assignments")
    membership = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="challenge_staff_assignments")
    team = models.ForeignKey("Team", null=True, blank=True, on_delete=models.PROTECT, related_name="staff_assignments")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.HOST)
    assigned_at = models.DateTimeField(auto_now_add=True)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_challenge_staff",
    )
    host_assignment_notice_seen_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    ended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ended_challenge_staff",
    )

    class Meta:
        ordering = ["membership__display_name", "assigned_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["month", "membership", "role"],
                condition=Q(ended_at__isnull=True),
                name="unique_active_challenge_staff_role",
            )
        ]

    def clean(self):
        if not self.membership_id or not self.month_id:
            return
        if self.membership.group_id != self.month.group_id:
            raise ValidationError("Challenge staff must belong to the same reading group.")
        if self.role in {self.Role.HOST, self.Role.FLOATER} and self.team_id:
            raise ValidationError(f"{self.get_role_display()} assignments cannot be tied to a team.")
        if self.role == self.Role.TEAM_LEADER:
            if not self.team_id:
                raise ValidationError("Team Leader assignments require a team.")
            if self.team.month_id != self.month_id:
                raise ValidationError("The Team Leader team must belong to the selected challenge month.")
        if self.ended_at is None and (not self.membership.is_active or self.membership.user.is_superuser):
            raise ValidationError("Challenge staff must have an active normal group membership.")
        active_other_roles = ChallengeStaffAssignment.objects.filter(
            month=self.month,
            membership=self.membership,
            ended_at__isnull=True,
        ).exclude(pk=self.pk)
        if self.ended_at is None and self.role == self.Role.HOST and active_other_roles.filter(role=self.Role.FLOATER).exists():
            raise ValidationError("End the active Floater assignment before assigning this member as a Host.")
        if self.ended_at is None and self.role == self.Role.FLOATER:
            if MonthEnrollment.objects.filter(month=self.month, participant=self.membership, is_active=True).exists():
                raise ValidationError("Enrolled Readers cannot be assigned as Floaters.")
            if active_other_roles.filter(role__in=(self.Role.HOST, self.Role.TEAM_LEADER)).exists():
                raise ValidationError("A current Host or Team Leader cannot be assigned as a Floater.")
        if self.ended_at is None and self.role == self.Role.TEAM_LEADER:
            if active_other_roles.filter(role=self.Role.FLOATER).exists():
                raise ValidationError("End the active Floater assignment before assigning this member as a Team Leader.")
            if not MonthEnrollment.objects.filter(month=self.month, participant=self.membership, is_active=True).exists():
                raise ValidationError("Team Leaders must already be enrolled in the challenge month.")
            if not TeamAssignment.objects.filter(
                month=self.month,
                participant=self.membership,
                team=self.team,
                ended_at__isnull=True,
            ).exists():
                raise ValidationError("Team Leaders can only lead their assigned team.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def is_active(self):
        return self.ended_at is None

    def __str__(self):
        return f"{self.month.name} — {self.get_role_display()}: {self.membership.display_name}"


class MonthTheme(models.Model):
    month = models.ForeignKey(ChallengeMonth, on_delete=models.CASCADE, related_name="themes")
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    starts_on = models.DateField()
    ends_on = models.DateField()
    bonus_pages = models.PositiveIntegerField(default=50)
    allow_stacking = models.BooleanField(default=True, help_text="Allow this theme to be claimed with another theme on the same book.")
    prompt = models.CharField(max_length=300, blank=True, help_text="Optional question readers must answer when claiming this theme.")
    is_active = models.BooleanField(default=True)
    is_visible = models.BooleanField(default=True)

    class Meta:
        ordering = ["starts_on", "name"]
        constraints = [models.UniqueConstraint(fields=["month", "name"], name="unique_theme_name_per_month")]

    def clean(self):
        if self.ends_on < self.starts_on:
            raise ValidationError("The theme end date must be on or after its start date.")
        if self.month_id:
            if not self.month.starts_on or not self.month.ends_on:
                raise ValidationError("Configure the Challenge schedule before adding themes.")
            if self.starts_on < self.month.starts_on or self.ends_on > self.month.ends_on:
                raise ValidationError("Theme dates must fall inside the challenge month.")

    def __str__(self):
        return f"{self.month.name} — {self.name}"


class Team(models.Model):
    month = models.ForeignKey(ChallengeMonth, on_delete=models.CASCADE, related_name="teams")
    name = models.CharField(max_length=80)
    color = models.CharField(max_length=7, default="#6d5dfc")
    is_archived = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["month", "name"], name="unique_month_team")]
        ordering = ["name"]

    def __str__(self):
        return f"{self.month.name} — {self.name}"

    @property
    def approved_pages(self):
        """Compatibility total for the current active team roster."""
        from .score_aggregation import challenge_score_totals

        participant_ids = self.assignments.filter(
            ended_at__isnull=True,
            participant__month_enrollments__month=self.month,
            participant__month_enrollments__is_active=True,
        ).values_list("participant_id", flat=True)
        scores = challenge_score_totals(month=self.month, participant_ids=participant_ids)
        return sum(score["total_pages"] for score in scores.values())



class MonthEnrollment(models.Model):
    class Origin(models.TextChoices):
        LEGACY = "legacy", "Legacy"
        SELF = "self", "Self-registration"
        STAFF = "staff", "Staff"

    class InactiveReason(models.TextChoices):
        WITHDRAWN = "withdrawn", "Withdrawn"
        REMOVED = "removed", "Removed"

    month = models.ForeignKey(ChallengeMonth, on_delete=models.CASCADE, related_name="enrollments")
    participant = models.ForeignKey(Membership, on_delete=models.CASCADE, related_name="month_enrollments")
    enrolled_at = models.DateTimeField(auto_now_add=True)
    enrolled_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="created_month_enrollments")
    is_active = models.BooleanField(default=True)
    origin = models.CharField(max_length=10, choices=Origin.choices, default=Origin.LEGACY)
    inactive_reason = models.CharField(max_length=12, choices=InactiveReason.choices, blank=True)
    inactivated_at = models.DateTimeField(null=True, blank=True)
    inactivated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="inactivated_month_enrollments",
    )

    class Meta:
        constraints = [models.UniqueConstraint(fields=["month", "participant"], name="one_enrollment_per_participant_per_month")]
        ordering = ["participant__display_name"]

    def clean(self):
        if self.participant_id and self.month_id and self.participant.group_id != self.month.group_id:
            raise ValidationError("The participant must belong to the same reading group.")
        if self.participant_id and self.month_id and ChallengeStaffAssignment.objects.filter(
            month=self.month,
            membership=self.participant,
            role=ChallengeStaffAssignment.Role.FLOATER,
            ended_at__isnull=True,
        ).exists():
            raise ValidationError("End this member's active Floater assignment before enrolling them as a Reader.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.month.name} — {self.participant.display_name}"

    @property
    def registration_answer_editing_deadline(self):
        if self.month.registration_answer_editing_policy != ChallengeMonth.RegistrationAnswerEditingPolicy.TIMED:
            return None
        return self.enrolled_at + timedelta(hours=self.month.registration_answer_editing_hours)

    def can_reader_edit_registration_answers(self, *, now=None):
        current_time = now or timezone.now()
        policy = self.month.registration_answer_editing_policy
        if policy == ChallengeMonth.RegistrationAnswerEditingPolicy.NONE:
            return False
        if policy == ChallengeMonth.RegistrationAnswerEditingPolicy.UNTIL_CLOSE:
            return self.month.registration_is_open
        return current_time <= self.registration_answer_editing_deadline


class PersonalTBR(models.Model):
    enrollment = models.OneToOneField(MonthEnrollment, on_delete=models.CASCADE, related_name="personal_tbr")
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Personal TBR"
        verbose_name_plural = "Personal TBRs"

    def clean(self):
        if self.pk:
            persisted = type(self).objects.filter(pk=self.pk).values("confirmed_at").first()
            if persisted and persisted["confirmed_at"] and self.confirmed_at != persisted["confirmed_at"]:
                raise ValidationError({"confirmed_at": "A confirmed Personal TBR cannot be unlocked or reconfirmed."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.confirmed_at or (self.pk and type(self).objects.filter(pk=self.pk, confirmed_at__isnull=False).exists()):
            raise ValidationError("A confirmed Personal TBR cannot be deleted.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.enrollment} — Personal TBR"


class PersonalTBRBook(models.Model):
    personal_tbr = models.ForeignKey(PersonalTBR, on_delete=models.CASCADE, related_name="books")
    position = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(9)])
    catalog_book = models.ForeignKey(
        "CatalogBook", null=True, blank=True, on_delete=models.PROTECT, related_name="personal_tbr_books"
    )
    catalog_edition = models.ForeignKey(
        "CatalogEdition", null=True, blank=True, on_delete=models.PROTECT, related_name="personal_tbr_books"
    )
    title_snapshot = models.CharField(max_length=300)
    author_snapshot = models.CharField(max_length=300)
    page_count_snapshot = models.PositiveIntegerField(null=True, blank=True)
    cover_url_snapshot = models.URLField(max_length=1000, blank=True)
    source_url_snapshot = models.URLField(max_length=1000, blank=True)
    normalized_title = models.CharField(max_length=300, editable=False)
    normalized_author = models.CharField(max_length=300, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def hardcover_url(self):
        return catalog_hardcover_url(
            catalog_book=self.catalog_book,
            catalog_edition=self.catalog_edition,
        )

    class Meta:
        ordering = ["position", "pk"]
        constraints = [
            models.CheckConstraint(condition=Q(position__gte=1, position__lte=9), name="personal_tbr_position_1_to_9"),
            models.CheckConstraint(
                condition=Q(page_count_snapshot__isnull=True) | Q(page_count_snapshot__gt=0),
                name="personal_tbr_page_count_positive",
            ),
            models.CheckConstraint(condition=~Q(normalized_title=""), name="personal_tbr_title_present"),
            models.CheckConstraint(condition=~Q(normalized_author=""), name="personal_tbr_author_present"),
            models.UniqueConstraint(fields=["personal_tbr", "position"], name="unique_personal_tbr_position"),
            models.UniqueConstraint(
                fields=["personal_tbr", "catalog_book"],
                condition=Q(catalog_book__isnull=False),
                name="unique_personal_tbr_catalog_book",
            ),
            models.UniqueConstraint(
                fields=["personal_tbr", "normalized_title", "normalized_author"],
                name="unique_personal_tbr_identity",
            ),
        ]

    def clean(self):
        self.normalized_title = normalize_book_identity(self.title_snapshot)
        self.normalized_author = normalize_book_identity(self.author_snapshot)
        errors = {}
        if self.personal_tbr_id and self.personal_tbr.confirmed_at:
            errors["personal_tbr"] = "A confirmed Personal TBR cannot be changed."
        if not self.normalized_title:
            errors["title_snapshot"] = "Enter a book title."
        if not self.normalized_author:
            errors["author_snapshot"] = "Enter a book author."
        if self.page_count_snapshot is not None and self.page_count_snapshot <= 0:
            errors["page_count_snapshot"] = "Page count must be positive when provided."
        if self.catalog_edition_id:
            if not self.catalog_book_id:
                errors["catalog_edition"] = "A catalog edition requires its catalog book."
            elif self.catalog_edition.book_id != self.catalog_book_id:
                errors["catalog_edition"] = "The catalog edition must belong to the selected catalog book."
        if self.personal_tbr_id:
            siblings = type(self).objects.filter(personal_tbr_id=self.personal_tbr_id).exclude(pk=self.pk)
            if self.catalog_book_id and siblings.filter(catalog_book_id=self.catalog_book_id).exists():
                errors["catalog_book"] = "This catalog book is already on the Personal TBR."
            if self.normalized_title and self.normalized_author and siblings.filter(
                normalized_title=self.normalized_title, normalized_author=self.normalized_author
            ).exists():
                errors["title_snapshot"] = "This title and author are already on the Personal TBR."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.personal_tbr.confirmed_at:
            raise ValidationError("A confirmed Personal TBR book cannot be deleted.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.personal_tbr} #{self.position} — {self.title_snapshot}"


class ChallengeSignupAnswer(models.Model):
    enrollment = models.ForeignKey(MonthEnrollment, on_delete=models.CASCADE, related_name="signup_answers")
    question = models.ForeignKey(ChallengeSignupQuestion, on_delete=models.PROTECT, related_name="answers")
    value = models.JSONField(default=str, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["question__position", "question_id"]
        constraints = [
            models.UniqueConstraint(fields=["enrollment", "question"], name="one_answer_per_signup_question"),
        ]

    def clean(self):
        if self.enrollment_id and self.question_id and self.enrollment.month_id != self.question.month_id:
            raise ValidationError("The signup answer must belong to the enrollment's Challenge.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class ProgressCheckpoint(models.Model):
    class ProgressBasis(models.TextChoices):
        BASE = "base", "Base Pages"
        TOTAL = "total", "Total Pages"

    class TargetBasis(models.TextChoices):
        PREVIOUS_AVERAGE = "previous_average", "Previous Monthly Average"
        FIXED = "fixed", "Fixed Target"

    class EvaluationState(models.TextChoices):
        PENDING = "pending", "Pending"
        EVALUATED = "evaluated", "Evaluated"
        SKIPPED = "skipped", "Skipped"

    month = models.ForeignKey(ChallengeMonth, on_delete=models.CASCADE, related_name="progress_checkpoints")
    scheduled_at = models.DateTimeField()
    threshold_percentage = models.PositiveSmallIntegerField(
        default=25,
        validators=[MinValueValidator(1), MaxValueValidator(100)],
    )
    progress_basis = models.CharField(max_length=8, choices=ProgressBasis.choices, default=ProgressBasis.BASE)
    target_basis = models.CharField(max_length=20, choices=TargetBasis.choices, default=TargetBasis.PREVIOUS_AVERAGE)
    fixed_target_pages = models.PositiveIntegerField(null=True, blank=True)
    position = models.PositiveSmallIntegerField(default=1)
    evaluation_state = models.CharField(max_length=10, choices=EvaluationState.choices, default=EvaluationState.PENDING)
    evaluated_at = models.DateTimeField(null=True, blank=True)
    recovery_hold = models.BooleanField(
        default=False,
        help_text="Platform recovery has paused automatic checkpoint processing until explicit release.",
    )

    class Meta:
        ordering = ["position", "scheduled_at", "pk"]
        constraints = [models.UniqueConstraint(fields=["month", "position"], name="unique_progress_checkpoint_position")]

    def clean(self):
        if self.month_id and not self.pk and self.month.progress_checkpoints.count() >= 5:
            raise ValidationError("A Challenge may have at most five progress checkpoints.")
        if self.target_basis == self.TargetBasis.FIXED and not self.fixed_target_pages:
            raise ValidationError({"fixed_target_pages": "Enter a positive page target for a Fixed Target checkpoint."})
        if self.target_basis == self.TargetBasis.PREVIOUS_AVERAGE:
            self.fixed_target_pages = None
        if self.pk:
            persisted = type(self).objects.filter(pk=self.pk).values("evaluation_state").first()
            if persisted and persisted["evaluation_state"] != self.EvaluationState.PENDING:
                editable = type(self).objects.filter(pk=self.pk).values(
                    "scheduled_at", "threshold_percentage", "progress_basis", "target_basis",
                    "fixed_target_pages", "position", "evaluation_state", "evaluated_at",
                ).first()
                current = {
                    "scheduled_at": self.scheduled_at,
                    "threshold_percentage": self.threshold_percentage,
                    "progress_basis": self.progress_basis,
                    "target_basis": self.target_basis,
                    "fixed_target_pages": self.fixed_target_pages,
                    "position": self.position,
                    "evaluation_state": self.evaluation_state,
                    "evaluated_at": self.evaluated_at,
                }
                if editable != current:
                    raise ValidationError("Evaluated checkpoint configuration is locked to preserve its historical results.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        persisted_state = type(self).objects.filter(pk=self.pk).values_list("evaluation_state", flat=True).first()
        if persisted_state != self.EvaluationState.PENDING:
            raise ValidationError("Evaluated checkpoints cannot be deleted.")
        return super().delete(*args, **kwargs)


class ProgressCheckpointResult(models.Model):
    class Outcome(models.TextChoices):
        MET = "met", "Met threshold"
        BELOW = "below", "Below threshold"
        NOT_EVALUATED = "not_evaluated", "Not Evaluated"

    checkpoint = models.ForeignKey(ProgressCheckpoint, on_delete=models.PROTECT, related_name="results")
    participant = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="progress_checkpoint_results")
    evaluated_at = models.DateTimeField()
    threshold_percentage = models.PositiveSmallIntegerField()
    progress_basis = models.CharField(max_length=8, choices=ProgressCheckpoint.ProgressBasis.choices)
    target_basis = models.CharField(max_length=20, choices=ProgressCheckpoint.TargetBasis.choices)
    target_pages = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    required_pages = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    progress_pages = models.PositiveIntegerField(default=0)
    outcome = models.CharField(max_length=16, choices=Outcome.choices)

    class Meta:
        ordering = ["checkpoint__scheduled_at", "participant__display_name", "pk"]
        constraints = [models.UniqueConstraint(fields=["checkpoint", "participant"], name="one_progress_result_per_checkpoint_reader")]

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Progress checkpoint results are immutable historical snapshots.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "Individual progress checkpoint results cannot be deleted; reset the whole evaluation through Platform recovery."
        )


class TeamAssignment(models.Model):
    month = models.ForeignKey(ChallengeMonth, on_delete=models.CASCADE, related_name="team_assignments")
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="assignments")
    participant = models.ForeignKey(Membership, on_delete=models.CASCADE, related_name="team_assignments")
    assigned_at = models.DateTimeField(auto_now_add=True, null=True)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_team_assignments",
    )
    ended_at = models.DateTimeField(null=True, blank=True)
    ended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ended_team_assignments",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["month", "participant"],
                condition=Q(ended_at__isnull=True),
                name="one_current_team_per_participant_per_month",
            )
        ]

    def clean(self):
        if self.team_id and self.month_id and self.team.month_id != self.month_id:
            raise ValidationError("The team must belong to the selected challenge month.")
        if self.participant_id and self.month_id and self.participant.group_id != self.month.group_id:
            raise ValidationError("The participant must belong to the same reading group.")
        if self.participant_id and self.month_id and ChallengeStaffAssignment.objects.filter(
            month=self.month,
            membership=self.participant,
            role=ChallengeStaffAssignment.Role.FLOATER,
            ended_at__isnull=True,
        ).exists():
            raise ValidationError("End this member's active Floater assignment before assigning them to a team.")
        if self.ended_at is None and self.participant_id and self.month_id and not MonthEnrollment.objects.filter(
            month=self.month,
            participant=self.participant,
            is_active=True,
        ).exists():
            raise ValidationError("Current team assignments require active Challenge participation.")

    def save(self, *args, **kwargs):
        previous = None
        if self.pk:
            previous = TeamAssignment.objects.filter(pk=self.pk).first()
        with transaction.atomic():
            if previous and (
                previous.month_id != self.month_id
                or previous.participant_id != self.participant_id
                or previous.team_id != self.team_id
            ):
                raise ValidationError("Historical team assignments cannot be rewritten. End the current assignment and create a new one.")
            self.full_clean()
            super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            end_active_team_leader_assignments(
                month=self.month,
                participant=self.participant,
                team=self.team,
                actor=getattr(self, "_staffing_change_actor", None),
                reason="the underlying team assignment was removed",
            )
            return super().delete(*args, **kwargs)

    @property
    def is_current(self):
        return self.ended_at is None


def end_active_team_leader_assignments(*, month, participant, team, actor=None, reason):
    active_assignments = ChallengeStaffAssignment.objects.filter(
        month=month,
        membership=participant,
        team=team,
        role=ChallengeStaffAssignment.Role.TEAM_LEADER,
        ended_at__isnull=True,
    )
    ended_at = timezone.now()
    for staffing in active_assignments:
        staffing.ended_at = ended_at
        staffing.ended_by = actor
        staffing.save(update_fields=["ended_at", "ended_by"])
        AuditEvent.objects.create(
            actor=actor,
            group=month.group,
            action="challenge.team_leader_ended",
            object_type="ChallengeStaffAssignment",
            object_id=str(staffing.pk),
            summary=(
                f"Ended {participant.display_name}'s Team Leader assignment for {team.name} "
                f"in {month.name} because {reason}."
            ),
        )


class CatalogBook(models.Model):
    provider = models.CharField(max_length=32)
    provider_book_id = models.CharField(max_length=100)
    title = models.CharField(max_length=300)
    author = models.CharField(max_length=300)
    subtitle = models.CharField(max_length=300, blank=True)
    cover_url = models.URLField(max_length=1000, blank=True)
    source_url = models.URLField(max_length=1000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    refreshed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["provider", "provider_book_id"], name="unique_catalog_book_per_provider"),
        ]
        ordering = ["title", "author"]

    def __str__(self):
        return f"{self.title} by {self.author}"

    @property
    def hardcover_url(self):
        return safe_hardcover_url(provider=self.provider, source_url=self.source_url)


class CatalogEdition(models.Model):
    book = models.ForeignKey(CatalogBook, on_delete=models.CASCADE, related_name="editions")
    provider = models.CharField(max_length=32)
    provider_edition_id = models.CharField(max_length=100)
    isbn_10 = models.CharField(max_length=10, blank=True)
    isbn_13 = models.CharField(max_length=13, blank=True)
    format_name = models.CharField(max_length=80, blank=True)
    page_count = models.PositiveIntegerField(null=True, blank=True)
    audio_seconds = models.PositiveIntegerField(null=True, blank=True)
    users_count = models.PositiveIntegerField(default=0)
    source_url = models.URLField(max_length=1000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    refreshed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["provider", "provider_edition_id"], name="unique_catalog_edition_per_provider"),
        ]
        ordering = ["book__title", "format_name", "provider_edition_id"]

    def __str__(self):
        label = self.format_name or "Edition"
        return f"{self.book.title} — {label}"

    @property
    def hardcover_url(self):
        return catalog_hardcover_url(catalog_book=self.book, catalog_edition=self)


def normalize_book_identity(value):
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    without_punctuation = "".join(
        " " if unicodedata.category(character).startswith("P") else character
        for character in normalized
    )
    return " ".join(without_punctuation.split())


class BotmBook(models.Model):
    month = models.ForeignKey(ChallengeMonth, on_delete=models.CASCADE, related_name="botm_books")
    position = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(9)])
    catalog_book = models.ForeignKey(
        CatalogBook,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="botm_books",
    )
    catalog_edition = models.ForeignKey(
        CatalogEdition,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="botm_books",
    )
    title_snapshot = models.CharField(max_length=300)
    author_snapshot = models.CharField(max_length=300)
    page_count_snapshot = models.PositiveIntegerField()
    cover_url_snapshot = models.URLField(max_length=1000, blank=True)
    source_url_snapshot = models.URLField(max_length=1000, blank=True)
    normalized_title = models.CharField(max_length=300, editable=False)
    normalized_author = models.CharField(max_length=300, editable=False)
    bonus_pages = models.PositiveIntegerField(default=0)
    is_retired = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def hardcover_url(self):
        return catalog_hardcover_url(
            catalog_book=self.catalog_book,
            catalog_edition=self.catalog_edition,
        )

    class Meta:
        ordering = ["position", "pk"]
        constraints = [
            models.CheckConstraint(condition=Q(position__gte=1, position__lte=9), name="botm_position_1_to_9"),
            models.CheckConstraint(condition=Q(page_count_snapshot__gt=0), name="botm_page_count_positive"),
            models.CheckConstraint(condition=Q(bonus_pages__gte=0), name="botm_bonus_nonnegative"),
            models.UniqueConstraint(
                fields=["month", "position"],
                condition=Q(is_retired=False),
                name="unique_active_botm_position",
            ),
            models.UniqueConstraint(
                fields=["month", "catalog_book"],
                condition=Q(is_retired=False, catalog_book__isnull=False),
                name="unique_active_botm_catalog_book",
            ),
            models.UniqueConstraint(
                fields=["month", "normalized_title", "normalized_author"],
                condition=Q(is_retired=False, catalog_book__isnull=True),
                name="unique_active_manual_botm_identity",
            ),
        ]

    def clean(self):
        self.normalized_title = normalize_book_identity(self.title_snapshot)
        self.normalized_author = normalize_book_identity(self.author_snapshot)
        errors = {}
        if self.pk and type(self).objects.filter(pk=self.pk, matches__isnull=False).exists():
            persisted = type(self).objects.filter(pk=self.pk).values(
                "catalog_book_id", "catalog_edition_id", "title_snapshot", "author_snapshot",
                "page_count_snapshot", "normalized_title", "normalized_author", "bonus_pages",
            ).first()
            protected = {
                "catalog_book_id": self.catalog_book_id,
                "catalog_edition_id": self.catalog_edition_id,
                "title_snapshot": self.title_snapshot,
                "author_snapshot": self.author_snapshot,
                "page_count_snapshot": self.page_count_snapshot,
                "normalized_title": self.normalized_title,
                "normalized_author": self.normalized_author,
                "bonus_pages": self.bonus_pages,
            }
            if persisted != protected:
                errors["title_snapshot"] = "BOTM identity, page count, and bonus are locked after match history exists."
        if not self.normalized_title:
            errors["title_snapshot"] = "Enter a Book of the Month title."
        if not self.normalized_author:
            errors["author_snapshot"] = "Enter a Book of the Month author."
        if self.page_count_snapshot is not None and self.page_count_snapshot <= 0:
            errors["page_count_snapshot"] = "Page count must be positive."
        if self.bonus_pages is not None and self.bonus_pages < 0:
            errors["bonus_pages"] = "Bonus pages cannot be negative."
        if self.catalog_edition_id:
            if not self.catalog_book_id:
                errors["catalog_edition"] = "A catalog edition requires its catalog book."
            elif self.catalog_edition.book_id != self.catalog_book_id:
                errors["catalog_edition"] = "The catalog edition must belong to the selected catalog book."
        if not self.is_retired and self.month_id:
            active_books = type(self).objects.filter(month_id=self.month_id, is_retired=False).exclude(pk=self.pk)
            if active_books.count() >= 9:
                errors["month"] = "A Challenge can have at most nine active Book of the Month books."
            if self.normalized_title and self.normalized_author:
                matching_identity = active_books.filter(
                    normalized_title=self.normalized_title,
                    normalized_author=self.normalized_author,
                )
                if self.catalog_book_id:
                    matching_identity = matching_identity.filter(catalog_book__isnull=True)
                if matching_identity.exists():
                    errors["title_snapshot"] = "This active Book of the Month work is already configured."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.matches.exists():
            raise ValidationError("BOTM books with match history must be retired rather than deleted.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.month.name} #{self.position} — {self.title_snapshot}"


class CatalogSearchCache(models.Model):
    query_hash = models.CharField(max_length=64, unique=True)
    query_text = models.CharField(max_length=300)
    results = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Cached catalog search: {self.query_text}"


class PersonalTBRMatch(models.Model):
    class Method(models.TextChoices):
        CATALOG_WORK = "catalog_work", "Catalog work"
        NORMALIZED_TITLE_AUTHOR = "normalized_title_author", "Normalized title and author"
        MANUAL_REVIEW = "manual_review", "Manual review"

    class Status(models.TextChoices):
        CONFIRMED = "confirmed", "Confirmed"
        PENDING_REVIEW = "pending_review", "Pending review"
        REJECTED = "rejected", "Rejected"

    personal_tbr_book = models.ForeignKey(PersonalTBRBook, on_delete=models.PROTECT, related_name="matches")
    month = models.ForeignKey(ChallengeMonth, on_delete=models.PROTECT, related_name="personal_tbr_matches")
    participant = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="personal_tbr_matches")
    submission = models.ForeignKey("BookSubmission", on_delete=models.PROTECT, related_name="personal_tbr_matches")
    method = models.CharField(max_length=32, choices=Method.choices)
    status = models.CharField(max_length=20, choices=Status.choices)
    is_qualifying = models.BooleanField(default=False)
    tbr_title_snapshot = models.CharField(max_length=300)
    tbr_author_snapshot = models.CharField(max_length=300)
    tbr_catalog_identity = models.CharField(max_length=160, blank=True)
    submission_title_snapshot = models.CharField(max_length=300)
    submission_author_snapshot = models.CharField(max_length=200)
    submission_catalog_identity = models.CharField(max_length=160, blank=True)
    normalized_title_evidence = models.CharField(max_length=300, blank=True)
    normalized_author_evidence = models.CharField(max_length=300, blank=True)
    evidence_summary = models.TextField(blank=True)
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="reviewed_personal_tbr_matches",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["personal_tbr_book", "submission"], name="unique_personal_tbr_match_relationship"
            ),
            models.UniqueConstraint(
                fields=["submission"], condition=Q(status="confirmed", is_qualifying=True),
                name="one_qualifying_tbr_per_submission",
            ),
            models.UniqueConstraint(
                fields=["personal_tbr_book"], condition=Q(status="confirmed", is_qualifying=True),
                name="one_qualifying_submission_per_tbr_book",
            ),
        ]

    def clean(self):
        errors = {}
        if self.personal_tbr_book_id and self.month_id:
            enrollment = self.personal_tbr_book.personal_tbr.enrollment
            if enrollment.month_id != self.month_id:
                errors["personal_tbr_book"] = "The Personal TBR book must belong to the match Challenge."
            if self.participant_id and enrollment.participant_id != self.participant_id:
                errors["participant"] = "The Reader must own the matched Personal TBR book."
        if self.submission_id and self.month_id and self.submission.month_id != self.month_id:
            errors["submission"] = "The submission must belong to the match Challenge."
        if self.submission_id and self.participant_id and self.submission.participant_id != self.participant_id:
            errors["participant"] = "The Reader must own the matched submission."
        if self.is_qualifying and self.status != self.Status.CONFIRMED:
            errors["is_qualifying"] = "Only a confirmed Personal TBR match can actively qualify."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Personal TBR match history cannot be deleted.")


class PersonalTBRCompletionAward(models.Model):
    personal_tbr = models.OneToOneField(
        PersonalTBR, on_delete=models.PROTECT, related_name="completion_award"
    )
    month = models.ForeignKey(
        ChallengeMonth, on_delete=models.PROTECT, related_name="personal_tbr_completion_awards"
    )
    participant = models.ForeignKey(
        Membership, on_delete=models.PROTECT, related_name="personal_tbr_completion_awards"
    )
    completion_set_fingerprint = models.CharField(max_length=64)
    configured_book_count = models.PositiveSmallIntegerField(default=9)
    bonus_amount_snapshot = models.PositiveIntegerField(default=0)
    qualified_at = models.DateTimeField()
    last_qualified_at = models.DateTimeField()
    effective_date = models.DateField()
    is_qualifying = models.BooleanField(default=True)
    inactivated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-qualified_at", "pk"]
        constraints = [
            models.CheckConstraint(condition=Q(configured_book_count=9), name="personal_tbr_completion_count_nine"),
            models.CheckConstraint(
                condition=Q(bonus_amount_snapshot__gte=0), name="personal_tbr_completion_bonus_nonnegative"
            ),
        ]

    def clean(self):
        errors = {}
        if self.personal_tbr_id:
            enrollment = self.personal_tbr.enrollment
            if self.month_id and enrollment.month_id != self.month_id:
                errors["month"] = "The completion award must belong to the Personal TBR Challenge."
            if self.participant_id and enrollment.participant_id != self.participant_id:
                errors["participant"] = "The completion award Reader must own the Personal TBR."
            if self.personal_tbr.confirmed_at is None:
                errors["personal_tbr"] = "Only a confirmed Personal TBR can have completion history."
        if self.configured_book_count != 9:
            errors["configured_book_count"] = "Personal TBR completion always requires exactly nine books."
        if self.pk and self.configured_books.exists() and self.configured_books.count() != 9:
            errors["configured_book_count"] = "The frozen Personal TBR completion set must contain exactly nine books."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Personal TBR completion award history cannot be deleted.")


class PersonalTBRCompletionAwardBook(models.Model):
    award = models.ForeignKey(
        PersonalTBRCompletionAward, on_delete=models.CASCADE, related_name="configured_books"
    )
    personal_tbr_book = models.ForeignKey(
        PersonalTBRBook, on_delete=models.PROTECT, related_name="completion_award_snapshots"
    )
    position_snapshot = models.PositiveSmallIntegerField()
    title_snapshot = models.CharField(max_length=300)
    author_snapshot = models.CharField(max_length=300)

    class Meta:
        ordering = ["position_snapshot", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["award", "personal_tbr_book"], name="unique_tbr_book_per_completion_award"
            ),
            models.UniqueConstraint(
                fields=["award", "position_snapshot"], name="unique_tbr_position_per_completion_award"
            ),
        ]

    def clean(self):
        if self.award_id and self.personal_tbr_book_id:
            if self.award.personal_tbr_id != self.personal_tbr_book.personal_tbr_id:
                raise ValidationError("The frozen book must belong to the award Personal TBR.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Frozen Personal TBR completion-set history cannot be deleted.")


class BookSubmission(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    class Format(models.TextChoices):
        PAPERBACK = "paperback", "Paperback"
        HARDCOVER = "hardcover", "Hardcover"
        EBOOK = "ebook", "E-book"
        AUDIO = "audio", "Audio"
        MANGA = "manga", "Manga"
        FANFIC = "fanfic", "Fanfic"
        OTHER = "other", "Other"

    class VerificationMethod(models.TextChoices):
        MANUAL = "manual", "Manual Review"
        HARDCOVER = "hardcover", "Hardcover Edition"
        HARDCOVER_AUDIO = "hardcover_audio", "Hardcover Audio Equivalent"

    month = models.ForeignKey(ChallengeMonth, on_delete=models.PROTECT, related_name="submissions")
    participant = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="submissions")
    catalog_book = models.ForeignKey(CatalogBook, null=True, blank=True, on_delete=models.PROTECT, related_name="submissions")
    catalog_edition = models.ForeignKey(CatalogEdition, null=True, blank=True, on_delete=models.PROTECT, related_name="submissions")
    scoring_catalog_edition = models.ForeignKey(CatalogEdition, null=True, blank=True, on_delete=models.PROTECT, related_name="scored_submissions")
    title = models.CharField(max_length=300)
    author = models.CharField(max_length=200)
    book_format = models.CharField(max_length=16, choices=Format.choices)
    started_on = models.DateField(null=True, blank=True)
    completed_on = models.DateField()
    submitted_pages = models.PositiveIntegerField()
    metadata_pages = models.PositiveIntegerField(null=True, blank=True)
    approved_pages = models.PositiveIntegerField(null=True, blank=True)
    bonus_pages = models.PositiveIntegerField(default=0)
    final_scored_pages = models.PositiveIntegerField(null=True, blank=True)
    reference_url = models.URLField(max_length=1000, blank=True)
    verification_url = models.URLField(max_length=1000, blank=True)
    verification_method = models.CharField(max_length=24, choices=VerificationMethod.choices, default=VerificationMethod.MANUAL)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    notes = models.TextField(blank=True)
    review_notes = models.TextField(blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="reviewed_book_submissions")
    is_removed = models.BooleanField(default=False)
    removed_at = models.DateTimeField(null=True, blank=True)
    removed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="removed_book_submissions")
    removal_reason = models.TextField(blank=True)

    @property
    def hardcover_url(self):
        return catalog_hardcover_url(
            catalog_book=self.catalog_book,
            catalog_edition=self.catalog_edition,
        )

    class Meta:
        ordering = ["-submitted_at"]
        constraints = [
            models.CheckConstraint(condition=Q(submitted_pages__gt=0), name="submitted_pages_positive"),
            models.CheckConstraint(condition=Q(approved_pages__isnull=True) | Q(approved_pages__gt=0), name="approved_pages_positive_or_null"),
        ]

    def clean(self):
        if self.participant_id and self.month_id and self.participant.group_id != self.month.group_id:
            raise ValidationError("The participant must belong to the selected reading group.")
        if self.month_id:
            if not self.month.starts_on or not self.month.ends_on:
                raise ValidationError("The Challenge schedule must be configured before accepting submissions.")
            if not (self.month.starts_on <= self.completed_on <= self.month.ends_on):
                raise ValidationError("Completion date must fall inside the challenge month.")
        if self.started_on and self.completed_on and self.started_on > self.completed_on:
            raise ValidationError("Start date cannot be later than completion date.")
        if self.status == self.Status.APPROVED and not self.approved_pages:
            raise ValidationError("Approved submissions require an approved page count.")
        if self.catalog_edition_id and self.catalog_book_id and self.catalog_edition.book_id != self.catalog_book_id:
            raise ValidationError("The selected catalog edition must belong to the selected catalog book.")
        if self.scoring_catalog_edition_id and self.catalog_book_id and self.scoring_catalog_edition.book_id != self.catalog_book_id:
            raise ValidationError("The scoring edition must belong to the selected catalog book.")
        if self.verification_method != self.VerificationMethod.MANUAL:
            if not self.catalog_edition_id or not self.scoring_catalog_edition_id or not self.metadata_pages:
                raise ValidationError("Hardcover-verified submissions require selected and scoring edition records.")
            if self.status != self.Status.APPROVED or self.approved_pages != self.metadata_pages:
                raise ValidationError("Hardcover-verified submissions must use the locked catalog page count.")

    def __str__(self):
        return f"{self.participant.display_name}: {self.title}"

    def save(self, *args, **kwargs):
        if self.status == self.Status.APPROVED and self.approved_pages and self.final_scored_pages is None:
            self.final_scored_pages = self.approved_pages + self.bonus_pages
        elif self.status != self.Status.APPROVED:
            self.final_scored_pages = None
            self.bonus_pages = 0
        super().save(*args, **kwargs)

    def recalculate_score(self, save=True):
        from .scoring import preview_submission_score, refresh_submission_score

        if save:
            return refresh_submission_score(self)
        self.bonus_pages, self.final_scored_pages = preview_submission_score(self)
        return self.final_scored_pages


class BotmMatch(models.Model):
    class Method(models.TextChoices):
        CATALOG_WORK = "catalog_work", "Catalog work"
        NORMALIZED_TITLE_AUTHOR = "normalized_title_author", "Normalized title and author"
        MANUAL_REVIEW = "manual_review", "Manual review"

    class Status(models.TextChoices):
        CONFIRMED = "confirmed", "Confirmed"
        PENDING_REVIEW = "pending_review", "Pending review"
        REJECTED = "rejected", "Rejected"

    botm_book = models.ForeignKey(BotmBook, on_delete=models.PROTECT, related_name="matches")
    month = models.ForeignKey(ChallengeMonth, on_delete=models.PROTECT, related_name="botm_matches")
    participant = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="botm_matches")
    submission = models.ForeignKey(BookSubmission, on_delete=models.PROTECT, related_name="botm_matches")
    method = models.CharField(max_length=32, choices=Method.choices)
    status = models.CharField(max_length=20, choices=Status.choices)
    is_qualifying = models.BooleanField(default=False)
    botm_title_snapshot = models.CharField(max_length=300)
    botm_author_snapshot = models.CharField(max_length=300)
    botm_catalog_identity = models.CharField(max_length=160, blank=True)
    submission_title_snapshot = models.CharField(max_length=300)
    submission_author_snapshot = models.CharField(max_length=200)
    submission_catalog_identity = models.CharField(max_length=160, blank=True)
    evidence_summary = models.TextField(blank=True)
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_botm_matches",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["botm_book", "participant", "submission"],
                name="unique_botm_match_relationship",
            ),
            models.UniqueConstraint(
                fields=["submission"],
                condition=Q(status="confirmed", is_qualifying=True),
                name="one_qualifying_botm_per_submission",
            ),
            models.UniqueConstraint(
                fields=["botm_book", "participant"],
                condition=Q(status="confirmed", is_qualifying=True),
                name="one_qualifying_botm_per_reader",
            ),
        ]

    def clean(self):
        errors = {}
        if self.botm_book_id and self.month_id and self.botm_book.month_id != self.month_id:
            errors["botm_book"] = "The BOTM book must belong to the match Challenge."
        if self.submission_id and self.month_id and self.submission.month_id != self.month_id:
            errors["submission"] = "The submission must belong to the match Challenge."
        if self.submission_id and self.participant_id and self.submission.participant_id != self.participant_id:
            errors["participant"] = "The Reader must own the matched submission."
        if self.participant_id and self.month_id and self.participant.group_id != self.month.group_id:
            errors["participant"] = "The Reader must belong to the match Challenge group."
        if self.is_qualifying and self.status != self.Status.CONFIRMED:
            errors["is_qualifying"] = "Only a confirmed BOTM match can actively qualify."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("BOTM match history cannot be deleted.")


class BotmCompletionAward(models.Model):
    month = models.ForeignKey(ChallengeMonth, on_delete=models.PROTECT, related_name="botm_completion_awards")
    participant = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="botm_completion_awards")
    completion_set_fingerprint = models.CharField(max_length=64)
    configured_book_count = models.PositiveSmallIntegerField()
    bonus_amount_snapshot = models.PositiveIntegerField(default=0)
    qualified_at = models.DateTimeField()
    last_qualified_at = models.DateTimeField()
    effective_date = models.DateField()
    is_qualifying = models.BooleanField(default=True)
    inactivated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-qualified_at", "pk"]
        constraints = [
            models.CheckConstraint(condition=Q(configured_book_count__gte=1), name="botm_completion_count_positive"),
            models.CheckConstraint(condition=Q(bonus_amount_snapshot__gte=0), name="botm_completion_bonus_nonnegative"),
            models.UniqueConstraint(
                fields=["month", "participant", "completion_set_fingerprint"],
                name="unique_botm_completion_set_per_reader",
            ),
        ]

    def clean(self):
        errors = {}
        if self.participant_id and self.month_id and self.participant.group_id != self.month.group_id:
            errors["participant"] = "The completion Reader must belong to the Challenge group."
        if self.pk and self.configured_book_count and self.configured_books.exists() and self.configured_books.count() != self.configured_book_count:
            errors["configured_book_count"] = "The frozen BOTM set must match its denominator snapshot."
        if errors:
            raise ValidationError(errors)

    def delete(self, *args, **kwargs):
        raise ValidationError("BOTM completion award history cannot be deleted.")


class BotmCompletionAwardBook(models.Model):
    award = models.ForeignKey(BotmCompletionAward, on_delete=models.CASCADE, related_name="configured_books")
    botm_book = models.ForeignKey(BotmBook, on_delete=models.PROTECT, related_name="completion_award_snapshots")
    position_snapshot = models.PositiveSmallIntegerField()
    title_snapshot = models.CharField(max_length=300)
    author_snapshot = models.CharField(max_length=300)

    class Meta:
        ordering = ["position_snapshot", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["award", "botm_book"], name="unique_botm_book_per_completion_award"),
            models.UniqueConstraint(fields=["award", "position_snapshot"], name="unique_botm_position_per_completion_award"),
        ]

    def clean(self):
        if self.award_id and self.botm_book_id and self.award.month_id != self.botm_book.month_id:
            raise ValidationError("The frozen BOTM book must belong to the award Challenge.")

    def delete(self, *args, **kwargs):
        raise ValidationError("Frozen BOTM completion-set history cannot be deleted.")


class ThemeClaim(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    submission = models.ForeignKey(BookSubmission, on_delete=models.CASCADE, related_name="theme_claims")
    theme = models.ForeignKey(MonthTheme, on_delete=models.PROTECT, related_name="claims")
    response = models.TextField(blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    approved_bonus_pages = models.PositiveIntegerField(default=0)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="reviewed_theme_claims")
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["theme__starts_on", "theme__name"]
        constraints = [models.UniqueConstraint(fields=["submission", "theme"], name="unique_theme_claim_per_submission")]

    def clean(self):
        if self.submission_id and self.theme_id:
            if self.submission.month_id != self.theme.month_id:
                raise ValidationError("The theme must belong to the submission month.")
            if not (self.theme.starts_on <= self.submission.completed_on <= self.theme.ends_on):
                raise ValidationError("The book completion date must fall inside the theme dates.")

    def __str__(self):
        return f"{self.submission.title} — {self.theme.name}"


class ModifierProvenance(models.Model):
    class SourceType(models.TextChoices):
        THEME_BONUS = "theme_bonus", "Theme bonus"
        LEGACY_MODIFIER = "legacy_modifier", "Legacy Modifier"
        BOTM_BOOK = "botm_book", "Book of the Month book bonus"
        BOTM_COMPLETION = "botm_completion", "Book of the Month completion bonus"
        TBR_BOOK = "tbr_book", "Personal TBR book bonus"
        TBR_COMPLETION = "tbr_completion", "Personal TBR completion bonus"
        GAME_REWARD = "game_reward", "Game/manual reward"

    month = models.ForeignKey(ChallengeMonth, on_delete=models.PROTECT, related_name="modifier_provenance")
    participant = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="modifier_provenance")
    submission = models.ForeignKey(
        BookSubmission,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="modifier_provenance",
    )
    source_type = models.CharField(max_length=24, choices=SourceType.choices)
    source_reference = models.CharField(max_length=120)
    source_label = models.CharField(max_length=200)
    source_context = models.TextField(blank=True)
    amount = models.PositiveIntegerField()
    effective_date = models.DateField()
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="applied_modifier_provenance",
    )
    applied_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_system_generated = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="voided_modifier_provenance",
    )
    voided_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["effective_date", "pk"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="modifier_provenance_amount_positive"),
            models.CheckConstraint(condition=~Q(source_reference=""), name="modifier_provenance_source_present"),
            models.UniqueConstraint(
                fields=["source_type", "source_reference"],
                name="unique_modifier_provenance_source",
            ),
        ]

    def clean(self):
        if self.month_id and self.participant_id and self.participant.group_id != self.month.group_id:
            raise ValidationError("The modifier recipient must belong to the Challenge's reading group.")
        if self.submission_id:
            if self.submission.month_id != self.month_id or self.submission.participant_id != self.participant_id:
                raise ValidationError("The related submission must belong to the same Challenge and Reader.")

    def __str__(self):
        return f"{self.participant.display_name}: {self.source_label} (+{self.amount})"


class Game(models.Model):
    month = models.ForeignKey(ChallengeMonth, on_delete=models.CASCADE, related_name="games")
    name = models.CharField(max_length=120)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    advertised_bonus_pages = models.PositiveIntegerField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["starts_at", "name", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["month", "name"], name="unique_game_name_per_challenge"),
            models.CheckConstraint(condition=Q(advertised_bonus_pages__gt=0), name="game_advertised_bonus_positive"),
            models.CheckConstraint(condition=Q(ends_at__gt=F("starts_at")), name="game_end_after_start"),
        ]

    def clean(self):
        if not self.name.strip():
            raise ValidationError({"name": "Enter a Game name."})
        if self.advertised_bonus_pages is not None and self.advertised_bonus_pages <= 0:
            raise ValidationError({"advertised_bonus_pages": "Advertised bonus pages must be positive."})
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValidationError({"ends_at": "Game ending date/time must be after its starting date/time."})

    def __str__(self):
        return f"{self.month.name} — {self.name}"


class GameRewardApplication(models.Model):
    class TargetType(models.TextChoices):
        READER = "reader", "Reader"
        TEAM = "team", "Team"
        CHALLENGE = "challenge", "Challenge-wide"

    game = models.ForeignKey(Game, on_delete=models.PROTECT, related_name="reward_applications")
    amount = models.PositiveIntegerField()
    target_type = models.CharField(max_length=12, choices=TargetType.choices)
    target_participant = models.ForeignKey(
        Membership,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="targeted_game_reward_applications",
    )
    target_team = models.ForeignKey(
        Team,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="game_reward_applications",
    )
    target_label = models.CharField(max_length=200)
    game_name_snapshot = models.CharField(max_length=120)
    advertised_bonus_pages_snapshot = models.PositiveIntegerField()
    reason = models.TextField()
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="applied_game_rewards",
    )
    applied_at = models.DateTimeField(default=timezone.now)
    idempotency_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    is_voided = models.BooleanField(default=False)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="voided_game_rewards",
    )
    voided_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["applied_at", "pk"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="game_reward_amount_positive"),
            models.CheckConstraint(
                condition=Q(advertised_bonus_pages_snapshot__gt=0),
                name="game_reward_advertised_snapshot_positive",
            ),
            models.CheckConstraint(
                condition=Q(amount__lte=F("advertised_bonus_pages_snapshot")),
                name="game_reward_amount_within_advertised",
            ),
            models.CheckConstraint(condition=~Q(target_label=""), name="game_reward_target_label_present"),
            models.CheckConstraint(condition=~Q(game_name_snapshot=""), name="game_reward_game_name_present"),
            models.CheckConstraint(condition=~Q(reason=""), name="game_reward_reason_present"),
            models.CheckConstraint(
                condition=(
                    Q(target_type="reader", target_participant__isnull=False, target_team__isnull=True)
                    | Q(target_type="team", target_participant__isnull=True, target_team__isnull=False)
                    | Q(target_type="challenge", target_participant__isnull=True, target_team__isnull=True)
                ),
                name="game_reward_target_coherent",
            ),
            models.CheckConstraint(
                condition=(
                    Q(is_voided=False, voided_at__isnull=True, voided_by__isnull=True, void_reason="")
                    | (Q(is_voided=True, voided_at__isnull=False) & ~Q(void_reason=""))
                ),
                name="game_reward_void_state_coherent",
            ),
        ]

    def clean(self):
        errors = {}
        if self.amount is not None and self.amount <= 0:
            errors["amount"] = "Applied reward pages must be positive."
        if (
            self.amount is not None
            and self.advertised_bonus_pages_snapshot is not None
            and self.amount > self.advertised_bonus_pages_snapshot
        ):
            errors["amount"] = "Applied reward pages cannot exceed the advertised amount snapshot."
        for field, message in (
            ("target_label", "Store a target label snapshot."),
            ("game_name_snapshot", "Store a Game name snapshot."),
            ("reason", "Enter a reward reason."),
        ):
            if not getattr(self, field).strip():
                errors[field] = message
        target_is_coherent = (
            self.target_type == self.TargetType.READER
            and self.target_participant_id
            and not self.target_team_id
        ) or (
            self.target_type == self.TargetType.TEAM
            and self.target_team_id
            and not self.target_participant_id
        ) or (
            self.target_type == self.TargetType.CHALLENGE
            and not self.target_participant_id
            and not self.target_team_id
        )
        if not target_is_coherent:
            errors["target_type"] = "The stored target must match the selected target type."
        if self.game_id and self.target_participant_id and self.target_participant.group_id != self.game.month.group_id:
            errors["target_participant"] = "The target Reader must belong to the Game's Group."
        if self.game_id and self.target_team_id and self.target_team.month_id != self.game.month_id:
            errors["target_team"] = "The target Team must belong to the Game's Challenge."
        if self.is_voided:
            if not self.voided_by_id or not self.voided_at or not self.void_reason.strip():
                errors["is_voided"] = "A void requires an actor, timestamp, and reason."
        elif self.voided_by_id or self.voided_at or self.void_reason:
            errors["is_voided"] = "Active applications cannot contain void history."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.game_name_snapshot}: {self.target_label} (+{self.amount})"


class GameRewardRecipient(models.Model):
    application = models.ForeignKey(
        GameRewardApplication,
        on_delete=models.PROTECT,
        related_name="recipients",
    )
    participant = models.ForeignKey(
        Membership,
        on_delete=models.PROTECT,
        related_name="game_reward_recipients",
    )
    provenance = models.OneToOneField(
        ModifierProvenance,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="game_reward_recipient",
    )

    class Meta:
        ordering = ["application_id", "participant_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["application", "participant"],
                name="unique_game_reward_recipient",
            ),
        ]

    def clean(self):
        if self.application_id and self.participant_id:
            if self.participant.group_id != self.application.game.month.group_id:
                raise ValidationError({"participant": "The reward recipient must belong to the Game's Group."})
        if self.provenance_id:
            expected_reference = f"game_reward_recipient:{self.pk}" if self.pk else None
            provenance_errors = (
                self.provenance.source_type != ModifierProvenance.SourceType.GAME_REWARD
                or self.provenance.submission_id is not None
                or self.provenance.month_id != self.application.game.month_id
                or self.provenance.participant_id != self.participant_id
                or self.provenance.amount != self.application.amount
                or (expected_reference and self.provenance.source_reference != expected_reference)
            )
            if provenance_errors:
                raise ValidationError({"provenance": "The scoring effect must match this Game reward recipient."})

    def __str__(self):
        return f"{self.application.game_name_snapshot} — {self.participant.display_name}"


AUDIT_ACTION_LABELS = {
    "platform.root_login": "Platform Owner Signed In",
    "platform.initial_owner_created": "Initial Platform Owner Created",
    "platform.owner_invitation_created": "Platform Owner Invitation Created",
    "platform.owner_invitation_redeemed": "Platform Owner Invitation Accepted",
    "platform.owner_invitation_revoked": "Platform Owner Invitation Revoked",
    "platform.owner_deactivated": "Platform Owner Deactivated",
    "platform.owner_reactivated": "Platform Owner Reactivated",
    "platform.backup_settings_updated": "Backup Schedule Updated",
    "platform.backup_created": "Backup Created",
    "platform.backup_downloaded": "Backup Downloaded",
    "platform.backup_deleted": "Backup Deleted",
    "platform.restore_staged": "Restore Staged",
    "platform.restore_restart_requested": "Restore Restart Requested",
    "platform.general_settings_updated": "General Settings Updated",
    "platform.hardcover_oauth_created": "Hardcover OAuth Configuration Created",
    "platform.hardcover_oauth_updated": "Hardcover OAuth Configuration Updated",
    "platform.disposable_cache_cleaned": "Disposable Cache Cleaned",
    "platform.audit_history_pruned": "Audit History Pruned",
    "platform.sqlite_optimized": "SQLite Database Optimized",
    "account.identity_updated": "Account Identity Updated",
    "participation.self_registered": "Reader Registered",
    "participation.self_reactivated": "Reader Re-registered",
    "participation.self_withdrew": "Reader Withdrew",
    "participation.staff_created": "Reader Added by Staff",
    "participation.staff_reactivated": "Reader Reactivated by Staff",
    "participation.staff_removed": "Reader Removed by Staff",
    "challenge.registration_schema_updated": "Challenge Registration Schema Updated",
    "challenge.tbr_settings_updated": "Personal TBR Settings Updated",
    "challenge.progress_checkpoints_updated": "Challenge Progress Checkpoints Updated",
    "registration.answers_admin_corrected": "Registration Answers Administratively Corrected",
    "team_assignment.ended": "Team Assignment Ended",
    "team_assignment.moved": "Reader Moved Teams",
    "game.reward_applied": "Game Reward Applied",
    "game.reward_voided": "Game Reward Voided",
    "game.created": "Game Created",
    "game.updated": "Game Updated",
    "game.retired": "Game Retired",
    "game.reactivated": "Game Reactivated",
    "game.deleted": "Game Deleted",
    "challenge.games_setting_updated": "Challenge Games Setting Updated",
    "botm.settings_updated": "Book of the Month Settings Updated",
    "botm.book_created": "Book of the Month Book Created",
    "botm.book_updated": "Book of the Month Book Updated",
    "botm.book_retired": "Book of the Month Book Retired",
    "botm.book_reactivated": "Book of the Month Book Reactivated",
    "botm.book_deleted": "Book of the Month Book Deleted",
    "reader_hardcover.connected": "Personal Hardcover Connected",
    "reader_hardcover.replaced": "Personal Hardcover Credential Replaced",
    "reader_hardcover.tested": "Personal Hardcover Connection Tested",
    "reader_hardcover.refreshed": "Personal Hardcover Credential Refreshed",
    "reader_hardcover.reconnect_required": "Personal Hardcover Reconnect Required",
    "reader_hardcover.disconnected": "Personal Hardcover Disconnected",
    "reader_hardcover.sync_completed_books_enabled": "Hardcover Completed-Book Sync Enabled",
    "reader_hardcover.sync_completed_books_disabled": "Hardcover Completed-Book Sync Disabled",
    "reader_hardcover.sync_completion_dates_enabled": "Hardcover Completion-Date Sync Enabled",
    "reader_hardcover.sync_completion_dates_disabled": "Hardcover Completion-Date Sync Disabled",
}
AUDIT_SECRET_PATTERN = re.compile(
    r"(?i)\b((?:[a-z0-9]+[_-])*(?:password(?:[_-]?hash)?|secret(?:[_-]?key)?|"
    r"api[_-]?token|access[_-]?token|refresh[_-]?token|authorization[_-]?code|"
    r"pkce[_-]?verifier|invitation[_-]?token|session[_-]?secret|"
    r"token[_-]?encryption[_-]?key))"
    r"(\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)


def audit_action_label(action):
    return AUDIT_ACTION_LABELS.get(action, action.replace(".", " ").replace("_", " ").title())


def safe_audit_summary(summary):
    summary = re.sub(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", "[REDACTED AUTHORIZATION]", summary)
    summary = re.sub(r"(?i)([?&](?:code|state|code_verifier)=)[^&\s]+", r"\1[REDACTED]", summary)
    return AUDIT_SECRET_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", summary)


class AuditEvent(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    group = models.ForeignKey(ReadingGroup, null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=100)
    object_type = models.CharField(max_length=80)
    object_id = models.CharField(max_length=80, blank=True)
    summary = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def action_label(self):
        return audit_action_label(self.action)

    @property
    def safe_summary(self):
        return safe_audit_summary(self.summary)


class RecoveryOperation(models.Model):
    class Tier(models.IntegerChoices):
        ROUTINE = 1, "Tier 1 — Routine correction"
        DESTRUCTIVE = 2, "Tier 2 — Destructive recovery"
        EMERGENCY = 3, "Tier 3 — Emergency recovery"

    class Result(models.TextChoices):
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    operation_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="recovery_operations")
    created_at = models.DateTimeField(auto_now_add=True)
    tier = models.PositiveSmallIntegerField(choices=Tier.choices)
    action = models.CharField(max_length=120)
    target_type = models.CharField(max_length=120)
    target_id = models.CharField(max_length=120, blank=True)
    target_label = models.CharField(max_length=300)
    group = models.ForeignKey(ReadingGroup, null=True, blank=True, on_delete=models.SET_NULL, related_name="recovery_operations")
    challenge = models.ForeignKey(ChallengeMonth, null=True, blank=True, on_delete=models.SET_NULL, related_name="recovery_operations")
    reason = models.TextField(blank=True)
    confirmation_method = models.CharField(max_length=120, blank=True)
    impact = models.JSONField(default=dict, blank=True)
    before_state = models.JSONField(default=dict, blank=True)
    after_state = models.JSONField(default=dict, blank=True)
    result = models.CharField(max_length=12, choices=Result.choices)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.CheckConstraint(condition=Q(tier__in=[1, 2, 3]), name="recovery_operation_tier_valid"),
        ]

    @property
    def action_label(self):
        return audit_action_label(self.action)

    def save(self, *args, **kwargs):
        if self.pk or (self.operation_id and type(self).objects.filter(operation_id=self.operation_id).exists()):
            raise ValidationError("Recovery ledger records are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Recovery ledger records cannot be deleted.")

    def __str__(self):
        return f"{self.operation_id} — {self.action} — {self.get_result_display()}"
