from django import forms
from django.forms import BaseFormSet, formset_factory
from django.core import signing
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.db import transaction
from django.utils.text import slugify
from django.conf import settings
from zoneinfo import available_timezones

from .models import BookSubmission, BotmBook, CatalogEdition, ChallengeMonth, ChallengeSignupAnswer, ChallengeSignupQuestion, ChallengeStaffAssignment, Game, GameRewardApplication, HardcoverOAuthApplication, Membership, MonthEnrollment, MonthTheme, PlatformBackupSettings, PlatformSettings, ProgressCheckpoint, ReaderHardcoverSyncPreference, ReadingGroup, Team, TeamAssignment, ThemeClaim, UserProfile, normalize_book_identity
from .permissions import DELEGABLE_CAPABILITIES
from .widgets import MidnightDateTimeInput


def avatar_choices():
    avatar_dir = settings.BASE_DIR / "static" / "avatars"
    files = sorted(path.name for path in avatar_dir.glob("*.png")) if avatar_dir.exists() else []
    return [("", "Use My Initials")] + [
        (filename, filename.rsplit(".", 1)[0].replace("_", " ").title())
        for filename in files
    ]


class AvatarFieldsMixin(forms.Form):
    profile_picture = forms.ImageField(
        label="Upload Your Own",
        required=False,
        widget=forms.ClearableFileInput(attrs={"accept": "image/*"}),
        help_text="Optional. A custom upload overrides a built-in avatar.",
    )
    selected_avatar = forms.ChoiceField(
        label="Built-In Avatar",
        required=False,
        widget=forms.RadioSelect,
    )

    def setup_avatar_fields(self, user=None):
        self.fields["selected_avatar"].choices = avatar_choices()
        if user and user.pk:
            profile, _ = UserProfile.objects.get_or_create(user=user)
            self.fields["profile_picture"].initial = profile.profile_picture
            self.fields["selected_avatar"].initial = profile.selected_avatar

    def clean_profile_picture(self):
        uploaded_picture = self.cleaned_data.get("profile_picture")
        if uploaded_picture and uploaded_picture.size > settings.NORTHBOUND_MAX_PROFILE_PICTURE_BYTES:
            maximum_mb = settings.NORTHBOUND_MAX_PROFILE_PICTURE_BYTES / (1024 * 1024)
            raise forms.ValidationError(f"Profile pictures must be {maximum_mb:g} MB or smaller.")
        return uploaded_picture

    def save_avatar(self, user):
        profile, _ = UserProfile.objects.get_or_create(user=user)
        uploaded_picture = self.cleaned_data.get("profile_picture")
        selected_avatar = self.cleaned_data.get("selected_avatar", "")
        clear_upload = self.data.get("profile_picture-clear") == "on"
        if uploaded_picture:
            old_picture = profile.profile_picture
            profile.profile_picture = uploaded_picture
            profile.selected_avatar = ""
            profile.save(update_fields=["profile_picture", "selected_avatar"])
            if old_picture and old_picture.name != profile.profile_picture.name:
                old_picture.delete(save=False)
        elif clear_upload or "selected_avatar" in self.changed_data:
            old_picture = profile.profile_picture
            profile.profile_picture = None
            profile.selected_avatar = selected_avatar
            profile.save(update_fields=["profile_picture", "selected_avatar"])
            if old_picture:
                old_picture.delete(save=False)
        return profile


class RootAuthenticationForm(AuthenticationForm):
    error_messages = {
        **AuthenticationForm.error_messages,
        "not_root": "The credentials entered are not authorized for platform owner access.",
    }

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not user.is_superuser:
            raise forms.ValidationError(self.error_messages["not_root"], code="not_root")


class RegularAuthenticationForm(AuthenticationForm):
    error_messages = {
        **AuthenticationForm.error_messages,
        "root_account": "Platform owner accounts must use the separate owner sign-in.",
    }

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if user.is_superuser:
            raise forms.ValidationError(self.error_messages["root_account"], code="root_account")


class AccountProfileForm(AvatarFieldsMixin, forms.ModelForm):
    discord_username = forms.CharField(
        label="Discord Username",
        required=False,
        max_length=100,
        help_text="Optional. Shared with appropriate Group and Challenge staff for registration and team planning.",
    )
    discord_username_is_public = forms.BooleanField(
        label="Make Discord username public",
        required=False,
        help_text="Allow other members of your Groups to see it on your Group Participant Profile.",
    )

    class Meta:
        model = get_user_model()
        fields = ("username", "first_name", "last_name", "email")
        labels = {"first_name": "First Name", "last_name": "Last Name"}

    def clean_email(self):
        email = self.cleaned_data["email"].strip()
        if not email:
            raise forms.ValidationError("An email address is required for account recovery.")
        return email

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setup_avatar_fields(self.instance)
        if self.instance.pk:
            profile, _ = UserProfile.objects.get_or_create(user=self.instance)
            self.fields["discord_username"].initial = profile.discord_username
            self.fields["discord_username_is_public"].initial = profile.discord_username_is_public

    def save(self, commit=True):
        user = super().save(commit=commit)
        profile = self.save_avatar(user)
        profile.discord_username = self.cleaned_data["discord_username"]
        profile.discord_username_is_public = self.cleaned_data["discord_username_is_public"]
        profile.save(update_fields=["discord_username", "discord_username_is_public"])
        return user


class PlatformAccountIdentityForm(forms.ModelForm):
    discord_username = forms.CharField(
        label="Discord Username",
        required=False,
        max_length=100,
        help_text="Optional reusable profile data. This does not change Group access or participation.",
    )

    class Meta:
        model = get_user_model()
        fields = ("username", "first_name", "last_name", "email")
        labels = {"first_name": "First Name", "last_name": "Last Name"}

    def clean_email(self):
        email = self.cleaned_data["email"].strip()
        if not email:
            raise forms.ValidationError("An email address is required for account recovery.")
        if get_user_model().objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("An account with this email address already exists.")
        return email

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            profile, _ = UserProfile.objects.get_or_create(user=self.instance)
            self.fields["discord_username"].initial = profile.discord_username

    def save(self, commit=True):
        user = super().save(commit=commit)
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.discord_username = self.cleaned_data["discord_username"]
        profile.save(update_fields=["discord_username"])
        return user


class FirstRunSetupForm(UserCreationForm):
    email = forms.EmailField()

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ("username", "email")

    def clean_email(self):
        email = self.cleaned_data["email"].strip()
        if get_user_model().objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email address already exists.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"]
        user.is_staff = True
        user.is_superuser = True
        if commit:
            user.save()
        return user


class PlatformOwnerInvitationForm(forms.Form):
    current_password = forms.CharField(
        label="Your Current Password",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
        help_text="Confirm your identity before generating a full-access invitation.",
    )

    def __init__(self, *args, owner, **kwargs):
        self.owner = owner
        super().__init__(*args, **kwargs)

    def clean_current_password(self):
        password = self.cleaned_data["current_password"]
        if not self.owner.check_password(password):
            raise forms.ValidationError("Your current password is incorrect.")
        return password


class PlatformOwnerStatusForm(forms.Form):
    current_password = forms.CharField(
        label="Your Current Password",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
        help_text="Confirm your identity before changing another Platform Owner's access.",
    )

    def __init__(self, *args, owner, **kwargs):
        self.owner = owner
        super().__init__(*args, **kwargs)

    def clean_current_password(self):
        password = self.cleaned_data["current_password"]
        if not self.owner.check_password(password):
            raise forms.ValidationError("Your current password is incorrect.")
        return password


