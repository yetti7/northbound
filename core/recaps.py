"""Read-only, permission-filtered Chronicle cards. No scoring writes or recovery."""
import calendar
from datetime import date
from zoneinfo import ZoneInfo

from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Sum, Q
from django.utils import timezone

from .models import (BookSubmission, BotmCompletionAward, BotmMatch, ChallengeMonth, ImmersiveRequest,
    Membership, ModifierProvenance, MonthEnrollment, PersonalTBRCompletionAward, PersonalTBRMatch,
    TeamAssignment, ThemeClaim)
from .permissions import (can_view_challenge, can_view_challenge_card_statistics, can_view_reader_scores,
    can_view_team_standings, can_view_team_books, can_manage_challenge_tbr, challenge_review_scope,
    roster_read_scope_includes, membership_for, restricted_host_team_ids)
from .score_aggregation import challenge_score_totals, challenge_team_totals

MODES = {'active': 'Progress Snapshot', 'completed': 'Final Wrapped', 'archived': 'Final Wrapped'}
PERIODS = {'all': 'All recorded results', 'custom': 'Custom dates', 'month': 'Calendar month',
           'quarter': 'Calendar quarter', 'year': 'Calendar year'}
NOTES = [
    'Approved submission entries are Challenge records, not universally deduplicated books read.',
    'Pages and reading achievements use completion dates; modifier points and completion awards use their own effective dates.',
    'Results reflect current valid credit, including later approvals and authorized corrections, not a past leaderboard snapshot.',
    'Team credit follows current or retained credited-team attribution; it is not split across transfer dates.',
    'Combined cards include only authorized metrics. Team results may be visible while Challenge aggregate cards are withheld.',
    'Missing legacy participation evidence is not reconstructed. Listening hours and historical leaderboard positions are not inferred.',
]


def _active_viewer(viewer):
    if not viewer.is_authenticated or not viewer.is_active:
        raise PermissionDenied('An active account is required.')


def _personal_month_ids(viewer):
    return set(BookSubmission.objects.filter(participant__user=viewer).values_list('month_id', flat=True)) | set(
        MonthEnrollment.objects.filter(participant__user=viewer).values_list('month_id', flat=True))


def can_view_final_wrapped(viewer, month):
    """Compose existing discovery, membership and completed-statistics boundaries."""
    return bool(viewer.is_authenticated and viewer.is_active
        and month.status in {'completed', 'archived'} and can_view_challenge(viewer, month)
        and (viewer.is_superuser or membership_for(viewer, month.group)))


def available_challenges(viewer):
    _active_viewer(viewer)
    own = _personal_month_ids(viewer)
    # Keep the Group boundary before loading Challenge metadata.
    query = ChallengeMonth.objects.select_related('group')
    if not viewer.is_superuser:
        query = query.filter(Q(pk__in=own) | Q(group__memberships__user=viewer, group__memberships__is_active=True)).distinct()
    return [(month, can_view_challenge(viewer, month)) for month in
            query.order_by('-starts_on', '-pk')
            if month.pk in own or can_view_challenge(viewer, month)]


def _label(month, discoverable):
    return month.name if discoverable else f'Personal history #{month.pk}'


def _reader_detail_allowed(viewer, month, member, team_id):
    scope = challenge_review_scope(viewer, month)
    enrollment = MonthEnrollment.objects.filter(month=month, participant=member).select_related('month__group', 'participant').first()
    return bool(enrollment and (roster_read_scope_includes(scope, team_id)
                               or can_manage_challenge_tbr(viewer, enrollment)))


