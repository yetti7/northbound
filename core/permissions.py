from django.db.models import Q

from .models import ChallengeStaffAssignment, Membership, MonthEnrollment, TeamAssignment


CAPABILITIES = {
    "manage_group_settings": "Manage Group settings, access code, and integrations",
    "manage_participants": "Manage permanent Group members",
    "manage_months": "Create Challenges, manage Draft Challenges, and assign Hosts",
    "manage_announcements": "Manage Group announcements",
    "remove_content": "Legacy compatibility: retired content authority",
    "view_hidden_stats": "Legacy compatibility: retired score-visibility authority",
    "manage_permissions": "Manage Group roles and permission delegation",
}

INTERNAL_COMPATIBILITY_CAPABILITIES = {"remove_content", "view_hidden_stats"}

# Preserve restored/historical override keys without exposing them as a second
# human-facing path to Challenge competition visibility.
DELEGABLE_CAPABILITIES = {
    capability: label
    for capability, label in CAPABILITIES.items()
    if capability not in INTERNAL_COMPATIBILITY_CAPABILITIES
}

ROLE_CAPABILITIES = {
    Membership.Role.OWNER: set(CAPABILITIES),
    Membership.Role.MODERATOR: {"manage_announcements"},
    Membership.Role.MEMBER: set(),
}

STAFF_ROLES = {Membership.Role.OWNER, Membership.Role.MODERATOR}


def membership_for(user, group):
    if not user.is_authenticated:
        return None
    return Membership.objects.filter(user=user, group=group, is_active=True).first()


def membership_has_capability(membership, capability):
    if not membership or not membership.is_active or capability not in CAPABILITIES:
        return False
    override = membership.permission_overrides.get(capability)
    if override is not None:
        return bool(override)
    return capability in ROLE_CAPABILITIES.get(membership.role, set())


def has_capability(user, group, capability):
    if user.is_superuser:
        return True
    return membership_has_capability(membership_for(user, group), capability)


def can_manage_group(user, group):
    return has_capability(user, group, "manage_group_settings")


def can_manage_participants(user, group):
    return has_capability(user, group, "manage_participants")


def can_manage_months(user, group):
    return has_capability(user, group, "manage_months")


def can_manage_challenge_hosts(user, group):
    if user.is_superuser:
        return True
    return membership_has_capability(membership_for(user, group), "manage_months")


def is_challenge_host(user, month):
    membership = membership_for(user, month.group)
    if not membership or user.is_superuser:
        return False
    return ChallengeStaffAssignment.objects.filter(
        month=month,
        membership=membership,
        role=ChallengeStaffAssignment.Role.HOST,
        ended_at__isnull=True,
    ).exists()


def can_operate_challenge(user, month):
    """Allow active Hosts or the installation-level Platform Owner override."""
    return user.is_authenticated and (user.is_superuser or is_challenge_host(user, month))


def can_view_challenge(user, month):
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    membership = membership_for(user, month.group)
    if not membership:
        return False
    if month.status != month.Status.DRAFT:
        return True
    if membership.role == Membership.Role.OWNER:
        return True
    return membership_has_capability(membership, "manage_months") or is_challenge_host(user, month)


def can_transition_challenge(user, month):
    """Lifecycle authority without conflating Group authority with Host operations."""
    return can_manage_months(user, month.group) or can_operate_challenge(user, month)


def visible_challenges_for(user, queryset):
    if not user.is_authenticated:
        return queryset.none()
    if user.is_superuser:
        return queryset
    membership_ids = Membership.objects.filter(user=user, is_active=True).values_list("pk", flat=True)
    return queryset.filter(
        Q(status__isnull=False) & (
            ~Q(status="draft")
            | Q(group__memberships__user=user, group__memberships__is_active=True, group__memberships__role=Membership.Role.OWNER)
            | Q(group__memberships__user=user, group__memberships__is_active=True, group__memberships__permission_overrides__manage_months=True)
            | Q(
                staff_assignments__membership_id__in=membership_ids,
                staff_assignments__role=ChallengeStaffAssignment.Role.HOST,
                staff_assignments__ended_at__isnull=True,
            )
        )
    ).distinct()


def can_manage_group_announcements(user, group):
    return has_capability(user, group, "manage_announcements")


def can_manage_challenge_announcements(user, month):
    return can_operate_challenge(user, month) or can_manage_months(user, month.group)


