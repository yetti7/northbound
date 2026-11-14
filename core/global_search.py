"""Read-only search over existing Northbound destinations and scopes."""
from collections import defaultdict

from django.db.models import Case, Exists, IntegerField, OuterRef, Q, Value, When
from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .models import BookSubmission, ChallengeMonth, MonthEnrollment, PersonalTBR, ReadingGroup, TeamAssignment
from .permissions import (
    can_edit_submission, can_operate_challenge, can_manage_team_assignments, can_run_challenge_wide_operations,
    can_view_team_books, challenge_review_scope, is_active_floater, membership_for,
    visible_challenges_for,
)
from .submission_activity import activity_scope, can_view_staff_activity

CATEGORIES = ("Navigation", "Groups", "Challenges", "Readers", "Books", "Submissions", "Actions")
RESULT_LIMIT = 6
CHALLENGE_LIMIT = 24
TEAM_LIMIT = 24


def _database_rank(query, *fields):
    exact = Q()
    prefix = Q()
    for field in fields:
        exact |= Q(**{f"{field}__iexact": query})
        prefix |= Q(**{f"{field}__istartswith": query})
    return Case(When(exact, then=Value(0)), When(prefix, then=Value(1)), default=Value(2))


def _match_rank(query, terms):
    matches = [0 if term == query else 1 if term.startswith(query) else 2
               for value in terms if query in (term := value.casefold())]
    return min(matches, default=None)