class PlatformOwnerAcceptanceForm(UserCreationForm):
    email = forms.EmailField()

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ("username", "email")

    def clean_email(self):
        email = self.cleaned_data["email"].strip()
        if get_user_model().objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email address already exists.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"]
        user.is_staff = True
        user.is_superuser = True
        if commit:
            user.save()
        return user


class PlatformBackupSettingsForm(forms.ModelForm):
    weekdays = forms.MultipleChoiceField(
        label="Backup Days",
        choices=PlatformBackupSettings.Weekday.choices,
        widget=forms.CheckboxSelectMultiple,
    )

    class Meta:
        model = PlatformBackupSettings
        fields = ("enabled", "weekdays", "backup_time", "retention_count")
        labels = {
            "enabled": "Enable Automatic Backups",
            "backup_time": "Backup Time",
            "retention_count": "Backups to Keep",
        }
        widgets = {"backup_time": forms.TimeInput(attrs={"type": "time"})}

    def clean_retention_count(self):
        count = self.cleaned_data["retention_count"]
        if not 1 <= count <= 100:
            raise forms.ValidationError("Keep between 1 and 100 automatic backups.")
        return count

    def clean_weekdays(self):
        weekdays = self.cleaned_data["weekdays"]
        if not weekdays:
            raise forms.ValidationError("Select at least one backup day.")
        return [int(day) for day in weekdays]


class PlatformSettingsForm(forms.ModelForm):
    timezone = forms.ChoiceField(
        label="Platform Timezone",
        choices=[(name, name) for name in sorted(available_timezones())],
        help_text="Used for backups, audit dates, System Status, and other installation-wide timestamps. Group timezones are unchanged.",
    )

    class Meta:
        model = PlatformSettings
        fields = (
            "display_name",
            "timezone",
            "allow_public_registration",
            "allow_user_group_creation",
        )
        labels = {
            "display_name": "Platform Display Name",
            "allow_public_registration": "Allow Public Registration",
            "allow_user_group_creation": "Allow Normal Accounts to Create Groups",
        }
        help_texts = {
            "display_name": "The human-readable name of this installation. Northbound remains the application name.",
            "allow_public_registration": "When disabled, existing accounts continue working but new public account creation is unavailable.",
            "allow_user_group_creation": "When disabled, normal accounts can still join existing groups. Platform Owners can still create groups.",
        }

    def clean_display_name(self):
        display_name = self.cleaned_data["display_name"].strip()
        if not display_name:
            raise forms.ValidationError("Enter a platform display name.")
        return display_name


class HardcoverOAuthApplicationForm(forms.ModelForm):
    client_secret = forms.CharField(
        label="Client Secret",
        required=False,
        strip=True,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}, render_value=False),
        help_text="Stored encrypted. Leave blank to keep the configured secret.",
    )

    class Meta:
        model = HardcoverOAuthApplication
        fields = ("enabled", "client_id")
        labels = {"enabled": "Enable Hardcover OAuth", "client_id": "Client ID"}

    def clean_client_id(self):
        return self.cleaned_data["client_id"].strip()

    def clean(self):
        cleaned = super().clean()
        has_secret = bool(self.instance and self.instance.encrypted_client_secret)
        if cleaned.get("enabled"):
            if not cleaned.get("client_id"):
                self.add_error("client_id", "Enter the Hardcover Developer App Client ID before enabling OAuth.")
            if not cleaned.get("client_secret") and not has_secret:
                self.add_error("client_secret", "Enter the Hardcover Developer App Client Secret before enabling OAuth.")
        return cleaned


class PublicRegistrationForm(AvatarFieldsMixin, UserCreationForm):
    email = forms.EmailField()

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ("username", "email")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setup_avatar_fields()

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"]
        if commit:
            user.save()
            self.save_avatar(user)
        return user


class GroupCreateForm(forms.ModelForm):
    hardcover_api_token = forms.CharField(
        label="Hardcover API Token",
        required=False,
        strip=True,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="Optional. Use only read:catalog:data and read:catalog:search. You can also connect this later.",
    )

    class Meta:
        model = ReadingGroup
        fields = ("name", "timezone", "announcement_enabled", "announcement")
        labels = {"announcement_enabled": "Enable Group Announcement", "announcement": "Group Announcement"}
        widgets = {"announcement": forms.Textarea(attrs={"rows": 3})}

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("announcement_enabled") and not cleaned.get("announcement", "").strip():
            self.add_error("announcement", "Enter an announcement before enabling it.")
        return cleaned

    @transaction.atomic
    def save(self, creator, commit=True):
        group = super().save(commit=False)
        base_slug = slugify(group.name) or "reading-group"
        slug = base_slug
        counter = 2
        while ReadingGroup.objects.filter(slug=slug).exists():
            slug = f"{base_slug}-{counter}"
            counter += 1
        group.slug = slug
        if commit:
            group.save()
            if not creator.is_superuser:
                Membership.objects.create(group=group, user=creator, role=Membership.Role.OWNER, display_name=creator.get_full_name() or creator.username)
        return group


class GroupEditForm(forms.ModelForm):
    class Meta:
        model = ReadingGroup
        fields = ("name", "timezone", "announcement_enabled", "announcement")
        labels = {"announcement_enabled": "Enable Group Announcement", "announcement": "Group Announcement"}
        widgets = {"announcement": forms.Textarea(attrs={"rows": 3})}

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("announcement_enabled") and not cleaned.get("announcement", "").strip():
            self.add_error("announcement", "Enter an announcement before enabling it.")
        return cleaned


class HardcoverConnectionForm(forms.Form):
    api_token = forms.CharField(
        label="API Token",
        strip=True,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="Create a token with only read:catalog:data and read:catalog:search.",
    )


class ReaderHardcoverConnectionForm(forms.Form):
    api_token = forms.CharField(
        label="Personal Hardcover API Token",
        strip=True,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text=(
            "This token belongs to your personal Hardcover account, is stored encrypted, and should include "
            "read:catalog, read:library, and write:library. Northbound cannot inspect PAT scope metadata."
        ),
    )


class ReaderHardcoverSyncPreferenceForm(forms.ModelForm):
    class Meta:
        model = ReaderHardcoverSyncPreference
        fields = ("sync_completed_books", "sync_completion_dates")
        labels = {
            "sync_completed_books": "Sync completed books",
            "sync_completion_dates": "Sync Northbound completion dates",
        }

    def __init__(self, *args, write_available=False, unavailable_reason="", **kwargs):
        super().__init__(*args, **kwargs)
        self.write_available = write_available
        self.unavailable_reason = unavailable_reason

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("sync_completed_books"):
            cleaned["sync_completion_dates"] = False
        if cleaned.get("sync_completed_books") and not self.write_available:
            raise forms.ValidationError(
                self.unavailable_reason or "A valid personal Hardcover connection with library-write permission is required."
            )
        return cleaned


class GroupJoinForm(forms.Form):
    access_code = forms.CharField(label="Access Code", min_length=6, max_length=6, help_text="Enter the six-character code shared by the group owner.")

    def clean(self):
        cleaned = super().clean()
        code = cleaned.get("access_code")
        if not code:
            return cleaned
        code = code.strip().upper()
        group = ReadingGroup.objects.filter(join_code=code, is_active=True).first()
        if not group:
            raise forms.ValidationError("That access code is invalid.")
        cleaned["access_code"] = code
        cleaned["group"] = group
        return cleaned


