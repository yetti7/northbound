"""Optional local artwork, independent of competition scores and permissions."""
from io import BytesIO
import json
import warnings
from PIL import Image, ImageOps, UnidentifiedImageError
from django import forms
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.core.exceptions import PermissionDenied
from .models import ReadingGroup, ChallengeMonth, AuditEvent, Membership, Team, ChallengeStaffAssignment
from .permissions import membership_for, can_manage_months, can_operate_challenge, can_view_challenge, challenge_history_locked, is_challenge_host

BRANDING_MODE_LABELS = {"logo": "Logo Only", "banner_only": "Banner Only", "banner": "Logo and Banner"}

APPEARANCE_FIELDS = ["gradient_enabled", "gradient_coverage", "gradient_darkness"]

MAX_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 16_000_000


def _can_access_team_branding_settings(actor, team):
    if not actor.is_authenticated or not actor.is_active or not team.month.group.is_active:
        return False
    if not can_view_challenge(actor, team.month):
        return False
    if actor.is_superuser:
        return True
    member = membership_for(actor, team.month.group)
    if not member:
        return False
    if member.role in {Membership.Role.OWNER, Membership.Role.MODERATOR} or is_challenge_host(actor, team.month):
        return True
    return ChallengeStaffAssignment.objects.filter(month=team.month, team=team, membership=member,
        role=ChallengeStaffAssignment.Role.TEAM_LEADER, ended_at__isnull=True).exists()


def can_manage_team_branding(actor, team):
    return team.month.status in {'draft', 'upcoming', 'active'} and _can_access_team_branding_settings(actor, team)


def can_manage_branding(actor, target):
    if isinstance(target, Team):
        return can_manage_team_branding(actor, target)
    if not actor.is_authenticated or not actor.is_active:
        return False
    if isinstance(target, ReadingGroup):
        member = membership_for(actor, target)
        return actor.is_superuser or bool(member and member.role == Membership.Role.OWNER)
    return not challenge_history_locked(target) and (can_manage_months(actor, target.group) or can_operate_challenge(actor, target))


def can_view_branding_settings(actor, target):
    if isinstance(target, Team):
        return _can_access_team_branding_settings(actor, target)
    if can_manage_branding(actor, target):
        return True
    if isinstance(target, ReadingGroup) and actor.is_authenticated and actor.is_active:
        member = membership_for(actor, target)
        return bool(member and member.role == Membership.Role.MODERATOR)
    return False


def image_asset(upload, kind):
    if upload.size > MAX_BYTES:
        raise forms.ValidationError('Images must be 5 MB or smaller.')
    try:
        upload.seek(0)
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(upload) as original:
                if original.format not in {'PNG', 'JPEG', 'WEBP'}:
                    raise ValueError()
                if max(original.size) > 6000 or original.width * original.height > MAX_PIXELS:
                    raise ValueError()
                original.verify()
            upload.seek(0)
            with Image.open(upload) as original:
                image = ImageOps.exif_transpose(original).convert('RGBA')
                image.thumbnail({'logo': (800, 800), 'cover': (800, 1200)}.get(kind, (1600, 600)))
                output = BytesIO()
                image.save(output, format='PNG', optimize=True)
        return ContentFile(output.getvalue(), name='artwork.png')
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise forms.ValidationError('Choose a valid PNG, JPEG, or WebP image, at most 6000 pixels per side and 16 million pixels.') from None


class _ArtworkLabel:
    def __init__(self, url):
        self.url = url

    def __str__(self):
        return 'View current image'


class BrandingFileInput(forms.ClearableFileInput):
    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        if context['widget']['is_initial']:
            context['widget']['value'] = _ArtworkLabel(value.url)
        return context


