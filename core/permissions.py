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


def can_view_group_settings(user, group):
    if not user.is_authenticated or not user.is_active:
        return False
    member = membership_for(user, group)
    return can_manage_group(user, group) or bool(member and member.role == Membership.Role.MODERATOR)


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


def can_view_challenge_card_statistics(user, month):
    """Statistics on already-discoverable Challenge cards only."""
    return user.is_authenticated and (user.is_superuser or month.status in {
        month.Status.COMPLETED, month.Status.ARCHIVED,
    })


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


def is_active_floater(month, participant_id):
    return month.staff_assignments.filter(
        membership_id=participant_id, role=ChallengeStaffAssignment.Role.FLOATER,
        ended_at__isnull=True,
    ).exists()


def restricted_host_team_ids(user, month):
    """None means no Host restriction. An empty set grants no team access."""
    if not getattr(user, "is_authenticated", False) or not is_challenge_host(user, month):
        return None
    enrollment = MonthEnrollment.objects.filter(month=month, participant__user=user).first()
    if enrollment is None or (not enrollment.is_active and enrollment.host_scope_restored):
        return None
    if not enrollment.is_active:
        return frozenset()
    return frozenset(TeamAssignment.objects.filter(
        month=month, participant_id=enrollment.participant_id, ended_at__isnull=True,
    ).values_list("team_id", flat=True))


def group_directory_visible_member_ids(user, group, participants):
    """Group aggregates cannot be split safely by a viewer's Challenge team scope."""
    visible_ids = {participant.pk for participant in participants}
    membership = membership_for(user, group)
    if user.is_superuser or not membership:
        return visible_ids
    hosted = group.challenge_months.filter(
        staff_assignments__membership=membership,
        staff_assignments__role=ChallengeStaffAssignment.Role.HOST,
        staff_assignments__ended_at__isnull=True,
    )
    for month in hosted:
        teams = restricted_host_team_ids(user, month)
        if teams is not None:
            in_scope = TeamAssignment.objects.filter(
                month=month, team_id__in=teams, ended_at__isnull=True,
                participant__month_enrollments__month=month,
                participant__month_enrollments__is_active=True,
            ).values_list("participant_id", flat=True)
            visible_ids.intersection_update(in_scope)
    visible_ids.update(participant.pk for participant in participants if participant.user_id == user.pk)
    return visible_ids


def host_scope_allows_reader(user, month, participant_id):
    teams = restricted_host_team_ids(user, month)
    return teams is None or TeamAssignment.objects.filter(
        month=month, participant_id=participant_id, team_id__in=teams, ended_at__isnull=True,
        participant__month_enrollments__month=month, participant__month_enrollments__is_active=True,
    ).exists()


def can_run_challenge_wide_operations(user, month):
    return can_operate_challenge(user, month) and restricted_host_team_ids(user, month) is None


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
    if ChallengeStaffAssignment.Role.HOST in roles:
        teams = restricted_host_team_ids(user, month)
        if teams is not None:
            return "team", teams
    if roles.intersection({ChallengeStaffAssignment.Role.HOST, ChallengeStaffAssignment.Role.FLOATER}):
        return "challenge", frozenset()
    team_ids = frozenset(assignments.filter(
        role=ChallengeStaffAssignment.Role.TEAM_LEADER,
        team__isnull=False,
    ).values_list("team_id", flat=True))
    if team_ids:
        return "team", team_ids
    return None


def challenge_roster_read_scope(user, month):
    """Scope staff access to historical planning and Discord on Challenge rosters."""
    if not user.is_active or not can_view_challenge(user, month):
        return None
    return challenge_review_scope(user, month)


def roster_read_scope_includes(scope, team_id):
    return bool(scope and (scope[0] == "challenge" or team_id in scope[1]))


def challenge_history_locked(month):
    return month.status in {month.Status.COMPLETED, month.Status.ARCHIVED}


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


def _has_submission_void_authority(user, submission):
    if not user.is_authenticated or not user.is_active:
        return False
    if user.is_superuser:
        return True
    member = membership_for(user, submission.month.group)
    if (member and member.role in {Membership.Role.OWNER, Membership.Role.MODERATOR}
            and restricted_host_team_ids(user, submission.month) is None):
        return True
    return can_review_submission(user, submission)


def can_void_submission(user, submission):
    """Soft-removal authority; callers also enforce the Challenge lifecycle."""
    return not submission.is_removed and _has_submission_void_authority(user, submission)


def can_restore_submission(user, submission):
    month = submission.month
    return (
        submission.is_removed
        and submission.participant.group_id == month.group_id
        and month.status in {month.Status.DRAFT, month.Status.UPCOMING, month.Status.ACTIVE}
        and _has_submission_void_authority(user, submission)
    )


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
    host_teams = restricted_host_team_ids(user, month)
    if host_teams is not None:
        if month.status in {month.Status.COMPLETED, month.Status.ARCHIVED}:
            return True
        if visibility == month.CompetitionVisibility.EVERYBODY:
            return True
        return bool(team and team.month_id == month.pk and team.pk in host_teams)
    if _has_competition_staff_access(user, month, membership):
        return True
    if not membership:
        return False
    if month.status in {month.Status.COMPLETED, month.Status.ARCHIVED}:
        return True
    level = month.CompetitionVisibility
    if visibility == level.EVERYBODY:
        return True
    if visibility == level.HOSTS:
        return False
    if visibility in {level.HOSTS_FLOATERS, level.HOSTS_TEAM_LEADERS}:
        if ChallengeStaffAssignment.objects.filter(
            month=month,
            membership=membership,
            role=ChallengeStaffAssignment.Role.FLOATER,
            ended_at__isnull=True,
        ).exists():
            return True
        if visibility == level.HOSTS_FLOATERS:
            return False
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


