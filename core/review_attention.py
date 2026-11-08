from .immersive import review_queue_requests
from .models import BookSubmission, ChallengeMonth, ChallengeStaffAssignment, ProgressCheckpointResult, ThemeClaim
from .permissions import challenge_review_scope, is_challenge_host, scope_reviewable_submissions, restricted_host_team_ids


ACTIONABLE_MONTH_STATUSES = (ChallengeMonth.Status.ACTIVE, ChallengeMonth.Status.FINALIZING)


def challenge_attention_count(user, month):
    submissions = scope_reviewable_submissions(
        user,
        month,
        month.submissions.filter(is_removed=False),
    )
    if month.status not in ACTIONABLE_MONTH_STATUSES:
        submissions = submissions.none()
    manual_count = submissions.filter(status=BookSubmission.Status.PENDING).count()
    theme_claim_count = ThemeClaim.objects.filter(
        submission_id__in=submissions.values("pk"),
        status=ThemeClaim.Status.PENDING,
    ).count()
    return manual_count + theme_claim_count + review_queue_requests(actor=user, month=month).count()


def needs_attention_summary(user):
    if not user.is_authenticated:
        return {"total": 0, "challenges": [], "available": False}
    host_notices = []
    if not user.is_superuser:
        host_notices = list(ChallengeStaffAssignment.objects.filter(
            membership__user=user,
            membership__is_active=True,
            role=ChallengeStaffAssignment.Role.HOST,
            ended_at__isnull=True,
            host_assignment_notice_seen_at__isnull=True,
        ).select_related("month__group", "membership"))
    if user.is_superuser:
        month_ids = set(ChallengeMonth.objects.filter(
            status__in=ACTIONABLE_MONTH_STATUSES,
        ).values_list("pk", flat=True))
    else:
        month_ids = set(ChallengeStaffAssignment.objects.filter(
            membership__user=user,
            membership__is_active=True,
            ended_at__isnull=True,
            month__status__in=ACTIONABLE_MONTH_STATUSES,
            role__in=(
                ChallengeStaffAssignment.Role.HOST,
                ChallengeStaffAssignment.Role.TEAM_LEADER,
                ChallengeStaffAssignment.Role.FLOATER,
            ),
        ).values_list("month_id", flat=True).distinct())
    month_ids.update(assignment.month_id for assignment in host_notices)
    legacy_month_ids = set(month_ids)
    # Discover only pending Immersive candidates; the shared queue service makes
    # the actual authorization/eligibility decision, including Completed reviews.
    immersive_months = ChallengeMonth.objects.filter(
        status__in=(*ACTIONABLE_MONTH_STATUSES, ChallengeMonth.Status.COMPLETED),
        submissions__immersive_requests__status='pending',
    )
    if not user.is_superuser:
        immersive_months = immersive_months.filter(
            group__memberships__user=user, group__memberships__is_active=True)
    for month in immersive_months.distinct():
        if review_queue_requests(actor=user, month=month).exists():
            month_ids.add(month.pk)
    challenges = []
    for month in ChallengeMonth.objects.filter(pk__in=month_ids).select_related("group"):
        review_count = challenge_attention_count(user, month)
        month_host_notices = [assignment for assignment in host_notices if assignment.month_id == month.pk]
        checkpoint_results = []
        if month.pk in legacy_month_ids and (user.is_superuser or is_challenge_host(user, month)):
            checkpoints = ProgressCheckpointResult.objects.filter(
                checkpoint__month=month,
                outcome=ProgressCheckpointResult.Outcome.BELOW,
            )
            teams = restricted_host_team_ids(user, month)
            if teams is not None:
                checkpoints = checkpoints.filter(
                    participant__team_assignments__month=month,
                    participant__team_assignments__ended_at__isnull=True,
                    participant__team_assignments__team_id__in=teams,
                    participant__month_enrollments__month=month,
                    participant__month_enrollments__is_active=True,
                )
            checkpoint_results = list(checkpoints.select_related("checkpoint", "participant").order_by(
                "checkpoint__scheduled_at", "participant__display_name"
            ))
        if not review_count and not checkpoint_results and not month_host_notices:
            continue
        scope_label = "Entire Challenge"
        if review_count:
            scope_name, team_ids = challenge_review_scope(user, month) or ('challenge', frozenset())
            if scope_name == "team":
                scope_label = ", ".join(month.teams.filter(pk__in=team_ids).values_list("name", flat=True))
        challenges.append({
            "month": month,
            "count": review_count + len(checkpoint_results) + len(month_host_notices),
            "review_count": review_count,
            "checkpoint_results": checkpoint_results,
            "host_notices": month_host_notices,
            "scope_label": scope_label,
        })
    return {
        "total": sum(item["count"] for item in challenges),
        "challenges": challenges,
        "available": user.is_superuser or bool(month_ids),
    }
