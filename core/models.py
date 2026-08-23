from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q, Sum
from django.urls import reverse
from django.templatetags.static import static
from django.utils import timezone
from datetime import timedelta
from datetime import time as datetime_time
from django.core.validators import MaxValueValidator, MinValueValidator
import hashlib
import secrets


def profile_picture_path(instance, filename):
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    return f"profile-pictures/user-{instance.user_id}.{extension}"


class UserProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="northbound_profile")
    profile_picture = models.ImageField(upload_to=profile_picture_path, blank=True)
    selected_avatar = models.CharField(max_length=100, blank=True)
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
    weekday = models.PositiveSmallIntegerField(choices=Weekday.choices, default=Weekday.MONDAY)
    backup_time = models.TimeField(default=datetime_time(1, 0))
    retention_count = models.PositiveSmallIntegerField(default=5, validators=[MinValueValidator(1), MaxValueValidator(100)])
    last_run_date = models.DateField(null=True, blank=True, editable=False)
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
        STAFF = "staff", "Owners, administrators, and moderators"
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

class Membership(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", "Group owner"
        ADMIN = "admin", "Group administrator"
        MODERATOR = "moderator", "Moderator"
        GAME_MANAGER = "game_manager", "Game manager"
        READER = "reader", "Reader"

    group = models.ForeignKey(ReadingGroup, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reading_memberships")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.READER)
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
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"
        FINALIZED = "finalized", "Finalized"
        ARCHIVED = "archived", "Archived"

    class TeamStatsVisibility(models.TextChoices):
        EVERYONE = "everyone", "Everyone in the group"
        STAFF = "staff", "Owners, administrators, and moderators"
        OWNER = "owner", "Group owners only"

    class AnnouncementMode(models.TextChoices):
        INHERIT = "inherit", "Use Group Announcement"
        CUSTOM = "custom", "Custom Announcement"
        NONE = "none", "No Announcement"

    group = models.ForeignKey(ReadingGroup, on_delete=models.CASCADE, related_name="challenge_months")
    name = models.CharField(max_length=80)
    starts_on = models.DateField()
    ends_on = models.DateField()
    late_entry_deadline = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    team_stats_visibility = models.CharField(max_length=10, choices=TeamStatsVisibility.choices, default=TeamStatsVisibility.OWNER)
    announcement_mode = models.CharField(max_length=10, choices=AnnouncementMode.choices, default=AnnouncementMode.INHERIT)
    announcement = models.TextField(blank=True, help_text="Message displayed when Custom Announcement is selected.")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["group", "name"], name="unique_group_challenge_month")]
        ordering = ["-starts_on"]

    def clean(self):
        if self.ends_on < self.starts_on:
            raise ValidationError("The end date must be on or after the start date.")
        if self.announcement_mode == self.AnnouncementMode.CUSTOM and not self.announcement.strip():
            raise ValidationError({"announcement": "Enter an announcement or select a different announcement option."})

    def __str__(self):
        return f"{self.group}: {self.name}"

    def get_absolute_url(self):
        return reverse("month-detail", kwargs={"group_slug": self.group.slug, "pk": self.pk})

    @property
    def effective_announcement(self):
        if self.announcement_mode == self.AnnouncementMode.CUSTOM:
            return self.announcement.strip()
        if self.announcement_mode == self.AnnouncementMode.INHERIT:
            return self.group.announcement.strip() if self.group.announcement_enabled else ""
        return ""


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
        if self.month_id and (self.starts_on < self.month.starts_on or self.ends_on > self.month.ends_on):
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
        return self.month.submissions.filter(
            participant__team_assignments__team=self,
            participant__team_assignments__month=self.month,
            status=BookSubmission.Status.APPROVED,
            is_removed=False,
        ).aggregate(total=Sum("final_scored_pages"))["total"] or 0


class MonthEnrollment(models.Model):
    month = models.ForeignKey(ChallengeMonth, on_delete=models.CASCADE, related_name="enrollments")
    participant = models.ForeignKey(Membership, on_delete=models.CASCADE, related_name="month_enrollments")
    enrolled_at = models.DateTimeField(auto_now_add=True)
    enrolled_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="created_month_enrollments")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["month", "participant"], name="one_enrollment_per_participant_per_month")]
        ordering = ["participant__display_name"]

    def clean(self):
        if self.participant_id and self.month_id and self.participant.group_id != self.month.group_id:
            raise ValidationError("The participant must belong to the same reading group.")

    def __str__(self):
        return f"{self.month.name} — {self.participant.display_name}"


class TeamAssignment(models.Model):
    month = models.ForeignKey(ChallengeMonth, on_delete=models.CASCADE, related_name="team_assignments")
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="assignments")
    participant = models.ForeignKey(Membership, on_delete=models.CASCADE, related_name="team_assignments")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["month", "participant"], name="one_team_per_participant_per_month")]

    def clean(self):
        if self.team_id and self.month_id and self.team.month_id != self.month_id:
            raise ValidationError("The team must belong to the selected challenge month.")
        if self.participant_id and self.month_id and self.participant.group_id != self.month.group_id:
            raise ValidationError("The participant must belong to the same reading group.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
        MonthEnrollment.objects.get_or_create(month=self.month, participant=self.participant)


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

    class Meta:
        ordering = ["-submitted_at"]
        constraints = [
            models.CheckConstraint(condition=Q(submitted_pages__gt=0), name="submitted_pages_positive"),
            models.CheckConstraint(condition=Q(approved_pages__isnull=True) | Q(approved_pages__gt=0), name="approved_pages_positive_or_null"),
        ]

    def clean(self):
        if self.participant_id and self.month_id and self.participant.group_id != self.month.group_id:
            raise ValidationError("The participant must belong to the selected reading group.")
        if self.month_id and not (self.month.starts_on <= self.completed_on <= self.month.ends_on):
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
        if self.status == self.Status.APPROVED and self.approved_pages:
            bonus = self.theme_claims.filter(status=ThemeClaim.Status.APPROVED).aggregate(total=Sum("approved_bonus_pages"))["total"] or 0
            self.bonus_pages = bonus
            self.final_scored_pages = self.approved_pages + bonus
        else:
            self.bonus_pages = 0
            self.final_scored_pages = None
        if save:
            self.save(update_fields=["bonus_pages", "final_scored_pages"])
        return self.final_scored_pages


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