def can_manage_permissions(user, group):
    return has_capability(user, group, "manage_permissions")


def challenge_review_scope(user, month):
    """Return challenge-wide or team-scoped review authority from active staffing."""
    if user.is_authenticated and user.is_superuser:
        return "challenge", frozenset()
    membership = membership_for(user, month.group)
    if not membership:
        return None
    assignments = ChallengeStaffAssignment.objects.filter(
        month=month,
        membership=membership,
        ended_at__isnull=True,
        role__in=(
            ChallengeStaffAssignment.Role.HOST,
            ChallengeStaffAssignment.Role.TEAM_LEADER,
            ChallengeStaffAssignment.Role.FLOATER,
        ),
    )
    roles = set(assignments.values_list("role", flat=True))
    if roles.intersection({ChallengeStaffAssignment.Role.HOST, ChallengeStaffAssignment.Role.FLOATER}):
        return "challenge", frozenset()
    team_ids = frozenset(assignments.filter(
        role=ChallengeStaffAssignment.Role.TEAM_LEADER,
        team__isnull=False,
    ).values_list("team_id", flat=True))
    if team_ids:
        return "team", team_ids
    return None


def can_review_challenge(user, month):
    return challenge_review_scope(user, month) is not None


def scope_reviewable_submissions(user, month, queryset):
    scope = challenge_review_scope(user, month)
    if not scope:
        return queryset.none()
    scope_name, team_ids = scope
    if scope_name == "challenge":
        return queryset
    return queryset.filter(
        participant__team_assignments__month=month,
        participant__team_assignments__team_id__in=team_ids,
        participant__team_assignments__ended_at__isnull=True,
        participant__month_enrollments__month=month,
        participant__month_enrollments__is_active=True,
    ).distinct()


def can_review_submission(user, submission):
    return scope_reviewable_submissions(
        user,
        submission.month,
        submission.month.submissions.filter(pk=submission.pk),
    ).exists()


def _has_competition_staff_access(user, month, membership):
    if user.is_superuser:
        return True
    if not membership:
        return False
    if membership.role == Membership.Role.OWNER:
        return True
    if membership.role == Membership.Role.MODERATOR and membership_has_capability(membership, "manage_months"):
        return True
    return is_challenge_host(user, month)


def _can_view_competition_domain(user, month, visibility, *, team=None):
    if not user.is_authenticated:
        return False
    membership = membership_for(user, month.group)
    if _has_competition_staff_access(user, month, membership):
        return True
    if not membership:
        return False
    level = month.CompetitionVisibility
    if visibility == level.EVERYBODY:
        return True
    if visibility == level.HOSTS:
        return False
    if visibility == level.HOSTS_FLOATERS:
        return ChallengeStaffAssignment.objects.filter(
            month=month,
            membership=membership,
            role=ChallengeStaffAssignment.Role.FLOATER,
            ended_at__isnull=True,
        ).exists()
    if team is None or team.month_id != month.pk:
        return False
    if visibility == level.HOSTS_TEAM_LEADERS:
        return ChallengeStaffAssignment.objects.filter(
            month=month,
            team=team,
            membership=membership,
            role=ChallengeStaffAssignment.Role.TEAM_LEADER,
            ended_at__isnull=True,
        ).exists()
    if visibility == level.TEAM_MEMBERS:
        return MonthEnrollment.objects.filter(
            month=month,
            participant=membership,
            is_active=True,
        ).exists() and TeamAssignment.objects.filter(
            month=month,
            team=team,
            participant=membership,
            ended_at__isnull=True,
        ).exists()
    return False


def can_view_team_standings(user, month, team=None):
    return _can_view_competition_domain(
        user,
        month,
        month.team_standings_visibility,
        team=team,
    )


def can_view_reader_scores(user, month, team=None, reader=None):
    return _can_view_competition_domain(
        user,
        month,
        month.reader_scores_visibility,
        team=team,
    )


def can_configure_competition_visibility(user, month):
    return can_manage_months(user, month.group) or can_operate_challenge(user, month)


def can_view_access_code(user, group):
    if user.is_superuser:
        return True
    membership = membership_for(user, group)
    if not membership:
        return False
    if group.access_code_visibility == group.AccessCodeVisibility.MEMBERS:
        return True
    if group.access_code_visibility == group.AccessCodeVisibility.STAFF:
        return membership.role in STAFF_ROLES
    return membership.role == Membership.Role.OWNER