class GroupAccessCodeForm(forms.Form):
    access_code_visibility = forms.ChoiceField(label="Access Code Visibility", choices=ReadingGroup.AccessCodeVisibility.choices)
    regenerate_code = forms.BooleanField(label="Regenerate Code", required=False, help_text="Generate a new code. The current code will immediately stop working.")

    def __init__(self, *args, group, **kwargs):
        super().__init__(*args, **kwargs)
        self.group = group
        self.fields["access_code_visibility"].initial = group.access_code_visibility

    def save(self, group):
        if self.cleaned_data["regenerate_code"] or not group.join_code:
            group.regenerate_access_code()
        group.access_code_visibility = self.cleaned_data["access_code_visibility"]
        group.save(update_fields=["join_code_hash", "join_code_hint", "join_code", "access_code_visibility"])
        return group


class ChallengeMonthForm(forms.ModelForm):
    class Meta:
        model = ChallengeMonth
        fields = (
            "name",
            "description",
            "registration_opens_at",
            "auto_open_registration",
            "registration_closes_at",
            "auto_close_registration",
            "starts_at",
            "auto_start_challenge",
            "ends_at",
            "auto_end_challenge",
            "final_announcement_at",
            "auto_complete_challenge",
        )
        labels = {
            "name": "Challenge Title",
            "registration_opens_at": "Registration Opens",
            "auto_open_registration": "Open registration automatically",
            "registration_closes_at": "Registration Closes",
            "auto_close_registration": "Close registration automatically",
            "starts_at": "Challenge Starts",
            "auto_start_challenge": "Move Challenge to Active automatically",
            "ends_at": "Challenge Ends",
            "auto_end_challenge": "Move Challenge to Finalizing automatically",
            "final_announcement_at": "Final Announcement",
            "auto_complete_challenge": "Mark Challenge Completed at this time",
        }
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
            "registration_opens_at": MidnightDateTimeInput(),
            "registration_closes_at": MidnightDateTimeInput(),
            "starts_at": MidnightDateTimeInput(),
            "ends_at": MidnightDateTimeInput(),
            "final_announcement_at": MidnightDateTimeInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        schedule_fields = (
            "registration_opens_at",
            "registration_closes_at",
            "starts_at",
            "ends_at",
            "final_announcement_at",
        )
        for field_name in schedule_fields:
            self.fields[field_name].input_formats = ["%Y-%m-%dT%H:%M"]
        if not (self.instance and self.instance.pk):
            for field_name in ("registration_opens_at", "registration_closes_at", "starts_at", "ends_at"):
                self.fields[field_name].required = True
        if self.instance and self.instance.pk and self.instance.status == ChallengeMonth.Status.ARCHIVED:
            for field in self.fields.values():
                field.disabled = True

class ChallengeCreateForm(forms.Form):
    name = forms.CharField(label="Challenge Title", max_length=80)
    hosts = forms.ModelMultipleChoiceField(
        label="Hosts",
        queryset=Membership.objects.none(),
        widget=forms.MultipleHiddenInput,
        help_text="Select one or more Group members to configure and operate this Challenge.",
    )

    def __init__(self, *args, group, **kwargs):
        super().__init__(*args, **kwargs)
        self.group = group
        self.fields["hosts"].queryset = group.memberships.filter(
            is_active=True,
            user__is_superuser=False,
        ).order_by("display_name", "pk")

    def save(self, *, created_by):
        with transaction.atomic():
            month = ChallengeMonth.objects.create(
                group=self.group,
                name=self.cleaned_data["name"],
                description="",
                status=ChallengeMonth.Status.DRAFT,
                registration_is_open=False,
            )
            assignments = []
            for membership in self.cleaned_data["hosts"]:
                assignments.append(ChallengeStaffAssignment.objects.create(
                    month=month,
                    membership=membership,
                    role=ChallengeStaffAssignment.Role.HOST,
                    assigned_by=created_by,
                ))
        return month, assignments


class ChallengeGeneralSettingsForm(forms.ModelForm):
    class Meta:
        model = ChallengeMonth
        fields = ("name", "description")
        labels = {"name": "Challenge Title"}
        widgets = {"description": forms.Textarea(attrs={"rows": 4})}


class ChallengeAnnouncementForm(forms.ModelForm):
    class Meta:
        model = ChallengeMonth
        fields = ("announcement",)
        labels = {"announcement": "Challenge Announcement"}
        widgets = {"announcement": forms.Textarea(attrs={"rows": 4})}
        help_texts = {"announcement": "Leave blank when this Challenge does not need its own announcement."}

    def clean(self):
        cleaned = super().clean()
        self.instance.announcement_mode = (
            ChallengeMonth.AnnouncementMode.CUSTOM
            if cleaned.get("announcement", "").strip()
            else ChallengeMonth.AnnouncementMode.NONE
        )
        return cleaned

    def save(self, commit=True):
        challenge = super().save(commit=False)
        if commit:
            challenge.save(update_fields=["announcement", "announcement_mode"])
        return challenge


class ChallengeGamesSettingsForm(forms.ModelForm):
    class Meta:
        model = ChallengeMonth
        fields = ("games_enabled",)
        labels = {"games_enabled": "Enable Games"}
        help_texts = {
            "games_enabled": "Show Games for this Challenge. Disabling Games preserves Games, rewards, and scores.",
        }


class ChallengeBotmSettingsForm(forms.ModelForm):
    class Meta:
        model = ChallengeMonth
        fields = ("botm_enabled", "botm_completion_bonus_pages")
        labels = {
            "botm_enabled": "Enable Book of the Month",
            "botm_completion_bonus_pages": "Full BOTM Completion Bonus Pages",
        }
        help_texts = {
            "botm_enabled": "Show Book of the Month for this Challenge. Disabling it preserves configured books.",
            "botm_completion_bonus_pages": "Use 0 when there is no full-completion bonus.",
        }


