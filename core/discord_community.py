"""Approved opt-in projections for the configured Community Discord audience.

Non-Draft identity/schedule metadata is visible to every Group member and is
explicitly approved for Discord sharing by the Stage C opt-in policy. Links keep
the existing authentication requirements; this does not create public web access.
"""
from django.utils import timezone


def community_eligible(month):
    return bool(month.group.is_active and month.discord_community_opt_in
        and month.status in {'upcoming','active','finalizing','completed','archived'})


def participation_visible(month):
    # Discord recipients have no authenticated Northbound identity. The metadata
    # exception does not grant them membership or statistics permissions.
    from django.contrib.auth.models import AnonymousUser
    from .permissions import can_view_challenge, can_view_challenge_card_statistics
    audience = AnonymousUser()
    return community_eligible(month) and can_view_challenge(audience, month) and can_view_challenge_card_statistics(audience, month)


def results_visible(month):
    from django.contrib.auth.models import AnonymousUser
    from .permissions import can_view_challenge, can_view_team_standings
    audience = AnonymousUser()
    return community_eligible(month) and month.status in {'completed','archived'} and can_view_challenge(audience, month) and can_view_team_standings(audience, month)


def community_fields(month, categories):
    """The existing summary projection, shared by presentation diagnostics only."""
    selected=set(categories)
    fields=[]
    if 'active' in selected and month.status=='active':fields.append({'name':'Active Challenge','value':'Reading is underway.','inline':False})
    if 'upcoming' in selected and month.status=='upcoming':fields.append({'name':'Upcoming Challenge','value':'Get ready for the next Challenge.','inline':False})
    if 'registration' in selected and month.status in {'upcoming','active'}:
        fields.append({'name':'Registration','value':'Open' if month.registration_is_open else 'Closed','inline':False})
    if 'dates' in selected and month.starts_on and month.ends_on:
        fields.append({'name':'Challenge Dates','value':f'{month.starts_on:%b %d, %Y} – {month.ends_on:%b %d, %Y}','inline':False})
    # Completion releases member access, not access for arbitrary Discord recipients.
    if 'participation' in selected and participation_visible(month):
        fields.append({'name':'Participation','value':f'{month.enrollments.count()} registered Readers','inline':False})
    if 'results' in selected and results_visible(month):
        fields.append({'name':'Published Results','value':'Final standings are available in Northbound.','inline':False})
    return fields


def community_empty_reason(group, categories, *, months=None):
    months = months if months is not None else group.challenge_months.select_related("group").all()
    eligible = [month for month in months if community_eligible(month)]
    if not eligible:return "no_eligible"
    return "no_selected_content" if not any(community_fields(month,categories) for month in eligible) else ""


def community_embeds(group, categories, *, send_empty=False):
    from .models import ChallengeMonth
    from .discord_embeds import embed, name, clip, link_field, application_url, COMMUNITY_COLOR, community_status_embed
    selected=set(categories)
    result=[]
    months=list(ChallengeMonth.objects.select_related('group').filter(group=group,discord_community_opt_in=True).exclude(status='draft').order_by('pk'))
    for month in months:
        if not community_eligible(month):continue
        fields=community_fields(month,selected)
        if not fields:continue
        link=link_field('View Final Standings' if 'results' in selected and results_visible(month) else 'View Challenge',
            application_url('month-detail',[group.slug,month.pk],public=True))
        if link:fields.append(link)
        value=embed(group=group,title=clip('📚 '+name(group.name)+' — Challenge Update',256),
            description='**'+name(month.name,markdown=True)+'**',color=COMMUNITY_COLOR,timestamp=timezone.now().isoformat())
        value['fields']=fields;result.append(value)
    if not result and send_empty:result=[community_status_embed(group,timezone.now().isoformat(),reason=community_empty_reason(group,selected,months=months))]
    return result
