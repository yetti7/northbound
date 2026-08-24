from .models import ChallengeStaffAssignment, Membership


CAPABILITIES = {
    "manage_group_settings": "Manage Group settings, access code, and integrations",
    "manage_participants": "Manage permanent Group members",
    "manage_months": "Create and edit Months, delete Draft Months, and assign Hosts",
    "manage_announcements": "Edit permanent Group announcements",
    "remove_content": "Manage legacy team-stat visibility configuration",
    "view_hidden_stats": "View team statistics when they are restricted",
    "manage_permissions": "Manage Group roles and permission delegation",
}

# remove_content remains an internal compatibility capability for the
# Phase-3-deferred team-stat visibility setting. It is intentionally not a
# human-facing Group delegation control.
DELEGABLE_CAPABILITIES = {
    capability: label
    for capability, label in CAPABILITIES.items()
    if capability != "remove_content"
}

ROLE_CAPABILITIES = {
    Membership.Role.OWNER: set(CAPABILITIES),
    Membership.Role.MODERATOR: {"manage_announcements", "view_hidden_stats"},
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


def can_manage_announcements(user, group):
    return has_capability(user, group, "manage_announcements")


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
    ).distinct()


def can_review_submission(user, submission):
    return scope_reviewable_submissions(
        user,
        submission.month,
        submission.month.submissions.filter(pk=submission.pk),
    ).exists()


def can_remove(user, group):
    return has_capability(user, group, "remove_content")


def can_view_team_stats(user, month):
    if user.is_superuser:
        return True
    membership = membership_for(user, month.group)
    if not membership:
        return False
    if month.team_stats_visibility == month.TeamStatsVisibility.EVERYONE:
        return True
    override = membership.permission_overrides.get("view_hidden_stats")
    if override is not None:
        return bool(override)
    if month.team_stats_visibility == month.TeamStatsVisibility.STAFF:
        return membership.role in STAFF_ROLES
    return membership.role == Membership.Role.OWNER


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