class ChallengeScheduleForm(forms.ModelForm):
    class Meta:
        model = ChallengeMonth
        fields = (
            "registration_opens_at",
            "auto_open_registration",
            "registration_closes_at",
            "auto_close_registration",
            "starts_at",
            "auto_start_challenge",
            "ends_at",
            "auto_end_challenge",
            "final_announcement_at",
            "auto_complete_challenge",
        )
        labels = {
            "registration_opens_at": "Registration Opens",
            "auto_open_registration": "Open registration automatically",
            "registration_closes_at": "Registration Closes",
            "auto_close_registration": "Close registration automatically",
            "starts_at": "Challenge Starts",
            "auto_start_challenge": "Move Challenge to Active automatically",
            "ends_at": "Challenge Ends",
            "auto_end_challenge": "Move Challenge to Finalizing automatically",
            "final_announcement_at": "Final Announcement",
            "auto_complete_challenge": "Mark Challenge Completed at this time",
        }
        widgets = {
            field_name: MidnightDateTimeInput()
            for field_name in (
                "registration_opens_at",
                "registration_closes_at",
                "starts_at",
                "ends_at",
                "final_announcement_at",
            )
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name in (
            "registration_opens_at",
            "registration_closes_at",
            "starts_at",
            "ends_at",
            "final_announcement_at",
        ):
            self.fields[field_name].input_formats = ["%Y-%m-%dT%H:%M"]
        if self.instance and self.instance.pk:
            for timestamp_field in ("registration_opens_at", "registration_closes_at", "starts_at", "ends_at"):
                if getattr(self.instance, timestamp_field) is None:
                    self.fields[timestamp_field].required = True
        if self.instance and self.instance.pk and self.instance.status == ChallengeMonth.Status.ARCHIVED:
            for field in self.fields.values():
                field.disabled = True


class ChallengeRegistrationSettingsForm(forms.ModelForm):
    class Meta:
        model = ChallengeMonth
        fields = ("registration_answer_editing_policy", "registration_answer_editing_hours")
        labels = {
            "registration_answer_editing_policy": "Reader Answer Editing",
            "registration_answer_editing_hours": "Editing Duration (Hours)",
        }
        help_texts = {
            "registration_answer_editing_hours": "Used only when editing is allowed for a set period. Choose 1–720 hours.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        policy = self.data.get(self.add_prefix("registration_answer_editing_policy")) if self.is_bound else self.initial.get("registration_answer_editing_policy")
        if self.is_bound and policy != ChallengeMonth.RegistrationAnswerEditingPolicy.TIMED:
            # Ignore irrelevant submitted durations; retain the saved/default duration.
            self.fields["registration_answer_editing_hours"].disabled = True

    def clean(self):
        cleaned = super().clean()
        if (
            cleaned.get("registration_answer_editing_policy")
            == ChallengeMonth.RegistrationAnswerEditingPolicy.TIMED
            and not cleaned.get("registration_answer_editing_hours")
        ):
            self.add_error("registration_answer_editing_hours", "Enter the number of hours Readers may edit answers.")
        return cleaned


class ChallengeTbrSettingsForm(forms.ModelForm):
    class Meta:
        model = ChallengeMonth
        fields = ("tbr_enabled", "tbr_book_bonus_pages", "tbr_completion_bonus_pages")
        labels = {
            "tbr_enabled": "Enable Personal TBR",
            "tbr_book_bonus_pages": "Per-book TBR Bonus Pages",
            "tbr_completion_bonus_pages": "Full 9-Book TBR Completion Bonus Pages",
        }
        help_texts = {
            "tbr_enabled": "Readers may optionally submit up to 9 books during Challenge registration.",
            "tbr_book_bonus_pages": "Bonus awarded for each qualifying completed book from the Reader's locked Personal TBR.",
            "tbr_completion_bonus_pages": "Additional bonus awarded only when a Reader registered exactly 9 TBR books and completes all 9.",
        }


class ChallengeSignupQuestionForm(forms.Form):
    wording = forms.CharField(label="Question", max_length=240)
    question_type = forms.ChoiceField(label="Type", choices=ChallengeSignupQuestion.QuestionType.choices)
    is_required = forms.BooleanField(label="Required", required=False)
    choices_text = forms.CharField(
        label="Choices",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="For Single Choice or Multiple Choice, enter one choice per line (2–20 choices).",
    )

    def clean(self):
        cleaned = super().clean()
        question_type = cleaned.get("question_type")
        choices = [line.strip() for line in cleaned.get("choices_text", "").splitlines() if line.strip()]
        if len(choices) != len(set(choices)):
            self.add_error("choices_text", "Choices must be unique.")
        if len(choices) > 20:
            self.add_error("choices_text", "A question may have at most 20 choices.")
        if any(len(choice) > 120 for choice in choices):
            self.add_error("choices_text", "Each choice must be 120 characters or fewer.")
        choice_types = {
            ChallengeSignupQuestion.QuestionType.SINGLE_CHOICE,
            ChallengeSignupQuestion.QuestionType.MULTIPLE_CHOICE,
        }
        if question_type in choice_types and len(choices) < 2:
            self.add_error("choices_text", "Single Choice and Multiple Choice questions require at least two nonblank choices.")
        if question_type and question_type not in choice_types and choices:
            self.add_error("choices_text", "Choices are available only for Single Choice and Multiple Choice questions.")
        cleaned["choices"] = choices
        return cleaned


class ProgressCheckpointForm(forms.Form):
    checkpoint_id = forms.IntegerField(required=False, widget=forms.HiddenInput)
    scheduled_at = forms.DateTimeField(
        label="Checkpoint Date and Time",
        input_formats=["%Y-%m-%dT%H:%M"],
        widget=MidnightDateTimeInput(),
    )
    threshold_percentage = forms.IntegerField(label="Threshold Percentage", min_value=1, max_value=100, initial=25)
    progress_basis = forms.ChoiceField(label="Progress Basis", choices=ProgressCheckpoint.ProgressBasis.choices)
    target_basis = forms.ChoiceField(label="Target Basis", choices=ProgressCheckpoint.TargetBasis.choices)
    fixed_target_pages = forms.IntegerField(label="Fixed Target Pages", min_value=1, required=False)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("target_basis") == ProgressCheckpoint.TargetBasis.FIXED:
            if not cleaned.get("fixed_target_pages"):
                self.add_error("fixed_target_pages", "Enter a positive page target for a Fixed Target checkpoint.")
        else:
            cleaned["fixed_target_pages"] = None
        return cleaned


class BaseProgressCheckpointFormSet(BaseFormSet):
    ordering_widget = forms.HiddenInput
    deletion_widget = forms.HiddenInput

    def clean(self):
        super().clean()
        active = [form for form in self.forms if form.cleaned_data and not form.cleaned_data.get("DELETE")]
        if len(active) > 5:
            raise forms.ValidationError("A Challenge may have at most five progress checkpoints.")


ProgressCheckpointFormSet = formset_factory(
    ProgressCheckpointForm,
    formset=BaseProgressCheckpointFormSet,
    extra=0,
    can_order=True,
    can_delete=True,
    max_num=5,
    validate_max=True,
)


class BaseChallengeSignupQuestionFormSet(BaseFormSet):
    ordering_widget = forms.HiddenInput
    deletion_widget = forms.HiddenInput

    def clean(self):
        super().clean()
        active_forms = [
            form for form in self.forms
            if form.cleaned_data and not form.cleaned_data.get("DELETE")
        ]
        if len(active_forms) > 10:
            raise forms.ValidationError("A Challenge may have at most ten signup questions.")


ChallengeSignupQuestionFormSet = formset_factory(
    ChallengeSignupQuestionForm,
    formset=BaseChallengeSignupQuestionFormSet,
    extra=0,
    can_delete=True,
    can_order=True,
    max_num=10,
    validate_max=True,
)


class ChallengeRegistrationForm(forms.Form):
    discord_username = forms.CharField(
        label="Discord Username (Optional)",
        required=False,
        max_length=100,
        help_text="Saved as reusable profile data. This does not change your public/private preference.",
    )

    def __init__(self, *args, month, profile, enrollment=None, include_discord=True, answers_editable=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.month = month
        self.profile = profile
        self.enrollment = enrollment
        self.questions = list(month.signup_questions.all())
        if not include_discord or profile.discord_username:
            self.fields.pop("discord_username")
        existing_answers = {
            answer.question_id: answer.value
            for answer in (enrollment.signup_answers.select_related("question") if enrollment else [])
        }
        for question in self.questions:
            field_name = f"question_{question.pk}"
            common = {"label": question.wording, "required": question.is_required}
            if question.question_type == ChallengeSignupQuestion.QuestionType.SHORT_TEXT:
                field = forms.CharField(max_length=500, **common)
            elif question.question_type == ChallengeSignupQuestion.QuestionType.NUMBER:
                field = forms.DecimalField(max_digits=14, decimal_places=2, **common)
            elif question.question_type == ChallengeSignupQuestion.QuestionType.SINGLE_CHOICE:
                field = forms.ChoiceField(choices=[(choice, choice) for choice in question.choices], **common)
            else:
                field = forms.MultipleChoiceField(
                    choices=[(choice, choice) for choice in question.choices],
                    widget=forms.CheckboxSelectMultiple,
                    **common,
                )
            if question.pk in existing_answers:
                field.initial = existing_answers[question.pk]
            field.disabled = not answers_editable
            self.fields[field_name] = field

    def save_profile_discord_username(self):
        if "discord_username" not in self.fields:
            return
        value = self.cleaned_data.get("discord_username", "")
        if value:
            self.profile.discord_username = value
            self.profile.save(update_fields=["discord_username"])

    def save_answers(self, enrollment):
        for question in self.questions:
            value = self.cleaned_data.get(f"question_{question.pk}", "")
            if question.question_type == ChallengeSignupQuestion.QuestionType.NUMBER and value is not None:
                value = format(value, "f")
            ChallengeSignupAnswer.objects.update_or_create(
                enrollment=enrollment,
                question=question,
                defaults={"value": value},
            )


class PersonalTbrRegistrationBookForm(forms.Form):
    catalog_selection = forms.CharField(required=False, widget=forms.HiddenInput())
    title_snapshot = forms.CharField(label="Title", max_length=300, required=False)
    author_snapshot = forms.CharField(label="Author", max_length=300, required=False)
    page_count_snapshot = forms.IntegerField(label="Page Count (Optional)", min_value=1, required=False)

    def clean(self):
        cleaned = super().clean()
        signed_selection = cleaned.get("catalog_selection", "")
        title = cleaned.get("title_snapshot", "").strip()
        author = cleaned.get("author_snapshot", "").strip()
        page_count = cleaned.get("page_count_snapshot")
        if not signed_selection and not title and not author and page_count is None:
            cleaned["is_empty"] = True
            return cleaned
        if signed_selection:
            try:
                selection = signing.loads(
                    signed_selection,
                    salt="northbound.personal-tbr-selection",
                    max_age=86400,
                )
                selected = CatalogEdition.objects.select_related("book").get(pk=selection["selected"])
                scoring = CatalogEdition.objects.select_related("book").get(pk=selection["scoring"])
            except (signing.BadSignature, signing.SignatureExpired, CatalogEdition.DoesNotExist, KeyError, TypeError):
                raise forms.ValidationError("The Hardcover selection is invalid or expired. Search again or use manual entry.")
            if selected.book_id != scoring.book_id or not scoring.page_count:
                raise forms.ValidationError("Select a Hardcover edition with a usable page count.")
            cleaned.update({
                "catalog_book": selected.book,
                "catalog_edition": selected,
                "title_snapshot": selected.book.title,
                "author_snapshot": selected.book.author,
                "page_count_snapshot": scoring.page_count,
                "cover_url_snapshot": selected.book.cover_url,
                "source_url_snapshot": selected.source_url or selected.book.source_url,
            })
        else:
            if not title:
                self.add_error("title_snapshot", "Enter a title or remove this book.")
            if not author:
                self.add_error("author_snapshot", "Enter an author or remove this book.")
            cleaned.update({
                "catalog_book": None,
                "catalog_edition": None,
                "title_snapshot": title,
                "author_snapshot": author,
                "cover_url_snapshot": "",
                "source_url_snapshot": "",
            })
        cleaned["is_empty"] = False
        return cleaned


class BasePersonalTbrRegistrationBookFormSet(BaseFormSet):
    ordering_widget = forms.HiddenInput
    deletion_widget = forms.HiddenInput

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        active = [
            form for form in self.forms
            if form.cleaned_data and not form.cleaned_data.get("DELETE") and not form.cleaned_data.get("is_empty")
        ]
        if len(active) > 9:
            raise forms.ValidationError("A Personal TBR may contain at most nine books.")
        identities = set()
        catalog_ids = set()
        for form in active:
            identity = (
                normalize_book_identity(form.cleaned_data["title_snapshot"]),
                normalize_book_identity(form.cleaned_data["author_snapshot"]),
            )
            if identity in identities:
                raise forms.ValidationError("The same title and author cannot appear twice on your Personal TBR.")
            identities.add(identity)
            catalog_book = form.cleaned_data.get("catalog_book")
            if catalog_book:
                if catalog_book.pk in catalog_ids:
                    raise forms.ValidationError("The same Hardcover book cannot appear twice on your Personal TBR.")
                catalog_ids.add(catalog_book.pk)

    def book_values(self):
        active = [
            form for form in self.forms
            if form.cleaned_data and not form.cleaned_data.get("DELETE") and not form.cleaned_data.get("is_empty")
        ]
        active.sort(key=lambda form: form.cleaned_data.get("ORDER") or self.forms.index(form) + 1)
        return [
            {
                "position": position,
                "catalog_book": form.cleaned_data.get("catalog_book"),
                "catalog_edition": form.cleaned_data.get("catalog_edition"),
                "title_snapshot": form.cleaned_data["title_snapshot"],
                "author_snapshot": form.cleaned_data["author_snapshot"],
                "page_count_snapshot": form.cleaned_data.get("page_count_snapshot"),
                "cover_url_snapshot": form.cleaned_data.get("cover_url_snapshot", ""),
                "source_url_snapshot": form.cleaned_data.get("source_url_snapshot", ""),
            }
            for position, form in enumerate(active, start=1)
        ]


PersonalTbrRegistrationBookFormSet = formset_factory(
    PersonalTbrRegistrationBookForm,
    formset=BasePersonalTbrRegistrationBookFormSet,
    extra=9,
    can_delete=True,
    can_order=True,
    max_num=9,
    validate_max=True,
)


class MonthThemeForm(forms.ModelForm):
    class Meta:
        model = MonthTheme
        fields = ("name", "description", "starts_on", "ends_on", "bonus_pages", "allow_stacking", "prompt", "is_active", "is_visible")
        labels = {"starts_on": "Starts On", "ends_on": "Ends On", "bonus_pages": "Bonus Pages", "allow_stacking": "Allow Stacking", "is_active": "Active", "is_visible": "Visible to Readers"}
        widgets = {"starts_on": forms.DateInput(attrs={"type": "date"}), "ends_on": forms.DateInput(attrs={"type": "date"}), "description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, month=None, **kwargs):
        super().__init__(*args, **kwargs)
        if month:
            self.instance.month = month


class GameForm(forms.ModelForm):
    class Meta:
        model = Game
        fields = ("name", "starts_at", "ends_at", "advertised_bonus_pages")
        labels = {
            "starts_at": "Starts At",
            "ends_at": "Ends At",
            "advertised_bonus_pages": "Maximum Bonus Pages",
        }
        widgets = {
            "starts_at": MidnightDateTimeInput(),
            "ends_at": MidnightDateTimeInput(),
        }

    def __init__(self, *args, month=None, **kwargs):
        super().__init__(*args, **kwargs)
        if month:
            self.instance.month = month
        for field_name in ("starts_at", "ends_at"):
            self.fields[field_name].input_formats = ["%Y-%m-%dT%H:%M"]


class BotmBookForm(forms.ModelForm):
    catalog_selection = forms.CharField(required=False, widget=forms.HiddenInput())
    entry_mode = forms.CharField(required=False, widget=forms.HiddenInput())

    class Meta:
        model = BotmBook
        fields = (
            "position", "title_snapshot", "author_snapshot", "page_count_snapshot",
            "cover_url_snapshot", "source_url_snapshot", "bonus_pages",
        )
        labels = {
            "title_snapshot": "Title", "author_snapshot": "Author",
            "page_count_snapshot": "Page Count", "cover_url_snapshot": "Cover URL",
            "source_url_snapshot": "Source URL", "bonus_pages": "Per-book Bonus Pages",
        }
        help_texts = {"bonus_pages": "Use 0 when this book has no bonus."}

    def __init__(self, *args, month, **kwargs):
        super().__init__(*args, **kwargs)
        self.month = month
        self.fields["position"].widget = forms.Select(choices=[(value, value) for value in range(1, 10)])
        if not self.is_bound and not self.instance.pk:
            used = set(month.botm_books.filter(is_retired=False).values_list("position", flat=True))
            self.initial["position"] = next((value for value in range(1, 10) if value not in used), 1)
        if self.instance.pk and self.instance.catalog_book_id:
            self.fields["title_snapshot"].disabled = True
            self.fields["author_snapshot"].disabled = True
            self.fields["cover_url_snapshot"].disabled = True
            self.fields["source_url_snapshot"].disabled = True

    def clean(self):
        cleaned = super().clean()
        signed_selection = cleaned.get("catalog_selection")
        entry_mode = cleaned.get("entry_mode")
        if entry_mode == "catalog" and not signed_selection:
            raise forms.ValidationError(
                "The Hardcover selection was not retained. Select the edition again before saving."
            )
        if signed_selection:
            try:
                selection = signing.loads(signed_selection, salt="northbound.catalog-selection", max_age=86400)
                selected = CatalogEdition.objects.select_related("book").get(pk=selection["selected"])
                scoring = CatalogEdition.objects.select_related("book").get(pk=selection["scoring"])
            except (signing.BadSignature, signing.SignatureExpired, CatalogEdition.DoesNotExist, KeyError, TypeError):
                raise forms.ValidationError("The Hardcover selection is invalid or expired. Select the edition again or use manual entry.")
            if selected.book_id != scoring.book_id or not scoring.page_count:
                raise forms.ValidationError("Select a Hardcover edition with a usable positive page count.")
            self.instance.catalog_book = selected.book
            self.instance.catalog_edition = selected
            cleaned.update({
                "title_snapshot": selected.book.title,
                "author_snapshot": selected.book.author,
                "page_count_snapshot": scoring.page_count,
                "cover_url_snapshot": selected.book.cover_url,
                "source_url_snapshot": selected.source_url or selected.book.source_url,
            })
        elif not self.instance.pk:
            self.instance.catalog_book = None
            self.instance.catalog_edition = None
        if self.instance.pk and not self.instance.is_retired and cleaned.get("position") != self.initial.get("position", self.instance.position):
            # The service owns atomic position swaps; avoid rejecting the
            # transient occupied target during ModelForm constraint validation.
            self.instance.is_retired = True
        return cleaned

    def service_values(self):
        values = {
            field: self.cleaned_data[field]
            for field in self.Meta.fields
        }
        if self.cleaned_data.get("catalog_selection"):
            values.update({
                "catalog_book": self.instance.catalog_book,
                "catalog_edition": self.instance.catalog_edition,
            })
        return values


class BotmReactivateForm(forms.Form):
    position = forms.TypedChoiceField(
        choices=[(value, value) for value in range(1, 10)], coerce=int, label="Active Position"
    )


class GameRewardApplyForm(forms.Form):
    amount = forms.IntegerField(min_value=1, label="Amount")
    target_type = forms.ChoiceField(
        choices=(
            (GameRewardApplication.TargetType.READER, "Individual"),
            (GameRewardApplication.TargetType.TEAM, "Team"),
            (GameRewardApplication.TargetType.CHALLENGE, "Challenge-wide"),
        ),
        label="Apply To",
    )
    target_participant = forms.ModelChoiceField(
        queryset=Membership.objects.none(), required=False, label="Target Reader"
    )
    target_team = forms.ModelChoiceField(
        queryset=Team.objects.none(), required=False, label="Target Team"
    )
    reason = forms.CharField(label="Reason / Note", widget=forms.Textarea(attrs={"rows": 3}))
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput())

    def __init__(self, *args, game, final_apply=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.game = game
        self.fields["amount"].initial = game.advertised_bonus_pages
        participants = game.month.group.memberships.filter(is_active=True, user__is_superuser=False)
        teams = game.month.teams.all()
        if not final_apply:
            enrolled_ids = game.month.enrollments.filter(is_active=True).values_list("participant_id", flat=True)
            participants = participants.filter(pk__in=enrolled_ids).exclude(
                challenge_staff_assignments__month=game.month,
                challenge_staff_assignments__role=ChallengeStaffAssignment.Role.FLOATER,
                challenge_staff_assignments__ended_at__isnull=True,
            )
            teams = teams.filter(is_archived=False)
        self.fields["target_participant"].queryset = participants.order_by("display_name", "pk")
        self.fields["target_team"].queryset = teams.order_by("name", "pk")

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount > self.game.advertised_bonus_pages:
            raise forms.ValidationError(
                f"Amount cannot exceed this Game's maximum of {self.game.advertised_bonus_pages} pages."
            )
        return amount

    def clean(self):
        cleaned = super().clean()
        target_type = cleaned.get("target_type")
        participant = cleaned.get("target_participant")
        team = cleaned.get("target_team")
        if target_type == GameRewardApplication.TargetType.READER:
            if not participant:
                self.add_error("target_participant", "Select a Reader.")
            if team:
                self.add_error("target_team", "Do not select a Team for an Individual reward.")
        elif target_type == GameRewardApplication.TargetType.TEAM:
            if not team:
                self.add_error("target_team", "Select a Team.")
            if participant:
                self.add_error("target_participant", "Do not select a Reader for a Team reward.")
        elif target_type == GameRewardApplication.TargetType.CHALLENGE:
            if participant or team:
                raise forms.ValidationError("Challenge-wide rewards do not use a Reader or Team target.")
        return cleaned


class GameRewardVoidForm(forms.Form):
    reason = forms.CharField(
        label="Void Reason",
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Required. To correct this reward, void it and create a new application.",
    )


class CompetitionVisibilityForm(forms.ModelForm):
    class Meta:
        model = ChallengeMonth
        fields = ("team_standings_visibility", "reader_scores_visibility")
        labels = {
            "team_standings_visibility": "Team Standings",
            "reader_scores_visibility": "Reader Scores",
        }
        help_texts = {
            "team_standings_visibility": "Controls which Challenge roles can see team totals and team comparisons.",
            "reader_scores_visibility": "Controls which Challenge roles can see individual Reader Base, Modifier, and Total scores.",
        }


class ChallengeHostAssignmentForm(forms.ModelForm):
    class Meta:
        model = ChallengeStaffAssignment
        fields = ("membership",)
        labels = {"membership": "Group Member"}

    def __init__(self, *args, month, **kwargs):
        super().__init__(*args, **kwargs)
        self.month = month
        active_host_ids = month.staff_assignments.filter(
            role=ChallengeStaffAssignment.Role.HOST,
            ended_at__isnull=True,
        ).values_list("membership_id", flat=True)
        active_floater_ids = month.staff_assignments.filter(
            role=ChallengeStaffAssignment.Role.FLOATER,
            ended_at__isnull=True,
        ).values_list("membership_id", flat=True)
        self.fields["membership"].queryset = month.group.memberships.filter(
            is_active=True,
            user__is_superuser=False,
        ).exclude(pk__in=active_host_ids).exclude(pk__in=active_floater_ids)

    def save(self, assigned_by, commit=True):
        assignment = super().save(commit=False)
        assignment.month = self.month
        assignment.role = ChallengeStaffAssignment.Role.HOST
        assignment.assigned_by = assigned_by
        if commit:
            assignment.save()
        return assignment


class ChallengeTeamLeaderAssignmentForm(forms.ModelForm):
    class Meta:
        model = ChallengeStaffAssignment
        fields = ("membership",)
        labels = {"membership": "Team Member"}

    def __init__(self, *args, team, **kwargs):
        super().__init__(*args, **kwargs)
        self.team = team
        active_leader_ids = team.staff_assignments.filter(
            role=ChallengeStaffAssignment.Role.TEAM_LEADER,
            ended_at__isnull=True,
        ).values_list("membership_id", flat=True)
        active_floater_ids = team.month.staff_assignments.filter(
            role=ChallengeStaffAssignment.Role.FLOATER,
            ended_at__isnull=True,
        ).values_list("membership_id", flat=True)
        assigned_ids = team.assignments.filter(
            participant__is_active=True,
            participant__user__is_superuser=False,
            participant__month_enrollments__month=team.month,
            participant__month_enrollments__is_active=True,
            ended_at__isnull=True,
        ).values_list("participant_id", flat=True)
        self.fields["membership"].queryset = team.month.group.memberships.filter(
            pk__in=assigned_ids,
        ).exclude(pk__in=active_leader_ids).exclude(pk__in=active_floater_ids)

    def save(self, assigned_by, commit=True):
        assignment = super().save(commit=False)
        assignment.month = self.team.month
        assignment.team = self.team
        assignment.role = ChallengeStaffAssignment.Role.TEAM_LEADER
        assignment.assigned_by = assigned_by
        if commit:
            assignment.save()
        return assignment


class ChallengeFloaterAssignmentForm(forms.ModelForm):
    class Meta:
        model = ChallengeStaffAssignment
        fields = ("membership",)
        labels = {"membership": "Group Member"}

    def __init__(self, *args, month, **kwargs):
        super().__init__(*args, **kwargs)
        self.month = month
        excluded_ids = month.enrollments.filter(is_active=True).values_list("participant_id", flat=True)
        active_staff_ids = month.staff_assignments.filter(
            role__in=(ChallengeStaffAssignment.Role.HOST, ChallengeStaffAssignment.Role.TEAM_LEADER, ChallengeStaffAssignment.Role.FLOATER),
            ended_at__isnull=True,
        ).values_list("membership_id", flat=True)
        self.fields["membership"].queryset = month.group.memberships.filter(
            is_active=True,
            user__is_superuser=False,
        ).exclude(pk__in=excluded_ids).exclude(pk__in=active_staff_ids)

    def save(self, assigned_by, commit=True):
        assignment = super().save(commit=False)
        assignment.month = self.month
        assignment.team = None
        assignment.role = ChallengeStaffAssignment.Role.FLOATER
        assignment.assigned_by = assigned_by
        if commit:
            assignment.save()
        return assignment


class TeamForm(forms.ModelForm):
    class Meta:
        model = Team
        fields = ("name", "color")
        widgets = {"color": forms.TextInput(attrs={"type": "color"})}


class MemberCreateForm(forms.Form):
    username = forms.CharField(max_length=150)
    display_name = forms.CharField(label="Display Name", max_length=100)
    email = forms.EmailField(required=False)
    role = forms.ChoiceField(choices=Membership.Role.choices, initial=Membership.Role.MEMBER)
    temporary_password = forms.CharField(label="Temporary Password", widget=forms.PasswordInput, min_length=12)

    def clean_username(self):
        username = self.cleaned_data["username"]
        if get_user_model().objects.filter(username=username).exists():
            raise forms.ValidationError("That username is already in use.")
        return username

    @transaction.atomic
    def save(self, group):
        user = get_user_model().objects.create_user(
            username=self.cleaned_data["username"],
            email=self.cleaned_data["email"],
            password=self.cleaned_data["temporary_password"],
        )
        return Membership.objects.create(
            group=group,
            user=user,
            display_name=self.cleaned_data["display_name"],
            role=self.cleaned_data["role"],
        )


class MembershipPermissionsForm(forms.Form):
    role = forms.ChoiceField(choices=Membership.Role.choices, help_text="The role supplies the default permission preset.")

    OVERRIDE_CHOICES = (
        ("inherit", "Use Role Default"),
        ("allow", "Allow"),
        ("deny", "Deny"),
    )

    def __init__(self, *args, membership, **kwargs):
        super().__init__(*args, **kwargs)
        self.membership = membership
        self.fields["role"].initial = membership.role
        for capability, label in DELEGABLE_CAPABILITIES.items():
            current = membership.permission_overrides.get(capability)
            initial = "inherit" if current is None else ("allow" if current else "deny")
            self.fields[capability] = forms.ChoiceField(
                label=label,
                choices=self.OVERRIDE_CHOICES,
                initial=initial,
            )

    def save(self):
        self.membership.role = self.cleaned_data["role"]
        # Only mutate capability keys represented by this form. Legacy,
        # deferred, or backup-restored keys require an explicit data migration
        # before they may be discarded.
        overrides = dict(self.membership.permission_overrides or {})
        for capability in DELEGABLE_CAPABILITIES:
            value = self.cleaned_data[capability]
            if value == "inherit":
                overrides.pop(capability, None)
            else:
                overrides[capability] = value == "allow"
        self.membership.permission_overrides = overrides
        self.membership.save(update_fields=["role", "permission_overrides"])
        return self.membership


class TeamAssignmentForm(forms.ModelForm):
    class Meta:
        model = TeamAssignment
        fields = ("participant", "team")

    def __init__(self, *args, month, **kwargs):
        super().__init__(*args, **kwargs)
        self.month = month
        assigned_ids = month.team_assignments.filter(ended_at__isnull=True).values_list("participant_id", flat=True)
        self.fields["participant"].queryset = month.group.memberships.filter(is_active=True, user__is_superuser=False).exclude(pk__in=assigned_ids)
        self.fields["team"].queryset = month.teams.filter(is_archived=False)

    def clean(self):
        cleaned = super().clean()
        participant = cleaned.get("participant")
        if participant and self.month.staff_assignments.filter(
            membership=participant,
            role=ChallengeStaffAssignment.Role.FLOATER,
            ended_at__isnull=True,
        ).exists():
            self.add_error("participant", "End this member's active Floater assignment before assigning them to a team.")
        return cleaned

    def save(self, actor, commit=True):
        assignment = super().save(commit=False)
        assignment.month = self.month
        if commit:
            from .participation import assign_participant_to_team

            assignment, _, _ = assign_participant_to_team(
                month=self.month,
                participant=assignment.participant,
                team=assignment.team,
                actor=actor,
            )
        return assignment


class MonthEnrollmentForm(forms.Form):
    participant = forms.ModelChoiceField(queryset=Membership.objects.none())
    team = forms.ModelChoiceField(queryset=Team.objects.none(), required=False, help_text="Optional. The participant can be assigned to a team later.")

    def __init__(self, *args, month, **kwargs):
        super().__init__(*args, **kwargs)
        self.month = month
        enrolled_ids = month.enrollments.filter(is_active=True).values_list("participant_id", flat=True)
        self.fields["participant"].queryset = month.group.memberships.filter(is_active=True, user__is_superuser=False).exclude(pk__in=enrolled_ids)
        self.fields["team"].queryset = month.teams.all()

    def clean(self):
        cleaned = super().clean()
        participant = cleaned.get("participant")
        if participant and self.month.staff_assignments.filter(
            membership=participant,
            role=ChallengeStaffAssignment.Role.FLOATER,
            ended_at__isnull=True,
        ).exists():
            self.add_error("participant", "End this member's active Floater assignment before enrolling them as a Reader.")
        return cleaned

    def save(self, enrolled_by=None, commit=True):
        from .participation import activate_participation, assign_participant_to_team

        participant = self.cleaned_data["participant"]
        team = self.cleaned_data.get("team")
        if team:
            _, enrollment, _ = assign_participant_to_team(
                month=self.month,
                participant=participant,
                team=team,
                actor=enrolled_by,
            )
        else:
            enrollment, _, _ = activate_participation(
                month=self.month,
                participant=participant,
                actor=enrolled_by,
                origin=MonthEnrollment.Origin.STAFF,
            )
        return enrollment


class MonthParticipantEditForm(forms.Form):
    team = forms.ModelChoiceField(queryset=Team.objects.none(), required=False, help_text="Leave blank to keep the participant enrolled without a team.")

    def __init__(self, *args, enrollment, **kwargs):
        super().__init__(*args, **kwargs)
        self.enrollment = enrollment
        self.fields["team"].queryset = enrollment.month.teams.filter(is_archived=False)
        assignment = TeamAssignment.objects.filter(
            month=enrollment.month,
            participant=enrollment.participant,
            ended_at__isnull=True,
        ).first()
        self.fields["team"].initial = assignment.team_id if assignment else None

    def save(self, actor=None):
        team = self.cleaned_data.get("team")
        from .participation import assign_participant_to_team, end_team_assignment

        assignment = TeamAssignment.objects.filter(
            month=self.enrollment.month,
            participant=self.enrollment.participant,
            ended_at__isnull=True,
        ).first()
        previous_team = assignment.team if assignment else None
        if team:
            assign_participant_to_team(
                month=self.enrollment.month,
                participant=self.enrollment.participant,
                team=team,
                actor=actor,
            )
        elif assignment:
            end_team_assignment(
                assignment=assignment,
                actor=actor,
                reason="staff left the Reader unassigned",
            )
        return previous_team, team


class MembershipRoleForm(forms.ModelForm):
    class Meta:
        model = Membership
        fields = ("role", "is_active")


class BookSubmissionForm(forms.ModelForm):
    catalog_selection = forms.CharField(required=False, widget=forms.HiddenInput())
    themes = forms.ModelMultipleChoiceField(queryset=MonthTheme.objects.none(), required=False, widget=forms.CheckboxSelectMultiple, label="Theme Claims")

    class Meta:
        model = BookSubmission
        fields = ("title", "author", "book_format", "started_on", "completed_on", "submitted_pages", "reference_url", "notes")
        labels = {"book_format": "Book Format", "started_on": "Started On", "completed_on": "Completed On", "submitted_pages": "Submitted Pages", "reference_url": "Reference Link"}
        help_texts = {"reference_url": "Optional. Link to the exact edition or another page that helps reviewers verify the page count."}
        widgets = {"started_on": forms.DateInput(attrs={"type": "date"}), "completed_on": forms.DateInput(attrs={"type": "date"}), "notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, month=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.month = month
        if month:
            themes = month.themes.filter(is_active=True, is_visible=True)
            self.fields["themes"].queryset = themes
            for theme in themes.exclude(prompt=""):
                self.fields[f"theme_response_{theme.pk}"] = forms.CharField(
                    label=theme.prompt,
                    required=False,
                    widget=forms.Textarea(attrs={"rows": 2, "data-theme-response": theme.pk}),
                )

    def clean(self):
        cleaned = super().clean()
        themes = list(cleaned.get("themes") or [])
        completed_on = cleaned.get("completed_on")
        if completed_on:
            for theme in themes:
                if not (theme.starts_on <= completed_on <= theme.ends_on):
                    self.add_error("themes", f"{theme.name} only applies from {theme.starts_on} through {theme.ends_on}.")
                if theme.prompt and not cleaned.get(f"theme_response_{theme.pk}", "").strip():
                    self.add_error(f"theme_response_{theme.pk}", "Answer this prompt to claim the theme.")
        if len(themes) > 1 and any(not theme.allow_stacking for theme in themes):
            self.add_error("themes", "A selected theme cannot be stacked with another theme on the same book.")
        signed_selection = cleaned.get("catalog_selection")
        if not signed_selection:
            self.instance.catalog_book = None
            self.instance.catalog_edition = None
            self.instance.scoring_catalog_edition = None
            self.instance.metadata_pages = None
            self.instance.verification_method = BookSubmission.VerificationMethod.MANUAL
            return cleaned
        try:
            selection = signing.loads(signed_selection, salt="northbound.catalog-selection", max_age=86400)
            selected = CatalogEdition.objects.select_related("book").get(pk=selection["selected"])
            scoring = CatalogEdition.objects.select_related("book").get(pk=selection["scoring"])
        except (signing.BadSignature, signing.SignatureExpired, CatalogEdition.DoesNotExist, KeyError, TypeError):
            raise forms.ValidationError("The Hardcover selection is invalid or expired. Select the edition again or use manual entry.")
        if selected.book_id != scoring.book_id or not scoring.page_count:
            raise forms.ValidationError("The Hardcover scoring edition is not valid for this book.")
        method = selection.get("method")
        if method not in {BookSubmission.VerificationMethod.HARDCOVER, BookSubmission.VerificationMethod.HARDCOVER_AUDIO}:
            raise forms.ValidationError("The Hardcover verification method is invalid.")
        selected_format = selected.format_name.casefold()
        if "audio" in selected_format or selected.audio_seconds:
            book_format = BookSubmission.Format.AUDIO
        elif "paperback" in selected_format:
            book_format = BookSubmission.Format.PAPERBACK
        elif "hardcover" in selected_format or "hardback" in selected_format:
            book_format = BookSubmission.Format.HARDCOVER
        elif any(value in selected_format for value in ("ebook", "e-book", "kindle", "digital")):
            book_format = BookSubmission.Format.EBOOK
        else:
            book_format = BookSubmission.Format.OTHER
        cleaned.update({
            "title": selected.book.title,
            "author": selected.book.author,
            "book_format": book_format,
            "submitted_pages": scoring.page_count,
            "reference_url": selected.source_url,
        })
        self.instance.catalog_book = selected.book
        self.instance.catalog_edition = selected
        self.instance.scoring_catalog_edition = scoring
        self.instance.metadata_pages = scoring.page_count
        self.instance.reference_url = selected.source_url
        self.instance.verification_url = scoring.source_url
        self.instance.verification_method = method
        self.instance.status = BookSubmission.Status.APPROVED
        self.instance.approved_pages = scoring.page_count
        return cleaned

    def save_theme_claims(self, submission):
        claims = []
        for theme in self.cleaned_data.get("themes") or []:
            claim = ThemeClaim.objects.create(submission=submission, theme=theme, response=self.cleaned_data.get(f"theme_response_{theme.pk}", "").strip())
            claims.append(claim)
        return claims


class SubmissionReviewForm(forms.ModelForm):
    class Meta:
        model = BookSubmission
        fields = ("approved_pages", "status", "verification_url", "review_notes")
        labels = {"approved_pages": "Approved Pages", "verification_url": "Verification Link", "review_notes": "Review Notes"}
        help_texts = {"verification_url": "Optional. Add the source used to verify the approved page count."}
        widgets = {"review_notes": forms.Textarea(attrs={"rows": 3})}


class ThemeClaimReviewForm(forms.ModelForm):
    class Meta:
        model = ThemeClaim
        fields = ("status",)
