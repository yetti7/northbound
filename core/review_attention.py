from .models import BookSubmission, ChallengeMonth, ChallengeStaffAssignment, ThemeClaim
from .permissions import challenge_review_scope, scope_reviewable_submissions


ACTIONABLE_MONTH_STATUSES = (ChallengeMonth.Status.OPEN, ChallengeMonth.Status.CLOSED)


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
    if user.is_superuser:
        month_ids = ChallengeMonth.objects.filter(
            status__in=ACTIONABLE_MONTH_STATUSES,
        ).values_list("pk", flat=True)
    else:
        month_ids = ChallengeStaffAssignment.objects.filter(
            membership__user=user,
            membership__is_active=True,
            ended_at__isnull=True,
            month__status__in=ACTIONABLE_MONTH_STATUSES,
            role__in=(
                ChallengeStaffAssignment.Role.HOST,
                ChallengeStaffAssignment.Role.TEAM_LEADER,
                ChallengeStaffAssignment.Role.FLOATER,
            ),
        ).values_list("month_id", flat=True).distinct()
    challenges = []
    for month in ChallengeMonth.objects.filter(pk__in=month_ids).select_related("group"):
        count = challenge_attention_count(user, month)
        if not count:
            continue
        scope_name, team_ids = challenge_review_scope(user, month)
        scope_label = "Entire Challenge"
        if scope_name == "team":
            scope_label = ", ".join(month.teams.filter(pk__in=team_ids).values_list("name", flat=True))
        challenges.append({"month": month, "count": count, "scope_label": scope_label})
    return {"total": sum(item["count"] for item in challenges), "challenges": challenges}
