from django.conf import settings
from django.urls import NoReverseMatch, reverse

from .platform_config import get_platform_settings
from .review_attention import needs_attention_summary


def platform_configuration(request):
    platform_settings = get_platform_settings()
    return {
        "development_banner_enabled": settings.NORTHBOUND_DEVELOPMENT_BANNER,
        "platform_display_name": platform_settings.display_name,
        "public_registration_enabled": platform_settings.allow_public_registration,
        "user_group_creation_enabled": platform_settings.allow_user_group_creation,
        "logical_parent_label": logical_parent_label(request),
    }


def needs_attention(request):
    return {"needs_attention": needs_attention_summary(request.user)}


# Source route -> (existing destination route, destination kwargs from source kwargs).
# APIs, actions, roots and existing contextual returns are documented below.
NAVIGATION_PARENTS = {
 'photo-correct': ('month-detail', {'group_slug':'group_slug','pk':'month_pk'}),
 'photo-credit-action': ('month-detail', {'group_slug':'group_slug','pk':'month_pk'}),
 'photo-review': ('review-queue', {'group_slug':'group_slug','month_pk':'month_pk'}),
 'photo-create': ('month-detail', {'group_slug':'group_slug','pk':'month_pk'}),
 'photo-detail': ('my-stats', {}),
 'photo-edit': ('my-stats', {}),
'reader-rumble': ('team-detail', {'group_slug':'group_slug','month_pk':'month_pk','pk':'team_pk'}),
 'team-branding': ('team-detail', {'group_slug':'group_slug','month_pk':'month_pk','pk':'team_pk'}),
 'group-branding': ('group-edit', {'group_slug':'group_slug'}),
 'challenge-prompt-settings': ('challenge-settings', {'group_slug':'group_slug','month_pk':'month_pk'}),
 'account': ('dashboard', {}),
 'advanced-recovery': ('config-dashboard', {}),
 'botm-book-create': ('botm-list', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'botm-book-delete': ('botm-list', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'botm-book-edit': ('botm-list', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'botm-book-reactivate': ('botm-list', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'botm-book-retire': ('botm-list', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'botm-list': ('month-detail', {'group_slug': 'group_slug', 'pk': 'month_pk'}),
 'botm-match-list': ('botm-list', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'challenge-botm-settings': ('challenge-settings', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'challenge-branding': ('challenge-settings', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'challenge-floater-end': ('challenge-floater-list', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'challenge-floater-list': ('month-detail', {'group_slug': 'group_slug', 'pk': 'month_pk'}),
 'challenge-games-settings': ('challenge-settings', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'challenge-general-settings': ('challenge-settings', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'challenge-host-end': ('challenge-host-list', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'challenge-host-list': ('month-detail', {'group_slug': 'group_slug', 'pk': 'month_pk'}),
 'challenge-lifecycle-transition': ('challenge-settings', {'group_slug': 'group_slug', 'month_pk': 'pk'}),
 'challenge-participant-detail': ('month-participant-list',
                                  {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'challenge-progress-checkpoints': ('challenge-settings',
                                    {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'challenge-register': ('month-detail', {'group_slug': 'group_slug', 'pk': 'month_pk'}),
 'challenge-registration-detail': ('month-participant-list',
                                   {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'challenge-registration-edit': ('month-detail', {'group_slug': 'group_slug', 'pk': 'month_pk'}),
 'challenge-schedule-settings': ('challenge-settings', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'challenge-settings': ('month-detail', {'group_slug': 'group_slug', 'pk': 'month_pk'}),
 'challenge-signup-settings': ('challenge-settings', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'challenge-visibility-settings': ('challenge-settings',
                                   {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'challenge-withdraw': ('month-detail', {'group_slug': 'group_slug', 'pk': 'month_pk'}),
 'config-audit': ('config-dashboard', {}),
 'config-group-detail': ('config-group-list', {}),
 'config-group-list': ('config-dashboard', {}),
 'config-user-detail': ('config-user-list', {}),
 'config-user-edit': ('config-user-detail', {'pk': 'pk'}),
 'config-user-list': ('config-dashboard', {}),
 'config-user-password-reset': ('config-user-detail', {'pk': 'pk'}),
 'game-create': ('game-list', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'game-delete': ('game-detail', {'group_slug': 'group_slug', 'month_pk': 'month_pk', 'pk': 'pk'}),
 'game-detail': ('game-list', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'game-edit': ('game-detail', {'group_slug': 'group_slug', 'month_pk': 'month_pk', 'pk': 'pk'}),
 'game-list': ('month-detail', {'group_slug': 'group_slug', 'pk': 'month_pk'}),
 'game-reward-apply': ('game-detail', {'group_slug': 'group_slug', 'month_pk': 'month_pk', 'pk': 'pk'}),
 'game-reward-void': ('game-detail', {'group_slug': 'group_slug', 'month_pk': 'month_pk', 'pk': 'pk'}),
 'group-access-code': ('group-edit', {'group_slug': 'group_slug'}),
 'group-create': ('dashboard', {}),
 'group-detail': ('dashboard', {}),
 'group-discord-notifications': ('group-edit', {'group_slug': 'group_slug'}),
 'group-edit': ('group-detail', {'group_slug': 'group_slug'}),
 'group-hardcover-connection': ('group-edit', {'group_slug': 'group_slug'}),
 'group-join': ('dashboard', {}),
 'immersive-settings': ('challenge-settings', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'member-create': ('group-detail', {'group_slug': 'group_slug'}),
 'month-create': ('month-list', {'group_slug': 'group_slug'}),
 'month-delete': ('month-detail', {'group_slug': 'group_slug', 'pk': 'pk'}),
 'month-detail': ('month-list', {'group_slug': 'group_slug'}),
 'month-edit': ('challenge-settings', {'group_slug': 'group_slug', 'month_pk': 'pk'}),
 'month-announcement-update': ('challenge-settings', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'month-list': ('group-detail', {'group_slug': 'group_slug'}),
 'month-participant-add': ('month-participant-list', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'month-participant-edit': ('month-participant-list', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'month-participant-list': ('month-detail', {'group_slug': 'group_slug', 'pk': 'month_pk'}),
 'month-participant-reactivate': ('month-participant-list',
                                  {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'month-participant-remove': ('month-participant-list', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'my-stats': ('dashboard', {}),
 'needs-attention': ('dashboard', {}),
 'participant-deactivate': ('participant-list', {'group_slug': 'group_slug'}),
 'participant-detail': ('participant-list', {'group_slug': 'group_slug'}),
 'participant-list': ('group-detail', {'group_slug': 'group_slug'}),
 'participant-permissions-edit': ('participant-list', {'group_slug': 'group_slug'}),
 'participant-role-edit': ('participant-list', {'group_slug': 'group_slug'}),
 'personal-tbr-match-list': ('review-queue', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'platform-audit-prune': ('platform-storage-maintenance', {}),
 'platform-backup-restore': ('platform-backups', {}),
 'platform-backups': ('config-dashboard', {}),
 'platform-cache-cleanup': ('platform-storage-maintenance', {}),
 'platform-general-settings': ('config-dashboard', {}),
 'platform-hardcover-oauth': ('config-dashboard', {}),
 'platform-owner-create': ('platform-owner-list', {}),
 'platform-owner-list': ('config-dashboard', {}),
 'platform-owner-status-toggle': ('platform-owner-list', {}),
 'platform-restore-restart': ('platform-backups', {}),
 'platform-settings': ('config-dashboard', {}),
 'platform-sqlite-optimize': ('platform-storage-maintenance', {}),
 'platform-storage-maintenance': ('config-dashboard', {}),
 'platform-system-status': ('config-dashboard', {}),
 'recovery-account-list': ('advanced-recovery', {}),
 'recovery-account-status': ('recovery-account-list', {}),
 'recovery-botm-book-correct': ('recovery-botm-list', {}),
 'recovery-botm-book-detail': ('recovery-botm-list', {}),
 'recovery-botm-book-status': ('recovery-botm-list', {}),
 'recovery-botm-list': ('advanced-recovery', {}),
 'recovery-botm-match': ('recovery-botm-list', {}),
 'recovery-botm-match-purge': ('recovery-botm-list', {}),
 'recovery-challenge-detail': ('recovery-challenge-list', {}),
 'recovery-challenge-list': ('advanced-recovery', {}),
 'recovery-challenge-purge': ('recovery-challenge-list', {}),
 'recovery-checkpoint-detail': ('recovery-checkpoint-list', {}),
 'recovery-checkpoint-list': ('advanced-recovery', {}),
 'recovery-checkpoint-release': ('recovery-checkpoint-list', {}),
 'recovery-checkpoint-reset': ('recovery-checkpoint-list', {}),
 'recovery-enrollment-origin': ('recovery-challenge-list', {}),
 'recovery-enrollment-status': ('recovery-challenge-list', {}),
 'recovery-game-application': ('recovery-game-list', {}),
 'recovery-game-detail': ('recovery-game-list', {}),
 'recovery-game-list': ('advanced-recovery', {}),
 'recovery-game-recreate': ('recovery-game-list', {}),
 'recovery-game-status': ('recovery-game-list', {}),
 'recovery-group-detail': ('recovery-group-list', {}),
 'recovery-group-hardcover-clear': ('recovery-hardcover-list', {}),
 'recovery-group-list': ('advanced-recovery', {}),
 'recovery-group-status': ('recovery-group-list', {}),
 'recovery-group-transfer-owner': ('recovery-group-list', {}),
 'recovery-hardcover-list': ('advanced-recovery', {}),
 'recovery-history': ('advanced-recovery', {}),
 'recovery-host-scope-restore': ('recovery-challenge-list', {}),
 'recovery-membership-role': ('recovery-group-list', {}),
 'recovery-membership-status': ('recovery-group-list', {}),
 'recovery-provenance-detail': ('recovery-provenance-list', {}),
 'recovery-provenance-list': ('advanced-recovery', {}),
 'recovery-provenance-purge': ('recovery-provenance-list', {}),
 'recovery-provenance-rebuild': ('recovery-provenance-list', {}),
 'recovery-provenance-void': ('recovery-provenance-list', {}),
 'recovery-reader-hardcover-clear': ('recovery-hardcover-list', {}),
 'recovery-staffing-role': ('recovery-challenge-list', {}),
 'recovery-staffing-status': ('recovery-challenge-list', {}),
 'recovery-submission-detail': ('recovery-submission-list', {}),
 'recovery-submission-list': ('advanced-recovery', {}),
 'recovery-submission-purge': ('recovery-submission-list', {}),
 'recovery-submission-status': ('recovery-submission-list', {}),
 'recovery-tbr-detail': ('recovery-tbr-list', {}),
 'recovery-tbr-title': ('recovery-tbr-list', {}),
 'recovery-tbr-entry': ('recovery-tbr-list', {}),
 'recovery-tbr-list': ('advanced-recovery', {}),
 'recovery-tbr-match': ('recovery-tbr-list', {}),
 'recovery-tbr-match-purge': ('recovery-tbr-list', {}),
 'recovery-tbr-rebuild': ('recovery-tbr-list', {}),
 'recovery-team-assignment-reassign': ('recovery-challenge-list', {}),
 'recovery-team-assignment-status': ('recovery-challenge-list', {}),
 'recovery-team-delete': ('recovery-challenge-list', {}),
 'recovery-team-status': ('recovery-challenge-list', {}),
 'recovery-theme-claim': ('recovery-submission-list', {}),
 'recovery-theme-correct': ('recovery-theme-list', {}),
 'recovery-theme-detail': ('recovery-theme-list', {}),
 'recovery-theme-list': ('advanced-recovery', {}),
 'recovery-theme-status': ('recovery-theme-list', {}),
 'review-queue': ('month-detail', {'group_slug': 'group_slug', 'pk': 'month_pk'}),
 'staff-challenge-tbr': ('challenge-participant-detail',
                         {'group_slug': 'group_slug', 'month_pk': 'month_pk', 'pk': 'pk'}),
 'stored-backup-restore': ('platform-backups', {}),
 'submission-create': ('month-detail', {'group_slug': 'group_slug', 'pk': 'month_pk'}),
 'submission-remove': ('month-detail', {'group_slug': 'group_slug', 'pk': 'month_pk'}),
 'submission-review': ('review-queue', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'team-archive-toggle': ('team-list', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'team-assignment-create': ('team-list', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'team-assignment-remove': ('team-list', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'team-create': ('team-list', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'team-delete': ('team-edit', {'group_slug': 'group_slug', 'month_pk': 'month_pk', 'pk': 'pk'}),
 'team-detail': ('team-list', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'team-edit': ('team-list', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'team-leader-end': ('team-leader-list',
                     {'group_slug': 'group_slug', 'month_pk': 'month_pk', 'team_pk': 'team_pk'}),
 'team-leader-list': ('team-list', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'team-list': ('month-detail', {'group_slug': 'group_slug', 'pk': 'month_pk'}),
 'team-stats-settings': ('month-detail', {'group_slug': 'group_slug', 'pk': 'month_pk'}),
 'theme-create': ('theme-list', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'theme-edit': ('theme-list', {'group_slug': 'group_slug', 'month_pk': 'month_pk'}),
 'theme-list': ('month-detail', {'group_slug': 'group_slug', 'pk': 'month_pk'})}

PARENT_LABELS = {'my-stats': 'Back to My Stats',
'team-detail': 'Back to Team',
 'account': 'Back to My Account',
 'advanced-recovery': 'Back to Advanced Recovery',
 'botm-list': 'Back to Book of the Month',
 'challenge-floater-list': 'Back to Challenge Staff',
 'challenge-host-list': 'Back to Challenge Staff',
 'challenge-participant-detail': 'Back to Challenge Participant',
 'challenge-settings': 'Back to Challenge Settings',
 'config-dashboard': 'Back to Platform Administration',
 'config-group-detail': 'Back to Group Overview',
 'config-group-list': 'Back to Groups',
 'config-user-detail': 'Back to Account',
 'config-user-list': 'Back to Accounts',
 'dashboard': 'Back to Home',
 'game-detail': 'Back to Game',
 'game-list': 'Back to Games',
 'group-detail': 'Back to Group',
 'group-edit': 'Back to Group Settings',
 'month-detail': 'Back to Challenge',
 'month-list': 'Back to Challenges',
 'month-participant-list': 'Back to Challenge Participants',
 'participant-list': 'Back to Members',
 'platform-backups': 'Back to Backups',
 'platform-owner-list': 'Back to Platform Owners',
 'platform-storage-maintenance': 'Back to Storage & Maintenance',
 'recovery-account-detail': 'Back to Account Recovery',
 'recovery-account-list': 'Back to Account Recovery',
 'recovery-botm-book-detail': 'Back to BOTM Recovery',
 'recovery-botm-detail': 'Back to BOTM Recovery',
 'recovery-botm-list': 'Back to BOTM Recovery',
 'recovery-challenge-detail': 'Back to Challenge Recovery',
 'recovery-challenge-list': 'Back to Challenge Recovery',
 'recovery-checkpoint-detail': 'Back to Checkpoint Recovery',
 'recovery-checkpoint-list': 'Back to Checkpoint Recovery',
 'recovery-game-detail': 'Back to Game Reward Recovery',
 'recovery-game-list': 'Back to Game Reward Recovery',
 'recovery-group-detail': 'Back to Group Recovery',
 'recovery-group-list': 'Back to Group Recovery',
 'recovery-hardcover-detail': 'Back to Hardcover Credential Recovery',
 'recovery-hardcover-list': 'Back to Hardcover Credential Recovery',
 'recovery-provenance-detail': 'Back to Modifier Recovery',
 'recovery-provenance-list': 'Back to Modifier Recovery',
 'recovery-submission-detail': 'Back to Submission Recovery',
 'recovery-submission-list': 'Back to Submission Recovery',
 'recovery-tbr-detail': 'Back to Personal TBR Recovery',
 'recovery-tbr-list': 'Back to Personal TBR Recovery',
 'recovery-theme-detail': 'Back to Theme Recovery',
 'recovery-theme-list': 'Back to Theme Recovery',
 'review-queue': 'Back to Review Queue',
 'team-edit': 'Back to Edit Team',
 'team-leader-list': 'Back to Team Leaders',
 'team-list': 'Back to Teams',
 'theme-list': 'Back to Themes'}

NAVIGATION_EXCEPTIONS = {'photo-withdraw': 'POST-only action endpoint; no application return control.',
'botm-catalog': 'JSON/API endpoint; no application return control.',
 'botm-match-review': 'Action endpoint, not a secondary navigation screen.',
 'botm-match-synchronize': 'Action endpoint, not a secondary navigation screen.',
 'botm-metadata-refresh': 'Action endpoint, not a secondary navigation screen.',
 'challenge-register-confirm': 'Existing contextual local return; avoid a duplicate global link.',
 'challenge-registration-action': 'Action endpoint, not a secondary navigation screen.',
 'config-audit-export': 'Download response; no page return control.',
 'config-dashboard': 'Primary dashboard.',
 'config-group-status-toggle': 'Action endpoint, not a secondary navigation screen.',
 'config-login': 'Authentication/setup/OAuth flow; retain its existing flow navigation.',
 'config-user-status-toggle': 'Action endpoint, not a secondary navigation screen.',
 'dashboard': 'Primary dashboard.',
 'game-active-toggle': 'Action endpoint, not a secondary navigation screen.',
 'global-search': 'JSON/API endpoint; no application return control.',
 'group-announcement-update': 'Action endpoint, not a secondary navigation screen.',
 'group-hardcover-disconnect': 'Action endpoint, not a secondary navigation screen.',
 'hardcover-test-token': 'JSON/API endpoint; no application return control.',
 'health': 'JSON/API endpoint; no application return control.',
 'host-assignment-notice-open': 'Action endpoint, not a secondary navigation screen.',
 'immersive-action': 'Existing contextual local return; avoid a duplicate global link.',
 'personal-tbr-catalog': 'JSON/API endpoint; no application return control.',
 'personal-tbr-detail': 'Existing contextual local return; avoid a duplicate global link.',
 'personal-tbr-match-review': 'Action endpoint, not a secondary navigation screen.',
 'personal-tbr-match-synchronize': 'Action endpoint, not a secondary navigation screen.',
 'platform-backup-create': 'Action endpoint, not a secondary navigation screen.',
 'platform-owner-accept': 'Authentication/setup/OAuth flow; retain its existing flow navigation.',
 'platform-owner-invitation-revoke': 'Action endpoint, not a secondary navigation screen.',
 'platform-submission-activity': 'Phase 2 contextual origin with an authorized logical fallback.',
 'prompt-progress': 'Existing contextual local return; avoid a duplicate global link.',
 'prompt-progress-report': 'Existing contextual local return; avoid a duplicate global link.',
 'reader-hardcover-connect': 'Authentication/setup/OAuth flow; retain its existing flow navigation.',
 'reader-hardcover-disconnect': 'Action endpoint, not a secondary navigation screen.',
 'reader-hardcover-oauth-callback': 'Authentication/setup/OAuth flow; retain its existing flow navigation.',
 'reader-hardcover-oauth-start': 'Authentication/setup/OAuth flow; retain its existing flow navigation.',
 'reader-hardcover-sync-now': 'Action endpoint, not a secondary navigation screen.',
 'reader-hardcover-sync-preferences': 'Action endpoint, not a secondary navigation screen.',
 'reader-hardcover-test': 'Action endpoint, not a secondary navigation screen.',
 'recap-dashboard': 'Validated recap context supplies the canonical parent.',
 'recap-wrapped': 'Existing contextual local return; avoid a duplicate global link.',
 'register': 'Authentication/setup/OAuth flow; retain its existing flow navigation.',
 'setup': 'Authentication/setup/OAuth flow; retain its existing flow navigation.',
 'staff-challenge-tbr-book-edit': 'Existing contextual local return; avoid a duplicate global link.',
 'staff-challenge-tbr-catalog': 'JSON/API endpoint; no application return control.',
 'stored-backup-delete': 'Action endpoint, not a secondary navigation screen.',
 'stored-backup-download': 'Download response; no page return control.',
 'submission-activity': 'Phase 2 contextual origin with an authorized logical fallback.',
 'submission-catalog': 'JSON/API endpoint; no application return control.',
 'submission-credit': 'Existing contextual local return; avoid a duplicate global link.',
 'submission-edit': 'Phase 2 contextual origin with an authorized logical fallback.',
 'submission-edit-catalog': 'JSON/API endpoint; no application return control.',
 'submission-editor': 'Phase 2 contextual origin with an authorized logical fallback.',
 'submission-fix-book': 'Phase 2 contextual origin with an authorized logical fallback.',
 'submission-fix-pages': 'Phase 2 contextual origin with an authorized logical fallback.',
 'submission-history': 'Phase 2 contextual origin with an authorized logical fallback.',
 'submission-immersive': 'Existing contextual local return; avoid a duplicate global link.',
 'submission-manage': 'Phase 2 contextual origin with an authorized logical fallback.',
 'tbr-metadata-refresh': 'Action endpoint, not a secondary navigation screen.',
 'team-book-detail': 'Existing contextual local return; avoid a duplicate global link.',
 'submission-restore': 'Phase 2 contextual origin with an authorized logical fallback.'}


def _authorized_parent(request, name, kwargs):
    """Navigation never grants access; use the existing destination boundaries."""
    from .models import ReadingGroup, ChallengeMonth, Team, MonthEnrollment
    from .permissions import (membership_for, can_view_challenge, can_manage_group,
        can_operate_challenge, can_manage_months, can_review_challenge,
        can_run_challenge_wide_operations, can_manage_challenge_tbr)
    user = request.user
    if not user.is_authenticated or not user.is_active:
        return False
    if name.startswith(('config-', 'platform-', 'recovery-')) or name == 'advanced-recovery':
        return user.is_superuser
    if name in {'dashboard', 'account', 'my-stats'}:
        return True
    slug = kwargs.get('group_slug')
    if not slug:
        return False
    group = ReadingGroup.objects.filter(slug=slug, is_active=True).first()
    if group is None or not (user.is_superuser or membership_for(user, group)):
        return False
    if name == 'group-edit':
        from .permissions import can_view_group_settings
        return can_view_group_settings(user, group)
    if name in {'group-detail', 'month-list', 'participant-list'}:
        return True
    month_id = kwargs.get('month_pk', kwargs.get('pk') if name == 'month-detail' else None)
    month = ChallengeMonth.objects.select_related('group').filter(pk=month_id, group=group).first()
    if month is None or not can_view_challenge(user, month):
        return False
    if name == 'challenge-settings':
        return can_manage_months(user, group) or can_operate_challenge(user, month)
    if name == 'review-queue':
        member = membership_for(user, group)
        return (month.status in {'active','finalizing','completed'} and
            (can_review_challenge(user, month) or bool(member and member.role == 'owner')))
    if name == 'team-edit':
        return (can_run_challenge_wide_operations(user, month)
            and month.status not in {'completed', 'archived'}
            and Team.objects.filter(pk=kwargs['pk'], month=month).exists())
    if name == 'challenge-participant-detail':
        enrollment = MonthEnrollment.objects.filter(month=month, participant_id=kwargs['pk']).first()
        return bool(enrollment and can_manage_challenge_tbr(user, enrollment))
    return True


def _parent(request, name, kwargs=None):
    kwargs = kwargs or {}
    if name not in PARENT_LABELS or not _authorized_parent(request, name, kwargs):
        return None
    try:
        return {'logical_parent_url': reverse(name, kwargs=kwargs or None),
                'logical_parent_label': PARENT_LABELS[name]}
    except NoReverseMatch:
        return None


def _specialized_recovery_parent(request):
    """Derive parents from persisted objects, never user-supplied return URLs."""
    from . import models
    if not (request.user.is_authenticated and request.user.is_active and request.user.is_superuser):
        return None
    rules = {
        'recovery-theme-correct': ('MonthTheme', '', 'recovery-theme-detail'),
        'recovery-theme-claim': ('ThemeClaim', 'submission_id', 'recovery-submission-detail'),
        'recovery-botm-book-correct': ('BotmBook', '', 'recovery-botm-book-detail'),
        'recovery-botm-match-purge': ('BotmMatch', 'botm_book_id', 'recovery-botm-book-detail'),
        'recovery-botm-match': ('BotmMatch', 'botm_book_id', 'recovery-botm-book-detail'),
        'recovery-tbr-title': ('PersonalTBRBook', 'personal_tbr_id', 'recovery-tbr-detail'),
        'recovery-tbr-entry': ('PersonalTBRBook', 'personal_tbr_id', 'recovery-tbr-detail'),
        'recovery-tbr-rebuild': ('PersonalTBR', '', 'recovery-tbr-detail'),
        'recovery-tbr-match-purge': ('PersonalTBRMatch', 'personal_tbr_book__personal_tbr_id', 'recovery-tbr-detail'),
        'recovery-tbr-match': ('PersonalTBRMatch', 'personal_tbr_book__personal_tbr_id', 'recovery-tbr-detail'),
        'recovery-game-recreate': ('GameRewardApplication', 'game_id', 'recovery-game-detail'),
    }
    rule = rules.get(request.resolver_match.view_name)
    if rule is None:
        return None
    model, field, target = rule
    pk = getattr(models, model).objects.filter(pk=request.resolver_match.kwargs.get('pk')).values_list(field or 'pk', flat=True).first()
    if pk is None:
        return None
    # Verify the detail target still exists, as for trusted confirmation hints.
    detail_models = {'recovery-theme-detail': models.MonthTheme,
        'recovery-submission-detail': models.BookSubmission,
        'recovery-botm-book-detail': models.BotmBook,
        'recovery-tbr-detail': models.PersonalTBR,
        'recovery-game-detail': models.Game}
    if not detail_models[target].objects.filter(pk=pk).exists():
        return None
    return _parent(request, target, {'pk': pk})


def _recovery_parent(request):
    """Only the shared Recovery view supplies this URL, never GET/POST/Referer."""
    from urllib.parse import urlsplit
    from django.urls import resolve, Resolver404
    from . import models
    candidate = getattr(request, '_recovery_navigation_parent', '')
    if not candidate or not (request.user.is_authenticated and request.user.is_active and request.user.is_superuser):
        return None
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith('/') or parsed.path.startswith('//'):
        return None
    try:
        match = resolve(parsed.path)
    except Resolver404:
        return None
    objects = {'recovery-group-detail': models.ReadingGroup,
               'recovery-challenge-detail': models.ChallengeMonth,
               'recovery-submission-detail': models.BookSubmission,
               'recovery-theme-detail': models.MonthTheme,
               'recovery-provenance-detail': models.ModifierProvenance,
               'recovery-botm-book-detail': models.BotmBook,
               'recovery-tbr-detail': models.PersonalTBR,
               'recovery-game-detail': models.Game,
               'recovery-checkpoint-detail': models.ProgressCheckpoint}
    name = match.view_name
    model = objects.get(name)
    if model is not None and not model.objects.filter(pk=match.kwargs.get('pk')).exists():
        return None
    if model is None and not (name.startswith('recovery-') and name.endswith('-list')):
        return None
    return _parent(request, name, match.kwargs)


def _resolve_logical_parent(request):
    match = request.resolver_match
    if not match:
        return None
    from .navigation_context import contextual_parent
    contextual = contextual_parent(request)
    if contextual:
        return contextual
    name, kwargs = match.view_name, match.kwargs
    if name == 'recap-dashboard':
        # Only _recap_context supplies these validated IDs after authorization.
        context = getattr(request, '_validated_recap_navigation', {})
        from .models import ChallengeMonth, ReadingGroup
        month = ChallengeMonth.objects.select_related('group').filter(pk=context.get('challenge')).first() if context.get('challenge') else None
        if month:
            parent = _parent(request, 'month-detail', {'group_slug':month.group.slug, 'pk':month.pk})
            if parent:
                return parent
        group = ReadingGroup.objects.filter(pk=context.get('group')).first() if context.get('group') else None
        if group:
            parent = _parent(request, 'group-detail', {'group_slug':group.slug})
            if parent:
                return parent
        return _parent(request, 'config-dashboard' if request.user.is_superuser else 'dashboard')
    if name.startswith('recovery-'):
        parent = _specialized_recovery_parent(request) or _recovery_parent(request)
        if parent:
            return parent
    from .navigation_context import SCREENS
    if name in SCREENS and name not in NAVIGATION_PARENTS and name not in {'recap-wrapped','prompt-progress','team-book-detail','submission-credit','submission-immersive'}:
        return _parent(request, 'month-detail', {'group_slug':kwargs['group_slug'], 'pk':kwargs['month_pk']}) if kwargs.get('month_pk') else None
    if name == 'team-book-detail':
        from .navigation_context import authorized
        args={k:v for k,v in kwargs.items() if k != 'book_pk'}
        if authorized(request,'team-detail',args,{}):
            return {'logical_parent_url':reverse('team-detail',kwargs=args)+'?view=books', 'logical_parent_label':'Back to Team Books'}
    spec = NAVIGATION_PARENTS.get(name)
    if spec is None:
        return None
    target, argument_names = spec
    if name == 'account' and request.user.is_superuser:
        target = 'config-dashboard'
    try:
        parent_kwargs = {key:kwargs[source] for key,source in argument_names.items()}
    except KeyError:
        return None
    parent = _parent(request, target, parent_kwargs)
    if parent is None and name in {'group-branding','group-discord-notifications','group-access-code','group-hardcover-connection'}:
        return _parent(request, 'group-detail', {'group_slug':kwargs['group_slug']})
    return parent


def logical_navigation(request):
    """Resolve the public URL and label context keys atomically, once per request."""
    if not hasattr(request, '_logical_navigation_result'):
        request._logical_navigation_result = (_resolve_logical_parent(request) or
            {'logical_parent_url':None, 'logical_parent_label':''})
    return request._logical_navigation_result.copy()


def logical_parent_label(request):
    """Compatibility wrapper; labels always come from the resolved destination."""
    return logical_navigation(request)['logical_parent_label']
