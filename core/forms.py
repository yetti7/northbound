from django import forms
from django.core import signing
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.db import transaction
from django.utils.text import slugify
from django.conf import settings

from .models import BookSubmission, CatalogEdition, ChallengeMonth, Membership, MonthEnrollment, MonthTheme, ReadingGroup, Team, TeamAssignment, ThemeClaim, UserProfile
from .permissions import CAPABILITIES


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

    def save(self, commit=True):
        user = super().save(commit=commit)
        self.save_avatar(user)
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


class PlatformOwnerCreationForm(UserCreationForm):
    email = forms.EmailField()
    current_password = forms.CharField(
        label="Your Current Password",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
        help_text="Confirm your identity before granting full platform access.",
    )

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ("username", "email")

    def __init__(self, *args, owner, **kwargs):
        self.owner = owner
        super().__init__(*args, **kwargs)

    def clean_email(self):
        email = self.cleaned_data["email"].strip()
        if get_user_model().objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email address already exists.")
        return email

    def clean_current_password(self):
        password = self.cleaned_data["current_password"]
        if not self.owner.check_password(password):
            raise forms.ValidationError("Your current password is incorrect.")
        return password

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"]
        user.is_staff = True
        user.is_superuser = True
        if commit:
            user.save()
        return user


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
        fields = ("name", "starts_on", "ends_on", "late_entry_deadline", "status", "announcement_mode", "announcement")
        labels = {"starts_on": "Starts On", "ends_on": "Ends On", "late_entry_deadline": "Late Entry Deadline", "announcement_mode": "Announcement", "announcement": "Custom Announcement"}
        widgets = {"starts_on": forms.DateInput(attrs={"type": "date"}), "ends_on": forms.DateInput(attrs={"type": "date"}), "late_entry_deadline": forms.DateInput(attrs={"type": "date"}), "announcement": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["announcement_mode"].required = False
        if self.instance and self.instance.pk:
            transitions = {
                ChallengeMonth.Status.DRAFT: (ChallengeMonth.Status.DRAFT, ChallengeMonth.Status.OPEN),
                ChallengeMonth.Status.OPEN: (ChallengeMonth.Status.OPEN, ChallengeMonth.Status.CLOSED),
                ChallengeMonth.Status.CLOSED: (ChallengeMonth.Status.CLOSED, ChallengeMonth.Status.FINALIZED),
                ChallengeMonth.Status.FINALIZED: (ChallengeMonth.Status.FINALIZED, ChallengeMonth.Status.ARCHIVED),
                ChallengeMonth.Status.ARCHIVED: (ChallengeMonth.Status.ARCHIVED,),
            }
            allowed = transitions[self.instance.status]
            self.fields["status"].choices = [choice for choice in ChallengeMonth.Status.choices if choice[0] in allowed]
            if self.instance.status in {ChallengeMonth.Status.FINALIZED, ChallengeMonth.Status.ARCHIVED}:
                for name, field in self.fields.items():
                    if name != "status":
                        field.disabled = True
            if self.instance.status == ChallengeMonth.Status.ARCHIVED:
                self.fields["status"].disabled = True

    def clean_announcement_mode(self):
        return self.cleaned_data.get("announcement_mode") or ChallengeMonth.AnnouncementMode.INHERIT


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


class TeamStatsVisibilityForm(forms.ModelForm):
    class Meta:
        model = ChallengeMonth
        fields = ("team_stats_visibility",)
        labels = {"team_stats_visibility": "Visibility"}


class TeamForm(forms.ModelForm):
    class Meta:
        model = Team
        fields = ("name", "color")
        widgets = {"color": forms.TextInput(attrs={"type": "color"})}


class MemberCreateForm(forms.Form):
    username = forms.CharField(max_length=150)
    display_name = forms.CharField(label="Display Name", max_length=100)
    email = forms.EmailField(required=False)
    role = forms.ChoiceField(choices=Membership.Role.choices, initial=Membership.Role.READER)
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
        for capability, label in CAPABILITIES.items():
            current = membership.permission_overrides.get(capability)
            initial = "inherit" if current is None else ("allow" if current else "deny")
            self.fields[capability] = forms.ChoiceField(
                label=label,
                choices=self.OVERRIDE_CHOICES,
                initial=initial,
            )

    def save(self):
        self.membership.role = self.cleaned_data["role"]
        overrides = {}
        for capability in CAPABILITIES:
            value = self.cleaned_data[capability]
            if value != "inherit":
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
        assigned_ids = month.team_assignments.values_list("participant_id", flat=True)
        self.fields["participant"].queryset = month.group.memberships.filter(is_active=True, user__is_superuser=False).exclude(pk__in=assigned_ids)
        self.fields["team"].queryset = month.teams.filter(is_archived=False)

    def save(self, commit=True):
        assignment = super().save(commit=False)
        assignment.month = self.month
        if commit:
            assignment.full_clean()
            assignment.save()
        return assignment


class MonthEnrollmentForm(forms.ModelForm):
    team = forms.ModelChoiceField(queryset=Team.objects.none(), required=False, help_text="Optional. The participant can be assigned to a team later.")

    class Meta:
        model = MonthEnrollment
        fields = ("participant",)

    def __init__(self, *args, month, **kwargs):
        super().__init__(*args, **kwargs)
        self.month = month
        enrolled_ids = month.enrollments.values_list("participant_id", flat=True)
        self.fields["participant"].queryset = month.group.memberships.filter(is_active=True, user__is_superuser=False).exclude(pk__in=enrolled_ids)
        self.fields["team"].queryset = month.teams.all()

    def save(self, enrolled_by=None, commit=True):
        enrollment = super().save(commit=False)
        enrollment.month = self.month
        enrollment.enrolled_by = enrolled_by
        if commit:
            enrollment.full_clean()
            enrollment.save()
            team = self.cleaned_data.get("team")
            if team:
                TeamAssignment.objects.create(month=self.month, participant=enrollment.participant, team=team)
        return enrollment


class MonthParticipantEditForm(forms.Form):
    team = forms.ModelChoiceField(queryset=Team.objects.none(), required=False, help_text="Leave blank to keep the participant enrolled without a team.")

    def __init__(self, *args, enrollment, **kwargs):
        super().__init__(*args, **kwargs)
        self.enrollment = enrollment
        self.fields["team"].queryset = enrollment.month.teams.filter(is_archived=False)
        assignment = TeamAssignment.objects.filter(month=enrollment.month, participant=enrollment.participant).first()
        self.fields["team"].initial = assignment.team_id if assignment else None

    def save(self):
        team = self.cleaned_data.get("team")
        assignment = TeamAssignment.objects.filter(month=self.enrollment.month, participant=self.enrollment.participant).first()
        previous_team = assignment.team if assignment else None
        if team:
            if assignment:
                assignment.team = team
                assignment.save()
            else:
                TeamAssignment.objects.create(month=self.enrollment.month, participant=self.enrollment.participant, team=team)
        elif assignment:
            assignment.delete()
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