class BrandingForm(forms.ModelForm):
    logo = forms.FileField(required=False, help_text='Recommended: 1000 × 1000 px. Transparent PNG preferred; keep the original logo proportions. PNG, JPEG, or WebP, up to 5 MB.', widget=BrandingFileInput(attrs={'accept':'image/png,image/jpeg,image/webp'}))
    banner = forms.FileField(required=False, help_text='Recommended: 1920 × 480 px (4:1). Keep important content near the center and away from edges; banners may crop on narrow screens. PNG, JPEG, or WebP, up to 5 MB. Images: at most 6000 px per side and 16 million pixels.', widget=BrandingFileInput(attrs={'accept':'image/png,image/jpeg,image/webp'}))

    gradient_enabled = forms.BooleanField(required=False, label='Use Gradient Overlay')
    gradient_coverage = forms.IntegerField(min_value=0, max_value=100, initial=75,
        label='Gradient Coverage', widget=forms.NumberInput(attrs={'type': 'range', 'step': 1, 'data-default': 75}))
    gradient_darkness = forms.IntegerField(min_value=0, max_value=100, initial=85,
        label='Darkness Intensity', widget=forms.NumberInput(attrs={'type': 'range', 'step': 1, 'data-default': 85}))

    class Meta:
        model = ReadingGroup
        fields = ['branding_mode', 'logo', 'banner', *APPEARANCE_FIELDS]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.is_bound:
            self.data = self.data.copy()
            if 'branding_mode' in self.fields:
                self.data.setdefault('branding_mode', self.instance.branding_mode)
            for name in ('gradient_coverage', 'gradient_darkness'):
                self.data.setdefault(name, getattr(self.instance, name))
        if 'branding_mode' in self.fields:
            self.fields['branding_mode'].choices = list(BRANDING_MODE_LABELS.items())

    def clean_gradient_coverage(self):
        value = self.cleaned_data['gradient_coverage']
        return self.instance.gradient_coverage if value is None else value

    def clean_gradient_darkness(self):
        value = self.cleaned_data['gradient_darkness']
        return self.instance.gradient_darkness if value is None else value

    def clean_logo(self):
        value = self.cleaned_data.get('logo')
        return image_asset(value, 'logo') if isinstance(value, UploadedFile) else value

    def clean_banner(self):
        value = self.cleaned_data.get('banner')
        return image_asset(value, 'banner') if isinstance(value, UploadedFile) else value


class ChallengeBrandingForm(BrandingForm):
    class Meta:
        model = ChallengeMonth
        fields = ['branding_mode', 'logo', 'banner', *APPEARANCE_FIELDS]


class TeamBrandingForm(BrandingForm):

    class Meta:
        model = Team
        fields = ['branding_mode', 'logo', 'banner', *APPEARANCE_FIELDS]


def branding_form_class(target):
    if isinstance(target, Team):
        return TeamBrandingForm
    return BrandingForm if isinstance(target, ReadingGroup) else ChallengeBrandingForm


def update_branding(*, actor, target, data, files):
    # Check again on the locked record, including current lifecycle and staff roles.
    with transaction.atomic():
        if isinstance(target, Team):
            # Ordinary Challenge writes lock the parent first, including lifecycle transitions.
            month_id = Team.objects.values_list('month_id', flat=True).get(pk=target.pk)
            month = ChallengeMonth.objects.select_for_update().get(pk=month_id)
            locked = Team.objects.select_for_update().get(pk=target.pk)
            if locked.month_id != month.pk:
                raise PermissionDenied('Team context changed. Reload the settings page.')
            locked.month = month
        else:
            locked = type(target).objects.select_for_update().get(pk=target.pk)
        if not can_manage_branding(actor, locked):
            raise PermissionDenied('Branding management is not available.')
        form = branding_form_class(locked)(data, files, instance=locked)
        if not form.is_valid():
            return form, False
        legacy = isinstance(locked, Team) and locked.branding_legacy_visibility
        if not form.has_changed() and not legacy:
            return form, False
        fields = form._meta.fields
        # is_valid constructs instance changes; read original values separately for audit.
        original = type(target).objects.get(pk=target.pk)
        before = {key: str(getattr(original, key)) for key in fields}
        updated = form.save(commit=False)
        if legacy:
            updated.branding_legacy_visibility = False
            fields = [*fields, "branding_legacy_visibility"]
            before["branding_legacy_visibility"] = "True"
        updated.save(update_fields=fields)
        group = updated.month.group if isinstance(updated, Team) else updated if isinstance(updated, ReadingGroup) else updated.group
        context = {'team_id': updated.pk, 'challenge_id': updated.month_id} if isinstance(updated, Team) else {}
        AuditEvent.objects.create(actor=actor, group=group,
            action='branding.updated', object_type=type(updated).__name__, object_id=str(updated.pk),
            summary='Updated optional community branding. '+json.dumps({**context, 'before':before,'after':{key:str(getattr(updated,key)) for key in fields}}))
        return form, True


