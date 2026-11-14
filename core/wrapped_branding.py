"""Authorized artwork choices only; never calculate or filter recap statistics."""
from django import forms
from .models import ChallengeMonth, Team, TeamAssignment, MonthEnrollment
from .permissions import can_view_challenge, membership_for, is_active_floater
from .branding import community_branding, team_branding_many, can_view_branding_settings


def branding_choices(viewer, recap):
    options = {'sources': [('northbound', 'Northbound Default')], 'art': {}, 'teams': []}
    default = {'branding_source': 'northbound', 'branding_team': ''}
    sections = recap.get('sections', []) if recap else []
    contexts = [section.get('_branding_context') for section in sections]
    # Historical self-access or mixed private contexts never authorize identity discovery.
    if not contexts or any(not context for context in contexts):
        return options, default
    month_ids = {context['challenge_id'] for context in contexts}
    months = list(ChallengeMonth.objects.filter(pk__in=month_ids).select_related('group'))
    if len(months) != len(month_ids) or any(not m.group.is_active or not can_view_challenge(viewer,m) for m in months):
        return options, default
    by_id = {m.pk:m for m in months}
    if any(by_id[c['challenge_id']].group_id != c['group_id'] for c in contexts):
        return options, default
    groups = {m.group_id for m in months}
    if len(groups) != 1:
        return options, default
    group = months[0].group
    community = community_branding(viewer,group)
    if not community:
        return options, default
    options['sources'].append(('group','Group Branding'))
    options['art']['group'] = {'name':group.name,'logo':community.get('group_logo',''),'banner':community.get('group_banner','')}
    default['branding_source'] = 'group'
    if len(months) != 1:
        return options, default
    month = months[0]
    community = community_branding(viewer,group,month)
    options['sources'].append(('challenge','Challenge Branding'))
    options['art']['challenge'] = {'name':month.name,
        'logo':community.get('challenge_logo') or community.get('group_logo') or '',
        'banner':community.get('challenge_banner') or community.get('group_banner') or ''}
    default['branding_source'] = 'challenge'
    member = membership_for(viewer,group)
    current = set()
    if member and MonthEnrollment.objects.filter(month=month,participant=member,is_active=True).exists():
        current = set(TeamAssignment.objects.filter(month=month,participant=member,ended_at__isnull=True).values_list('team_id',flat=True))
    floater = bool(member and is_active_floater(month,member.pk))
    eligible = []
    for team in month.teams.all().order_by('name','pk'):
        team.month = month
        # Results visibility alone is deliberately not an artwork authority.
        if floater or team.pk in current or can_view_branding_settings(viewer,team):
            eligible.append(team)
    art = team_branding_many(viewer,eligible)
    options['teams'] = [{'id':str(t.pk),'name':t.name,'logo':art[t.pk]['effective_logo'],
        'banner':art[t.pk]['effective_banner']} for t in eligible]
    if eligible:
        options['sources'].append(('team','Team Branding'))
    if recap['perspective']=='team' and len(sections)==1:
        target = str(sections[0].get('_branding_team_id') or '')
        if target in {t['id'] for t in options['teams']}:
            default.update(branding_source='team',branding_team=target)
    return options, default


class WrappedBrandingForm(forms.Form):
    branding_source = forms.ChoiceField(label='Branding Source',widget=forms.Select(attrs={'data-branding-source':'','aria-describedby':'wrapped-branding-help'}))
    branding_team = forms.ChoiceField(required=False,label='Team artwork only',widget=forms.Select(attrs={'data-branding-team':'','aria-describedby':'wrapped-branding-help'}))

    def __init__(self, data, *, viewer, recap):
        self.options, default = branding_choices(viewer,recap)
        supplied = {key:data[key] for key in ('branding_source','branding_team') if key in data}
        values = supplied if supplied else default
        if supplied and 'branding_source' not in supplied:
            values = {**supplied,'branding_source':default['branding_source']}
        super().__init__(values)
        self.fields['branding_source'].choices = self.options['sources']
        self.fields['branding_team'].choices = [('', 'Choose a Team — artwork only')] + [(t['id'],t['name']) for t in self.options['teams']]

    def clean(self):
        values = super().clean()
        if values.get('branding_source')=='team' and not values.get('branding_team'):
            self.add_error('branding_team','Choose an authorized Team for artwork.')
        if values.get('branding_source')!='team' and values.get('branding_team'):
            self.add_error('branding_team','A Team selection applies only to Team Branding.')
        return values
