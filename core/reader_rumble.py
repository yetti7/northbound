"""Current Team leaderboard presentation. No stored snapshots or scoring writes."""
from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, render
from django.utils.text import slugify
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from .models import Membership, Team
from .permissions import (can_view_challenge, can_view_reader_scores, membership_for,
    challenge_review_scope, restricted_host_team_ids, roster_read_scope_includes)
from .score_aggregation import challenge_score_totals
from .branding import team_branding

LIMITS = (5, 10, 15, 20, 25)


class ReaderRumbleForm(forms.Form):
    top = forms.TypedChoiceField(label='Top Readers', choices=[(n, str(n)) for n in LIMITS], coerce=int, initial=20)
    bonuses = forms.BooleanField(label='Include Bonuses', required=False)
    include_zero = forms.BooleanField(label='Include Zero-Score Readers', required=False)


def can_generate_reader_rumble(actor, team):
    month = team.month
    if not actor.is_authenticated or not actor.is_active or not can_view_challenge(actor, month):
        return False
    if not can_view_reader_scores(actor, month, team=team):
        return False
    if actor.is_superuser:
        return True
    member = membership_for(actor, month.group)
    if not member:
        return False
    restricted = restricted_host_team_ids(actor, month)
    if restricted is not None:
        return team.pk in restricted
    return (member.role in {Membership.Role.OWNER, Membership.Role.MODERATOR}
            or roster_read_scope_includes(challenge_review_scope(actor, month), team.pk))


def build_reader_rumble(*, actor, team, top=20, bonuses=False, include_zero=False):
    # The presentation service itself authorizes; callers cannot bypass the view.
    if not can_generate_reader_rumble(actor, team):
        raise PermissionDenied('Scoped staff access to individual Reader scores is required.')
    if type(top) is not int or top not in LIMITS or type(bonuses) is not bool or type(include_zero) is not bool:
        raise forms.ValidationError('Choose an available leaderboard size and scoring mode.')
    members = list(Membership.objects.filter(group=team.month.group,
        month_enrollments__month=team.month, month_enrollments__is_active=True,
        team_assignments__month=team.month, team_assignments__team=team,
        team_assignments__ended_at__isnull=True).select_related('user').distinct())
    scores = challenge_score_totals(month=team.month, participant_ids=[m.pk for m in members])
    key = 'total_pages' if bonuses else 'base_pages'
    if not include_zero:
        members = [m for m in members if scores[m.pk][key] != 0]
    members.sort(key=lambda m: (-scores[m.pk][key], m.user.username.casefold(), m.pk))
    artwork = team_branding(actor, team)
    return {'team': team.name, 'challenge': team.month.name, 'top': top,
        'metric': 'Total Challenge Points' if bonuses else 'Base Pages',
        'rows': [{'rank': rank, 'username': m.user.username, 'value': scores[m.pk][key]}
                 for rank, m in enumerate(members[:top], 1)],
        'timezone': team.month.group.timezone,
        'logo': artwork.get('effective_logo', ''), 'banner': artwork.get('effective_banner', ''),
        'filename': 'reader-rumble-'+(slugify(team.month.name) or 'challenge')+'-'+(slugify(team.name) or 'team')}


@login_required
@require_GET
@never_cache
def reader_rumble(request, group_slug, month_pk, team_pk):
    team = get_object_or_404(Team.objects.select_related('month__group'), pk=team_pk,
        month_id=month_pk, month__group__slug=group_slug)
    if not can_generate_reader_rumble(request.user, team):
        raise PermissionDenied('Scoped staff access to individual Reader scores is required.')
    form = ReaderRumbleForm(request.GET if request.GET else {'top': 20})
    payload = build_reader_rumble(actor=request.user, team=team, **form.cleaned_data) if form.is_valid() else None
    return render(request, 'core/reader_rumble.html', {'team': team, 'month': team.month,
        'group': team.month.group, 'form': form, 'rumble': payload}, status=200 if payload is not None else 400)