def _asset(field):
    try:
        return field.url if field and field.storage.exists(field.name) else ''
    except (OSError, ValueError, NotImplementedError):
        return ''


def community_branding(viewer, group, month=None):
    if not viewer.is_authenticated or not viewer.is_active:
        return {}
    if not viewer.is_superuser and not membership_for(viewer, group):
        return {}
    if month and (month.group_id != group.pk or not can_view_challenge(viewer, month)):
        return {}
    mode = month.branding_mode if month else group.branding_mode
    result = {'group_name': group.name,
              'group_logo': _asset(group.logo) if mode != 'banner_only' else '',
              'group_banner': _asset(group.banner) if mode != 'logo' else '',
              'group_appearance': banner_appearance(group)}
    if month:
        result.update(challenge_name=month.name,
            challenge_logo=_asset(month.logo) if mode != 'banner_only' else '',
            challenge_banner=_asset(month.banner) if mode != 'logo' else '',
            challenge_appearance=banner_appearance(month))
    return result


def recap_branding(viewer, group_id=None, challenge_id=None):
    if challenge_id:
        month = ChallengeMonth.objects.select_related('group').filter(pk=challenge_id).first()
        return community_branding(viewer, month.group, month) if month else {}
    if group_id:
        group = ReadingGroup.objects.filter(pk=group_id).first()
        return community_branding(viewer, group) if group else {}
    return {}


def team_branding(viewer, team, *, presentation=True):
    """Read-only per-asset inheritance; no copying or competition permission changes."""
    return _team_branding_assets(team, community_branding(viewer, team.month.group, team.month), presentation=presentation)


def _team_branding_assets(team, community, *, presentation=True):
    result = dict(community)
    if not result:
        return {}
    logo = _asset(team.logo)
    inherited_logo = _asset(team.month.logo) or _asset(team.month.group.logo)
    legacy = team.branding_legacy_visibility and team.hide_inherited_logo
    result.update(team_name=team.name, team_logo=logo, team_banner=_asset(team.banner))
    result['effective_logo'] = logo or ('' if presentation and legacy else inherited_logo)
    result['effective_banner'] = result['team_banner'] or _asset(team.month.banner) or _asset(team.month.group.banner)
    if presentation:
        if team.branding_mode == 'banner_only': result['effective_logo'] = ''
        if team.branding_mode == 'logo': result['effective_banner'] = ''
        result['appearance'] = banner_appearance(team)
        result['showcase'] = bool(result['team_banner'] and result['effective_banner'] and not result['effective_logo'])
    return result


def team_branding_many(viewer, teams, *, presentation=True):
    """Reuse one authorized community context per Challenge for a Team listing."""
    communities = {}
    result = {}
    for team in teams:
        if team.month_id not in communities:
            communities[team.month_id] = community_branding(viewer, team.month.group, team.month)
        result[team.pk] = _team_branding_assets(team, communities[team.month_id], presentation=presentation)
    return result


def banner_appearance(target):
    """Preferences belong to the displayed entity, independently of artwork origin."""
    if not target.gradient_enabled:
        return {}
    # Integer model/form validation bounds these; clamp defensively for CSS output.
    coverage = max(0, min(100, int(target.gradient_coverage)))
    darkness = max(0, min(100, int(target.gradient_darkness)))
    return {'custom': True, 'style': f'--branding-coverage:{coverage}%;--branding-darkness:{darkness / 100:.2f}'}