def filter_choices(viewer, *, perspective='reader', group_id=None, challenge_id=None, _contexts=None):
    contexts = available_challenges(viewer) if _contexts is None else _contexts
    groups = {}
    for month, visible in contexts:
        if perspective != 'reader' and not visible:
            continue
        label = month.group.name if visible else f'Former Group #{month.group_id} · personal history'
        # A discoverable context may supply the normal Group label.
        if month.group_id not in groups or visible:
            groups[month.group_id] = label
    months = [(m, v) for m, v in contexts if (not group_id or str(m.group_id) == str(group_id))
              and (perspective == 'reader' or v)]
    challenges = [(m.pk, _label(m, v)) for m, v in months]
    readers = {viewer.pk: 'My reading'}
    teams = []
    for month, visible in months:
        if not visible or (challenge_id and str(month.pk) != str(challenge_id)):
            continue
        if perspective == 'challenge':
            continue
        month_teams = list(month.teams.all())
        current = dict(TeamAssignment.objects.filter(month=month, ended_at__isnull=True).values_list('participant_id', 'team_id'))
        for team in month_teams if perspective == 'team' else []:
            scope = challenge_review_scope(viewer, month)
            operational = bool(scope and (scope[0] == 'challenge' or team.pk in scope[1]))
            if (can_view_team_standings(viewer, month, team=team) or can_view_team_books(viewer, team)
                    or can_view_reader_scores(viewer, month, team=team)):
                teams.append((team.pk, f'{month.name} · {team.name}'))
        scope = challenge_review_scope(viewer, month)
        viewer_member = membership_for(viewer, month.group)
        if perspective != 'reader' or not viewer.is_superuser:
            continue
        broad_details = bool(scope and scope[0] == 'challenge' or viewer_member and
            viewer_member.role in {'owner', 'moderator'} and restricted_host_team_ids(viewer, month) is None)
        # Reader audience checks currently depend on the team, not the individual.
        score_access = {team.pk: can_view_reader_scores(viewer, month, team=team) for team in month_teams}
        score_access[None] = can_view_reader_scores(viewer, month)
        for member in Membership.objects.filter(group=month.group, month_enrollments__month=month).select_related('user').distinct():
            team_id = current.get(member.pk)
            if (member.user_id != viewer.pk and score_access.get(team_id, False)
                    and (broad_details or roster_read_scope_includes(scope, team_id))):
                readers[member.user_id] = member.user.username
    return {'groups': list(groups.items()), 'challenges': challenges, 'readers': sorted(readers.items(), key=lambda row: (row[1].casefold(), row[0])), 'teams': teams}