@never_cache
@require_GET
def global_search(request):
    user = request.user
    if not user.is_authenticated or not user.is_active:
        return JsonResponse({"results": []}, status=403)
    query = request.GET.get("q", "").strip()[:120]
    if len(query) < 2:
        return JsonResponse({"results": []})
    from .views import REVIEWABLE_MONTH_STATUSES, _team_book_values, month_is_configurable

    results = defaultdict(list)
    seen = set()
    current_group = request.GET.get("group")
    current_month = request.GET.get("month")
    term = query.casefold()

    def add(category, label, route, args=(), *, context="", group=None, month=None, terms=(), suffix=""):
        rank = _match_rank(term, (label, *terms))
        if rank is None or (category, label, route, args) in seen:
            return
        seen.add((category, label, route, args))
        priority = 0 if month and str(month.pk) == current_month else 1 if group and group.slug == current_group else 2
        results[category].append(((rank, priority, label.casefold()), {
            "label": label, "context": context, "url": reverse(route, args=args) + suffix,
        }))

    add("Navigation", "Dashboard", "config-dashboard" if user.is_superuser else "dashboard")
    add("Navigation", "My Groups", "config-group-list" if user.is_superuser else "dashboard")
    if not user.is_superuser:
        add("Navigation", "My Stats", "my-stats", terms=("reporting",))
    groups = ReadingGroup.objects.filter(is_active=True)
    if not user.is_superuser:
        groups = groups.filter(memberships__user=user, memberships__is_active=True)
    for group in groups.filter(name__icontains=query).order_by(_database_rank(query, "name"), "name", "pk")[:CHALLENGE_LIMIT]:
        add("Groups", group.name, "group-detail", (group.slug,), group=group)

    months = visible_challenges_for(user, ChallengeMonth.objects.filter(group__in=groups))
    for month in months.filter(name__icontains=query).select_related("group").order_by(_database_rank(query, "name"), "-starts_on", "pk")[:CHALLENGE_LIMIT]:
        add("Challenges", month.name, "month-detail", (month.group.slug, month.pk),
            context=month.group.name, group=month.group, month=month)

    matches_book = Q(title__icontains=query) | Q(author__icontains=query)
    # Current context and current Challenges take priority when bounding deep searches.
    month_id = int(current_month) if current_month and current_month.isascii() and current_month.isdecimal() and len(current_month) <= 18 else None
    context_order = Case(
        When(pk=month_id, then=Value(0)),
        When(group__slug=current_group, then=Value(1)),
        When(status=ChallengeMonth.Status.ACTIVE, then=Value(2)),
        default=Value(3), output_field=IntegerField(),
    )
    book_rank = _database_rank(query, "title", "author")
    for month in months.select_related("group").order_by(context_order, "-starts_on", "pk")[:CHALLENGE_LIMIT]:
        group = month.group
        args = (group.slug, month.pk)
        member = membership_for(user, group)
        scope = challenge_review_scope(user, month)
        context = f"{group.name} · {month.name}"

        def destination(category, label, route, *, terms=(), suffix=""):
            add(category, label, route, args, context=context, group=group, month=month, terms=terms, suffix=suffix)

        destination("Navigation", "Challenge overview", "month-detail")
        add("Navigation", "My Challenges", "month-list", (group.slug,), context=group.name, group=group)
        destination("Navigation", "Challenge Participants", "month-participant-list")
        destination("Navigation", "Teams", "team-list")
        if user.is_superuser:
            destination("Navigation", "All Submissions", "month-detail", suffix="#challenge-submissions")
        if member and not user.is_superuser:
            destination("Actions", "View My Submissions", "month-detail", suffix="#challenge-submissions")
            if (month.status == month.Status.ACTIVE and not is_active_floater(month, member.pk)
                    and MonthEnrollment.objects.filter(month=month, participant=member, is_active=True).exists()):
                destination("Actions", "Submit a Book", "submission-create", terms=("submit",))
            if (_match_rank(term, ("personal tbr",)) is not None and
                    PersonalTBR.objects.filter(enrollment__month=month, enrollment__participant=member,
                                               confirmed_at__isnull=False).exists()):
                destination("Navigation", "Personal TBR", "personal-tbr-detail")
        if month.status in REVIEWABLE_MONTH_STATUSES and scope and (scope[0] == "challenge" or scope[1]):
            destination("Actions", "Review Pending Submissions", "review-queue", terms=("review",))
        if can_view_staff_activity(user, month):
            destination("Navigation", "Recent Activity", "submission-activity")
        if (_match_rank(term, ("manage participants",)) is not None and month_is_configurable(month)
                and can_operate_challenge(user, month) and scope and (scope[0] == "challenge" or scope[1])):
            destination("Actions", "Manage Participants", "month-participant-list")
        if _match_rank(term, ("assign participants",)) is not None and can_manage_team_assignments(user, month):
            destination("Actions", "Assign Participants", "team-assignment-create")

        staff, own = activity_scope(user, month)
        managing = term == "manage submissions" and month.status in REVIEWABLE_MONTH_STATUSES
        submissions = staff if managing else month.submissions.filter(
            Q(pk__in=staff.values("pk")) | Q(pk__in=own.values("pk")), matches_book,
        )
        for book in submissions.filter(is_removed=False).select_related("participant__user", "month__group").order_by(
                book_rank, "-submitted_at", "-pk")[:RESULT_LIMIT]:
            editable = can_edit_submission(user, book)
            if managing and not editable:
                continue
            add("Actions" if managing else "Submissions", f"Manage Submission: {book.title}" if managing else book.title, "submission-manage" if editable else "submission-history",
                (*args, book.pk), context=f"{context} · {book.participant.user.username}", group=group, month=month,
                terms=(book.author, "manage submissions" if editable else ""))

        matching_readers = month.submissions.filter(
            matches_book, is_removed=False,
            status__in=[BookSubmission.Status.PENDING, BookSubmission.Status.APPROVED],
        ).values("participant_id")
        matching_assignments = TeamAssignment.objects.filter(
            team_id=OuterRef("pk"), ended_at__isnull=True, participant_id__in=matching_readers,
        )
        teams = month.teams.filter(is_archived=False).select_related("month__group").annotate(
            has_book_match=Exists(matching_assignments),
        ).order_by("pk")[:TEAM_LIMIT]
        books_navigation = _match_rank(term, ("team books",)) is not None
        leaders_action = (_match_rank(term, ("manage team leaders",)) is not None
                          and month.status not in {month.Status.COMPLETED, month.Status.ARCHIVED}
                          and can_run_challenge_wide_operations(user, month))
        for team in teams:
            team_args = (*args, team.pk)
            team_context = f"{context} · {team.name}"
            books_access = (books_navigation or team.has_book_match) and can_view_team_books(user, team)
            add("Navigation", "Team Roster", "team-detail", team_args, context=team_context,
                group=group, month=month, terms=(team.name,))
            if leaders_action:
                add("Actions", "Manage Team Leaders", "team-leader-list", team_args,
                    context=team_context, group=group, month=month)
            if books_access:
                add("Navigation", "Team Books", "team-detail", team_args, context=team_context,
                    group=group, month=month, suffix="?view=books")
                if not team.has_book_match:
                    continue
                for book in _team_book_values(team).filter(matches_book).order_by(book_rank, "-submitted_at", "-pk")[:RESULT_LIMIT]:
                    add("Books", book["title"], "team-book-detail", (*team_args, book["pk"]),
                        context=f"{team_context} · {book['participant__user__username']}",
                        group=group, month=month, terms=(book["author"],))

        readers = MonthEnrollment.objects.filter(month=month, is_active=True, participant__is_active=True)
        scope_match = Q(pk__in=[])
        if scope and scope[0] == "challenge":
            scope_match = Q()
        elif scope:
            scope_match |= Q(participant__team_assignments__month=month,
                             participant__team_assignments__ended_at__isnull=True,
                             participant__team_assignments__team_id__in=scope[1])
        if member and not (scope and scope[0] == "challenge"):
            scope_match |= Q(participant=member)
        discord = Q(participant__user__northbound_profile__discord_username__icontains=query)
        if not scope:
            discord &= Q(participant=member) if member else Q(pk__in=[])
        for row in readers.filter(scope_match).filter(
                Q(participant__user__username__icontains=query) | discord
        ).select_related("participant__user__northbound_profile").distinct().order_by(
            _database_rank(query, "participant__user__username",
                           "participant__user__northbound_profile__discord_username"),
            "participant__user__username", "pk",
        )[:RESULT_LIMIT]:
            profile = getattr(row.participant.user, "northbound_profile", None)
            add("Readers", row.participant.user.username, "challenge-participant-detail", (*args, row.participant_id),
                context=context, group=group, month=month, terms=(profile.discord_username if profile else "",))

    return JsonResponse({"results": [
        {"category": category, "items": [item for _, item in sorted(results[category], key=lambda entry: entry[0])[:RESULT_LIMIT]]}
        for category in CATEGORIES if results[category]
    ]})
