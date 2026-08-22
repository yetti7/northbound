from .models import Membership


CAPABILITIES = {
    "manage_group_settings": "Edit group settings and integrations",
    "manage_participants": "Add participants and manage monthly enrollment",
    "manage_months": "Create and configure challenge months",
    "manage_announcements": "Edit group and month announcements",
    "manage_teams": "Create teams and manage team assignments",
    "review_submissions": "Review submissions and verified page counts",
    "remove_content": "Remove participants, submissions, months, and teams",
    "view_hidden_stats": "View team statistics when they are restricted",
    "manage_permissions": "Change roles and permission overrides",
}

ROLE_CAPABILITIES = {
    Membership.Role.OWNER: set(CAPABILITIES),
    Membership.Role.ADMIN: {"manage_group_settings", "manage_participants", "manage_months", "manage_announcements", "manage_teams", "review_submissions", "view_hidden_stats"},
    Membership.Role.MODERATOR: {"manage_announcements", "review_submissions", "view_hidden_stats"},
    Membership.Role.GAME_MANAGER: {"view_hidden_stats"},
    Membership.Role.READER: set(),
}

STAFF_ROLES = {Membership.Role.OWNER, Membership.Role.ADMIN, Membership.Role.MODERATOR}


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


def can_manage_announcements(user, group):
    return has_capability(user, group, "manage_announcements")


def can_manage_teams(user, group):
    return has_capability(user, group, "manage_teams")


def can_manage_permissions(user, group):
    return has_capability(user, group, "manage_permissions")


def can_review(user, group):
    return has_capability(user, group, "review_submissions")


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