def period_bounds(period, *, group, anchor=None, start=None, end=None):
    if period not in PERIODS:
        raise ValidationError('Choose a valid reporting period.')
    if period == 'custom':
        if not isinstance(start, date) or not isinstance(end, date) or start > end:
            raise ValidationError('Enter a start and end date in chronological order.')
        return start, end
    if period == 'all':
        return None, None
    anchor = anchor or timezone.localdate(timezone=ZoneInfo(group.timezone))
    if not isinstance(anchor, date):
        raise ValidationError('Enter a valid calendar date.')
    first = 1 if period == 'year' else ((anchor.month - 1) // 3) * 3 + 1 if period == 'quarter' else anchor.month
    last = 12 if period == 'year' else first + 2 if period == 'quarter' else first
    return date(anchor.year, first, 1), date(anchor.year, last, calendar.monthrange(anchor.year, last)[1])


def _bounded(query, field, bounds):
    start, end = bounds
    if start:
        query = query.filter(**{f'{field}__gte': start})
    if end:
        query = query.filter(**{f'{field}__lte': end})
    return query


def _card(key, label, value, detail=''):
    return {'key': key, 'label': label, 'value': value, 'digits': max(1, len(str(value))), 'detail': detail}


def _reading_cards(month, ids, bounds, *, achievements):
    books = _bounded(BookSubmission.objects.filter(month=month, participant_id__in=ids,
        status='approved', is_removed=False), 'completed_on', bounds)
    totals = books.aggregate(entries=Count('pk'), pages=Sum('approved_pages'))
    cards = [_card('entries', 'Approved submission entries', totals['entries']),
             _card('pages', 'Approved pages', totals['pages'] or 0, 'Recorded print-page credit; not listening hours.')]
    for row in books.values('book_format').annotate(count=Count('pk')).order_by('book_format'):
        label = dict(BookSubmission.Format.choices).get(row['book_format'], 'Other')
        cards.append(_card('format_' + row['book_format'], label + ' entries', row['count']))
    works = set()
    known_entries = 0
    for book in books.select_related('catalog_book', 'catalog_edition__book'):
        from .matching_resolution import _work_identity
        work, conflict = _work_identity(book)
        if work and not conflict and work.provider == 'hardcover' and work.provider_book_id:
            works.add((work.provider, work.provider_book_id))
            known_entries += 1
    if works:
        cards.append(_card('works', 'Verified catalog works', len(works),
            f'{known_entries} entries have trusted catalog identity; unlinked entries are excluded.'))
    if achievements:
        sources = set(ModifierProvenance.objects.filter(month=month, participant_id__in=ids,
            is_active=True, submission__in=books, source_type='theme_bonus').values_list('source_reference', flat=True))
        themes = sum(1 for pk, amount in ThemeClaim.objects.filter(submission__in=books, status='approved').values_list('pk','approved_bonus_pages')
                     if amount == 0 or f'theme_claim:{pk}' in sources)
        cards.append(_card('themes', 'Current Theme achievements', themes))
        for model, field, key, label in [(PersonalTBRMatch, 'personal_tbr_book_id', 'tbr', 'Personal TBR qualifications'),
                                        (BotmMatch, 'botm_book_id', 'botm', 'BOTM qualifications')]:
            count = model.objects.filter(month=month, participant_id__in=ids, submission__in=books,
                status='confirmed', is_qualifying=True).values(field).distinct().count()
            cards.append(_card(key, label, count))
        cards.append(_card('immersive', 'Active approved Immersive achievements',
            ImmersiveRequest.objects.filter(submission__in=books, status='approved', is_active=True).count()))
        for model, key, label in [(PersonalTBRCompletionAward, 'tbr_completion', 'Personal TBR completion achievements'),
                                   (BotmCompletionAward, 'botm_completion', 'BOTM completion achievements')]:
            awards = _bounded(model.objects.filter(month=month, participant_id__in=ids, is_qualifying=True), 'effective_date', bounds)
            references = set(ModifierProvenance.objects.filter(month=month, participant_id__in=ids, is_active=True,
                source_type=key).values_list('source_reference', flat=True))
            from .personal_tbr_completion import personal_tbr_completion_source_reference
            from .botm_completion import botm_completion_source_reference
            reference = personal_tbr_completion_source_reference if key == 'tbr_completion' else botm_completion_source_reference
            count = sum(1 for award in awards if award.bonus_amount_snapshot == 0 or reference(award) in references)
            cards.append(_card(key, label, count, 'Uses the stored award effective date.'))
    return cards, works


def _score_cards(score):
    return [_card('base_pages', 'Base credit', score['base_pages']),
            _card('modifier_pages', 'Active modifier credit', score['modifier_pages']),
            _card('total_pages', 'Earned Challenge points', score['total_pages'])]


def _team_members(month):
    """Mirror current/retained membership selection; totals always use the scoring helper."""
    current = dict(TeamAssignment.objects.filter(month=month, ended_at__isnull=True,
        team__month=month).values_list('participant_id', 'team_id'))
    result = {}
    for enrollment in month.enrollments.filter(participant__group_id=month.group_id).select_related('participant__user'):
        team_id = current.get(enrollment.participant_id) if enrollment.is_active else None
        if not enrollment.is_active or enrollment.awaiting_scoring_team:
            team_id = current.get(enrollment.participant_id) or enrollment.credited_team_id
        if team_id:
            result.setdefault(team_id, []).append(enrollment.participant)
    return result


def build_recap(*, viewer, perspective='reader', target_id=None, group_id=None, challenge_id=None,
                period='all', anchor=None, start=None, end=None, include_book_highlights=False):
    _active_viewer(viewer)
    def identifier(value):
        if value in (None, ''): return None
        try:
            result = int(value)
        except (TypeError, ValueError):
            raise ValidationError('Choose a valid recap target.') from None
        if result <= 0: raise ValidationError('Choose a valid recap target.')
        return result
    target_id, group_id, challenge_id = map(identifier, [target_id, group_id, challenge_id])
    if perspective not in {'reader', 'team', 'challenge'}:
        raise ValidationError('Choose a valid recap perspective.')
    if perspective == 'challenge' and target_id:
        if challenge_id and challenge_id != target_id:
            raise ValidationError('Choose one consistent Challenge target.')
        challenge_id = target_id
    contexts = available_challenges(viewer)
    choices = filter_choices(viewer, perspective=perspective, group_id=group_id, challenge_id=challenge_id, _contexts=contexts)
    if group_id and str(group_id) not in {str(pk) for pk, _ in choices['groups']}:
        raise PermissionDenied('This Group is outside your recap scope.')
    if challenge_id and str(challenge_id) not in {str(pk) for pk, _ in choices['challenges']}:
        raise PermissionDenied('This Challenge is outside your recap scope.')
    if perspective == 'reader':
        target_id = int(target_id or viewer.pk)
        if target_id != viewer.pk and not viewer.is_superuser:
            raise PermissionDenied('Only Platform Administrators may select another Reader.')
        if target_id not in {pk for pk, _ in choices['readers']}:
            raise PermissionDenied('This Reader is outside your recap scope.')
    if perspective == 'team' and target_id and str(target_id) not in {str(pk) for pk, _ in choices['teams']}:
        raise PermissionDenied('This team is outside your recap scope.')
    # Validate ranges even when there are no matching Challenges.
    if period not in PERIODS or period == 'custom' and (not start or not end or start > end):
        raise ValidationError('Choose a valid reporting period and chronological dates.')
    sections = []
    combined = {}
    all_works = set()
    for month, visible in contexts:
        if group_id and str(month.group_id) != str(group_id) or challenge_id and str(month.pk) != str(challenge_id):
            continue
        if month.status not in MODES:
            continue
        bounds = period_bounds(period, group=month.group, anchor=anchor, start=start, end=end)
        base = {'_branding_context': {'challenge_id': month.pk, 'group_id': month.group_id} if visible else None, 'challenge': _label(month, visible), 'group': month.group.name if visible else 'Personal history',
                'mode': MODES[month.status], 'timezone': month.group.timezone,
                'from_date': bounds[0].isoformat() if bounds[0] else '',
                'to_date': bounds[1].isoformat() if bounds[1] else '', 'cards': [], 'teams': [], 'contributions': []}
        if perspective == 'reader':
            own = target_id == viewer.pk
            member = Membership.objects.filter(group=month.group, user_id=target_id).first()
            if not member or not (BookSubmission.objects.filter(month=month, participant=member).exists()
                                  or month.enrollments.filter(participant=member).exists()):
                continue
            team = month.teams.filter(assignments__participant=member, assignments__ended_at__isnull=True).first()
            if not own and (not visible or not can_view_reader_scores(viewer, month, team=team, reader=member)
                    or not _reader_detail_allowed(viewer, month, member, team.pk if team else None)):
                continue
            cards, works = _reading_cards(month, [member.pk], bounds, achievements=True)
            score = challenge_score_totals(month=month, participant_ids=[member.pk], effective_from=bounds[0], effective_to=bounds[1])[member.pk]
            base['cards'] = cards + _score_cards(score)
            base['subject'] = f'{member.user.username} — My Reading'
            all_works.update(works)
            for card in base['cards']:
                if card['key'] != 'works':
                    combined.setdefault(card['key'], {'label': card['label'], 'value': 0})['value'] += card['value']
            if include_book_highlights:
                base['books'] = _book_highlights(viewer, month, [member.pk], bounds)
            sections.append(base)
            continue
        if not visible:
            continue
        teams = list(month.teams.all())
        permitted = [team for team in teams if can_view_team_standings(viewer, month, team=team)]
        if perspective == 'challenge':
            base['subject'] = 'Challenge Wrapped'
            if can_view_challenge_card_statistics(viewer, month):
                members = list(Membership.objects.filter(group=month.group).values_list('pk', flat=True))
                scope = challenge_review_scope(viewer, month)
                cards, works = _reading_cards(month, members, bounds, achievements=bool(scope and scope[0] == 'challenge'))
                all_works.update(works)
                scores = challenge_score_totals(month=month, effective_from=bounds[0], effective_to=bounds[1])
                total = {key: sum(row[key] for row in scores.values()) for key in ['base_pages','modifier_pages','total_pages']}
                base['cards'] = cards + _score_cards(total)
                for card in base['cards']:
                    if card['key'] != 'works':
                        combined.setdefault(card['key'], {'label': card['label'], 'value': 0})['value'] += card['value']
            base['teams'] = _standings(month, permitted, bounds)
            if not base['cards'] and not base['teams']:
                continue
            if include_book_highlights and can_view_challenge_card_statistics(viewer, month):
                base['books'] = _book_highlights(viewer, month, members, bounds)
            sections.append(base)
            continue
        attribution = _team_members(month)
        totals = challenge_team_totals(month=month, team_ids=[t.pk for t in permitted], effective_from=bounds[0], effective_to=bounds[1])
        for team in teams:
            if target_id and str(team.pk) != str(target_id):
                continue
            score_access = can_view_reader_scores(viewer, month, team=team)
            books_access = can_view_team_books(viewer, team)
            scope = challenge_review_scope(viewer, month)
            operational = bool(scope and (scope[0] == 'challenge' or team.pk in scope[1]))
            if team.pk not in totals and not books_access and not score_access:
                continue
            section = {**base, '_branding_team_id': team.pk, 'cards': [], 'teams': [], 'contributions': [], 'subject': team.name}
            members = attribution.get(team.pk, [])
            scope = challenge_review_scope(viewer, month)
            operational = bool(scope and (scope[0] == 'challenge' or team.pk in scope[1]))
            completed_results = can_view_final_wrapped(viewer, month)
            if completed_results or books_access and score_access and team.pk in totals:
                section['cards'], _ = _reading_cards(month, [m.pk for m in members], bounds, achievements=completed_results or operational)
            if team.pk in totals:
                section['cards'].append(_card('team_points', 'Team Challenge points', totals[team.pk]))
            if score_access:
                current = dict(TeamAssignment.objects.filter(month=month, ended_at__isnull=True).values_list('participant_id','team_id'))
                viewer_member = membership_for(viewer, month.group)
                broad_details = bool(scope and scope[0] == 'challenge' or viewer_member and
                    viewer_member.role in {'owner','moderator'} and restricted_host_team_ids(viewer, month) is None)
                active_roster = set(month.enrollments.filter(is_active=True).values_list('participant_id', flat=True))
                allowed = [m for m in members if m.pk in active_roster and current.get(m.pk) == team.pk
                           or m.user_id == viewer.pk or broad_details
                           or roster_read_scope_includes(scope, current.get(m.pk))]
                scores = challenge_score_totals(month=month, participant_ids=[m.pk for m in allowed], effective_from=bounds[0], effective_to=bounds[1])
                allowed.sort(key=lambda m: (-scores[m.pk]['total_pages'], m.user.username.casefold(), m.pk))
                section['contributions'] = [{'reader': m.user.username, 'points': scores[m.pk]['total_pages']} for m in allowed]
            if section['cards'] or section['contributions']:
                if include_book_highlights:
                    section['books'] = _book_highlights(viewer, month, [m.pk for m in members], bounds)
                sections.append(section)
    summary = [_card(key, row['label'], row['value']) for key, row in combined.items()] if perspective in {'reader', 'challenge'} and len(sections) > 1 else []
    if summary and all_works:
        summary.append(_card('works', 'Verified catalog works across selected Challenges', len(all_works), 'Only trusted identities; not a universal books-read total.'))
    heading = (f'{viewer.username} — My Reading' if target_id == viewer.pk else sections[0]['subject'] if sections else 'My Reading') if perspective == 'reader' else ('Team Wide' if perspective == 'team' else 'Challenge Wide')
    labels = {s['group'] for s in sections}
    if perspective != 'reader' and len(labels) == 1:
        heading = f'{next(iter(labels))} — {"Team Wrapped" if perspective == "team" else "Challenge Wrapped"}'
    from .branding import recap_branding
    return {'branding': recap_branding(viewer, group_id, challenge_id), 'heading': heading, 'perspective': perspective, 'period': PERIODS[period], 'sections': sections, 'summary': summary,
            'notes': NOTES, 'generated_at': timezone.now().isoformat(),
            'empty_message': 'No authorized recap cards are available. Progress Snapshot requires Active; Final Wrapped requires Completed or Archived.'}


def _standings(month, teams, bounds):
    totals = challenge_team_totals(month=month, team_ids=[t.pk for t in teams], effective_from=bounds[0], effective_to=bounds[1])
    # Show scores, not invented tie-breakers or immutable historical placements.
    return [{'name': team.name, 'points': totals[team.pk]} for team in sorted(teams, key=lambda t: (-totals[t.pk], t.pk))]


def _book_highlights(viewer, month, participant_ids, bounds):
    """At most six book highlights, authorized before grouping and ranking.

    Competition results alone do not grant submission-title access. Reuse staff
    submission scope and the existing Team Books active-roster boundary. Own
    historical entries retain the approved personal-history exception.
    """
    from urllib.parse import urlsplit
    from .submission_activity import activity_scope
    from .templatetags.book_covers import safe_cover_url
    from .matching_resolution import _work_identity
    staff, _ = activity_scope(viewer, month)
    allowed = Q(participant__user=viewer) | Q(pk__in=staff.values('pk'))
    teams = [team.pk for team in month.teams.all() if can_view_team_books(viewer, team)]
    if teams:
        roster = TeamAssignment.objects.filter(month=month, team_id__in=teams, ended_at__isnull=True,
            participant__is_active=True, participant__month_enrollments__month=month,
            participant__month_enrollments__is_active=True).values('participant_id')
        allowed |= Q(participant_id__in=roster)
    books = _bounded(BookSubmission.objects.filter(month=month, participant_id__in=participant_ids,
        status='approved', is_removed=False).filter(allowed), 'completed_on', bounds)
    # A single authorized query; no award joins or per-book database lookups.
    # Missing/conflicting identity stays submission-specific, regardless of title.
    groups = {}
    for book in books.select_related('catalog_book', 'catalog_edition__book').order_by('pk').iterator(chunk_size=500):
        work, conflict = _work_identity(book)
        trusted = bool(work and not conflict and work.provider == 'hardcover' and work.provider_book_id)
        key = ('work', work.provider, work.provider_book_id) if trusted else ('submission', book.pk)
        cover = safe_cover_url(work.cover_url) if work and not conflict else ''
        if cover:
            destination = urlsplit(cover)
            if destination.username or destination.password:
                cover = ''
        candidate = {'title': book.title, 'author': book.author, 'cover_url': cover}
        if key not in groups:
            groups[key] = {'book': candidate, 'count': 0, 'first': book.pk}
        group = groups[key]
        group['count'] += 1
        if cover and not group['book']['cover_url']:
            group['book'] = candidate
    ranked = sorted(groups.values(), key=lambda row: (-bool(row['book']['cover_url']), -row['count'], row['first']))
    return [row['book'] for row in ranked[:6]]
