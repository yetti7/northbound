from django import forms

from .models import (
    BotmBook, CatalogBook, CatalogEdition, ChallengeStaffAssignment, Membership,
    MonthEnrollment, MonthTheme, PersonalTBRBook, Team, ThemeClaim,
)
from .recovery import RECOVERY_REASON_MAX_LENGTH


class RecoveryConfirmationForm(forms.Form):
    reason = forms.CharField(
        max_length=RECOVERY_REASON_MAX_LENGTH,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Required. Explain why installation-level recovery is necessary.",
    )
    confirmation = forms.CharField(max_length=300)
    current_password = forms.CharField(
        required=False,
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )

    def __init__(self, *args, expected_confirmation, require_password=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.expected_confirmation = expected_confirmation
        self.fields["confirmation"].label = f'Type "{expected_confirmation}" exactly to confirm'
        if not require_password:
            self.fields.pop("current_password")


class SafeDeleteConfirmationForm(forms.Form):
    confirmation = forms.CharField(max_length=300)

    def __init__(self, *args, expected_confirmation, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["confirmation"].label = f'Type "{expected_confirmation}" exactly to confirm'


class OwnershipTransferForm(RecoveryConfirmationForm):
    target_membership = forms.ModelChoiceField(queryset=Membership.objects.none(), label="New Group Owner")

    def __init__(self, *args, group, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["target_membership"].queryset = Membership.objects.filter(
            group=group, is_active=True, user__is_superuser=False,
        ).select_related("user").order_by("display_name")


class MembershipRoleRecoveryForm(RecoveryConfirmationForm):
    role = forms.ChoiceField(choices=Membership.Role.choices)


class EnrollmentOriginRecoveryForm(RecoveryConfirmationForm):
    origin = forms.ChoiceField(choices=MonthEnrollment.Origin.choices)


class StaffingRoleRecoveryForm(RecoveryConfirmationForm):
    role = forms.ChoiceField(choices=ChallengeStaffAssignment.Role.choices)
    team = forms.ModelChoiceField(queryset=Team.objects.none(), required=False)

    def __init__(self, *args, month, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["team"].queryset = month.teams.filter(is_archived=False).order_by("name")


class TeamReassignmentRecoveryForm(RecoveryConfirmationForm):
    team = forms.ModelChoiceField(queryset=Team.objects.none())

    def __init__(self, *args, month, current_team, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["team"].queryset = month.teams.filter(is_archived=False).exclude(pk=current_team.pk).order_by("name")


class ThemeClaimRecoveryForm(RecoveryConfirmationForm):
    status = forms.ChoiceField(choices=ThemeClaim.Status.choices)
    rebuild_provenance = forms.BooleanField(
        required=False,
        help_text="Void and recreate the canonical Theme provenance from the claim's frozen approved amount.",
    )


class UnusedThemeRecoveryForm(RecoveryConfirmationForm):
    name = forms.CharField(max_length=120)
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    starts_on = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    ends_on = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    bonus_pages = forms.IntegerField(min_value=0)
    allow_stacking = forms.BooleanField(required=False)
    prompt = forms.CharField(max_length=300, required=False)
    is_visible = forms.BooleanField(required=False)

    def __init__(self, *args, theme: MonthTheme, **kwargs):
        if (not args or args[0] is None) and not kwargs.get("data"):
            kwargs["initial"] = {
                **kwargs.get("initial", {}),
                "name": theme.name,
                "description": theme.description,
                "starts_on": theme.starts_on,
                "ends_on": theme.ends_on,
                "bonus_pages": theme.bonus_pages,
                "allow_stacking": theme.allow_stacking,
                "prompt": theme.prompt,
                "is_visible": theme.is_visible,
            }
        super().__init__(*args, **kwargs)


class BotmMatchRecoveryForm(RecoveryConfirmationForm):
    decision = forms.ChoiceField(choices=(
        ("pending", "Reopen for review"), ("confirmed", "Confirm"), ("rejected", "Reject"),
    ))
    target_book = forms.ModelChoiceField(queryset=BotmBook.objects.none(), label="Correct BOTM Book")

    def __init__(self, *args, month, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["target_book"].queryset = BotmBook.objects.filter(
            month=month, is_retired=False,
        ).order_by("position", "pk")


class TbrMatchRecoveryForm(RecoveryConfirmationForm):
    decision = forms.ChoiceField(choices=(
        ("pending", "Reopen for review"), ("confirmed", "Confirm"), ("rejected", "Reject"),
    ))
    target_book = forms.ModelChoiceField(queryset=PersonalTBRBook.objects.none(), label="Correct Personal TBR Book")

    def __init__(self, *args, personal_tbr, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["target_book"].queryset = personal_tbr.books.order_by("position", "pk")


class RecoveryBookIdentityForm(RecoveryConfirmationForm):
    catalog_book = forms.ModelChoiceField(queryset=CatalogBook.objects.all(), required=False)
    catalog_edition = forms.ModelChoiceField(queryset=CatalogEdition.objects.all(), required=False)
    title_snapshot = forms.CharField(max_length=300, label="Title")
    author_snapshot = forms.CharField(max_length=300, label="Author")
    page_count_snapshot = forms.IntegerField(min_value=1, required=False, label="Page Count")
    cover_url_snapshot = forms.URLField(max_length=1000, required=False, label="Cover URL")
    source_url_snapshot = forms.URLField(max_length=1000, required=False, label="Source URL")

    def clean(self):
        cleaned = super().clean()
        catalog_book = cleaned.get("catalog_book")
        catalog_edition = cleaned.get("catalog_edition")
        if catalog_edition and (not catalog_book or catalog_edition.book_id != catalog_book.pk):
            self.add_error("catalog_edition", "The catalog edition must belong to the selected catalog book.")
        return cleaned


class LockedTbrListBookForm(forms.Form):
    include = forms.BooleanField(required=False)
    position = forms.IntegerField(min_value=1, max_value=9)
    catalog_book = forms.ModelChoiceField(queryset=CatalogBook.objects.all(), required=False)
    catalog_edition = forms.ModelChoiceField(queryset=CatalogEdition.objects.all(), required=False)
    title_snapshot = forms.CharField(max_length=300, required=False, label="Title")
    author_snapshot = forms.CharField(max_length=300, required=False, label="Author")
    page_count_snapshot = forms.IntegerField(min_value=1, required=False, label="Page Count")
    cover_url_snapshot = forms.URLField(max_length=1000, required=False, label="Cover URL")
    source_url_snapshot = forms.URLField(max_length=1000, required=False, label="Source URL")

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("include"):
            return cleaned
        for field in ("title_snapshot", "author_snapshot"):
            if not cleaned.get(field):
                self.add_error(field, "This field is required for an included book.")
        edition = cleaned.get("catalog_edition")
        book = cleaned.get("catalog_book")
        if edition and (not book or edition.book_id != book.pk):
            self.add_error("catalog_edition", "The catalog edition must belong to the selected catalog book.")
        return cleaned


LockedTbrListBookFormSet = forms.formset_factory(LockedTbrListBookForm, extra=9, max_num=9, validate_max=True)


class GameReplacementRecoveryForm(RecoveryConfirmationForm):
    amount = forms.IntegerField(min_value=1, label="Correct Replacement Amount")
