from .models import BookSubmission, ChallengeMonth, ChallengeStaffAssignment, ProgressCheckpointResult, ThemeClaim
from .permissions import challenge_review_scope, is_challenge_host, scope_reviewable_submissions


ACTIONABLE_MONTH_STATUSES = (ChallengeMonth.Status.ACTIVE, ChallengeMonth.Status.FINALIZING)


def challenge_attention_count(user, month):
    submissions = scope_reviewable_submissions(
        user,
        month,
        month.submissions.filter(is_removed=False),
    )
    manual_count = submissions.filter(status=BookSubmission.Status.PENDING).count()
    theme_claim_count = ThemeClaim.objects.filter(
        submission_id__in=submissions.values("pk"),
        status=ThemeClaim.Status.PENDING,
    ).count()
    return manual_count + theme_claim_count


def needs_attention_summary(user):
    if not user.is_authenticated:
        return {"total": 0, "challenges": []}
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
    challenges = []
    for month in ChallengeMonth.objects.filter(pk__in=month_ids).select_related("group"):
        review_count = challenge_attention_count(user, month)
        month_host_notices = [assignment for assignment in host_notices if assignment.month_id == month.pk]
        checkpoint_results = []
        if user.is_superuser or is_challenge_host(user, month):
            checkpoint_results = list(ProgressCheckpointResult.objects.filter(
                checkpoint__month=month,
                outcome=ProgressCheckpointResult.Outcome.BELOW,
            ).select_related("checkpoint", "participant").order_by("checkpoint__scheduled_at", "participant__display_name"))
        if not review_count and not checkpoint_results and not month_host_notices:
            continue
        scope_label = "Entire Challenge"
        if review_count:
            scope_name, team_ids = challenge_review_scope(user, month)
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
    return {"total": sum(item["count"] for item in challenges), "challenges": challenges}