def can_correct_registration_answers(user, enrollment):
    month = enrollment.month
    if (not user.is_authenticated or not user.is_active
            or enrollment.participant.group_id != month.group_id
            or month.status not in {
                month.Status.DRAFT, month.Status.UPCOMING, month.Status.ACTIVE, month.Status.FINALIZING,
            }):
        return False
    if user.is_superuser:
        return True
    member = membership_for(user, month.group)
    return bool(member and (
        member.role in STAFF_ROLES or is_challenge_host(user, month)
    ) and host_scope_allows_reader(user, month, enrollment.participant_id))


def can_manage_challenge_tbr(user, enrollment):
    """TBR-only staff authority; never grants registration or account access."""
    month = enrollment.month
    if not user.is_authenticated or enrollment.participant.group_id != month.group_id:
        return False
    if user.is_superuser:
        return True
    member = membership_for(user, month.group)
    if not member:
        return False
    if member.role in STAFF_ROLES and restricted_host_team_ids(user, month) is None:
        return True
    scope = challenge_review_scope(user, month)
    if not scope:
        return False
    return scope[0] == "challenge" or TeamAssignment.objects.filter(
        month=month, participant=enrollment.participant,
        team_id__in=scope[1], ended_at__isnull=True,
    ).exists()


def can_reconcile_personal_tbr(user, month):
    """Administrative repair authority, independent of broader review scope."""
    if not user.is_authenticated or not user.is_active:
        return False
    if user.is_superuser:
        return True
    member = membership_for(user, month.group)
    return bool(member and restricted_host_team_ids(user, month) is None
                and (member.role == Membership.Role.OWNER or is_challenge_host(user, month)))


def can_refresh_challenge_metadata(user, month):
    if not user.is_authenticated or not user.is_active or month.status not in {'draft', 'upcoming', 'active'}:
        return False
    if user.is_superuser:
        return True
    member = membership_for(user, month.group)
    return bool(member and (member.role == Membership.Role.OWNER or is_challenge_host(user, month)))


def can_refresh_tbr_metadata(user, book):
    enrollment = book.personal_tbr.enrollment
    if can_refresh_challenge_metadata(user, enrollment.month) and host_scope_allows_reader(user, enrollment.month, enrollment.participant_id):
        return True
    return bool(user.is_authenticated and user.is_active
        and enrollment.month.status in {'draft', 'upcoming', 'active'}
        and enrollment.is_active and enrollment.participant.is_active
        and enrollment.participant.user_id == user.pk)


def can_refresh_botm_metadata(user, book):
    return (not book.is_retired and restricted_host_team_ids(user, book.month) is None
            and can_refresh_challenge_metadata(user, book.month))


def can_edit_submission(user, submission):
    """Correction authority; does not broaden ordinary review or Reader access."""
    if (not user.is_authenticated or not user.is_active or submission.is_removed
            or submission.month.status not in {'active', 'finalizing'}):
        return False
    if user.is_superuser:
        return True
    member = membership_for(user, submission.month.group)
    if member and member.role == Membership.Role.OWNER and restricted_host_team_ids(user, submission.month) is None:
        return True
    return can_review_submission(user, submission)


def can_manage_team_assignments(user, month, participant=None):
    """Authority over existing team assignments, separate from enrollment and scoring."""
    if not user.is_authenticated or not user.is_active or month.status not in {month.Status.DRAFT, month.Status.UPCOMING, month.Status.ACTIVE}:
        return False
    if participant is not None and participant.group_id != month.group_id:
        return False
    if user.is_superuser:
        return True
    member = membership_for(user, month.group)
    if not member:
        return False
    host_teams = restricted_host_team_ids(user, month)
    if host_teams is not None and participant is not None and participant.user_id == user.pk:
        return False
    if member.role == Membership.Role.OWNER or is_challenge_host(user, month):
        return True
    return is_active_floater(month, member.pk)


def can_view_team_books(user, team):
    month = team.month
    if not user.is_authenticated or not user.is_active or team.is_archived or not can_view_challenge(user, month):
        return False
    if user.is_superuser:
        return True
    member = membership_for(user, month.group)
    if not member:
        return False
    host_teams = restricted_host_team_ids(user, month)
    if host_teams is not None:
        return team.pk in host_teams
    if member.role == Membership.Role.OWNER or is_challenge_host(user, month) or is_active_floater(month, member.pk):
        return True
    return (
        MonthEnrollment.objects.filter(month=month, participant=member, is_active=True).exists()
        and TeamAssignment.objects.filter(month=month, participant=member, team=team, ended_at__isnull=True).exists()
    )


def can_configure_discord_notifications(user, group):
    if not user.is_authenticated or not user.is_active:
        return False
    member = membership_for(user, group)
    return user.is_superuser or bool(member and member.role == Membership.Role.OWNER)


def can_view_discord_notifications(user, group):
    if can_configure_discord_notifications(user, group):
        return True
    member = membership_for(user, group) if user.is_authenticated and user.is_active else None
    return bool(member and member.role == Membership.Role.MODERATOR)
