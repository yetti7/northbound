"""Group already-authorized items; never make review or scoring decisions."""
from .models import TeamAssignment, MonthEnrollment
from .identity import public_name


def grouped_queue(month, submissions, immersive_requests, photos):
    books={}
    for book in submissions:
        types=[]
        if book.status=='pending':types.append(('Book submission',1))
        themes=sum(claim.status=='pending' for claim in book.theme_claims.all())
        if themes:types.append(('Themes',themes))
        for claim in book.review_prompt_claims:types.append((claim.category.get_kind_display()+' prompt · needs confirmation',1))
        books[book.pk]={'kind':'book','book':book,'member':book.participant,'types':types,'immersive':[]}
    for claim in immersive_requests:
        book=claim.submission
        row=books.setdefault(book.pk,{'kind':'book','book':book,'member':book.participant,'types':[],'immersive':[]})
        row['types'].append(('Immersive Reading',1));row['immersive'].append(claim)
    rows=list(books.values())+[{'kind':'photo','photo':p,'member':p.participant,'types':[('Scavenger Hunt photo',1)]} for p in photos]
    ids={row['member'].pk for row in rows}
    assignments={}
    for assignment in TeamAssignment.objects.filter(month=month,participant_id__in=ids,ended_at__isnull=True).select_related('team'):
        assignments.setdefault(assignment.participant_id,[]).append(assignment.team)
    credited={e.participant_id:e.credited_team for e in MonthEnrollment.objects.filter(month=month,participant_id__in=ids).select_related('credited_team')}
    from django.contrib.auth import get_user_model
    users={u.pk:u for u in get_user_model().objects.filter(pk__in={row['member'].user_id for row in rows}).select_related('northbound_profile')}
    teams={}
    for row in rows:
        member=row['member'];user=users[member.user_id];current=assignments.get(member.pk,[])
        team=current[0] if len(current)==1 else None
        if not current:
            team=credited.get(member.pk)
            if team is None and row['kind']=='photo' and row['photo'].accepted_team_assignment_id:
                team=row['photo'].accepted_team_assignment.team
        if team and team.month_id!=month.pk:team=None
        group=teams.setdefault(team.pk if team else None,{'name':team.name if team else 'Unassigned or historical Team','team':team,'count':0,'readers':{}})
        profile=getattr(user,'northbound_profile',None)
        reader=group['readers'].setdefault(member.pk,{'name':public_name(user),'avatar':profile.avatar_url if profile else '', 'count':0,'rows':[]})
        row['count']=sum(count for _,count in row['types']);group['count']+=row['count'];reader['count']+=row['count'];reader['rows'].append(row)
    groups=sorted(teams.values(),key=lambda g:(g['team'] is None,g['name'].casefold()))
    for group in groups:group['readers']=sorted(group['readers'].values(),key=lambda r:r['name'].casefold())
    return {'queue_groups':groups,'queue_total':sum(g['count'] for g in groups),'queue_readers':sum(len(g['readers']) for g in groups)}
