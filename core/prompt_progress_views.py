from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404,render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from .models import ChallengeMonth,TeamAssignment
from .permissions import scope_reviewable_submissions
from .prompt_progress import categories,enrollments_and_scope,reporting_scope,progress_rows


@login_required
@never_cache
@require_GET
def prompt_progress(request,group_slug,month_pk,staff=False):
    month=get_object_or_404(ChallengeMonth.objects.select_related('group'),pk=month_pk,group__slug=group_slug)
    readers,scope=enrollments_and_scope(request.user,month,staff=staff)
    configured=list(categories(month))
    if not configured:raise Http404
    allowed_readers=readers
    teams=month.teams.all() if scope and scope[0]=='challenge' else month.teams.filter(pk__in=scope[1]) if scope else month.teams.none()
    for key in ('reader','team'):
        value=request.GET.get(key,'')
        if value and not value.isdecimal():raise Http404
    if request.GET.get('reader'):
        if not staff:raise PermissionDenied
        readers=readers.filter(participant_id=request.GET['reader'])
        if not readers.exists():raise PermissionDenied
    if request.GET.get('team'):
        if not teams.filter(pk=request.GET['team']).exists():raise PermissionDenied
        readers=readers.filter(participant_id__in=TeamAssignment.objects.filter(month=month,team_id=request.GET['team'],ended_at__isnull=True).values('participant_id'))
    kind=request.GET.get('category','')
    if kind:
        configured=[c for c in configured if c.kind==kind]
        if not configured:raise Http404
    status=request.GET.get('completion','')
    if status not in ('','complete','incomplete'):raise Http404
    # Aggregate only scoped, current qualifications before pagination.
    if status:
        from django.db.models import Count,Q
        ids=set()
        from .models import PromptClaim
        for category in configured:
            required=[p['id'] for p in category.frozen_prompt_set] or [p.pk for p in category.prompts.all() if p.is_active]
            completed=set(PromptClaim.objects.filter(category=category,participant_id__in=readers.values('participant_id'),prompt_id__in=required,is_current=True,state='qualified').filter(Q(submission__status='approved',submission__is_removed=False)|Q(photo_submission__status='approved')).values('participant_id').annotate(n=Count('prompt_id',distinct=True)).filter(n=len(required)).values_list('participant_id',flat=True))
            eligible=set(readers.values_list('participant_id',flat=True))
            ids.update(completed if status=='complete' else eligible-completed)
        readers=readers.filter(participant_id__in=ids)
    page=Paginator(readers,25).get_page(request.GET.get('page'))
    rows=progress_rows(month,page.object_list,configured)
    if status:rows=[row for row in rows if row['complete']==(status=='complete')]
    from .models import BookSubmission
    book_ids={p['submission'].pk for row in rows for p in row['prompts'] if p['submission']}
    reviewable=set(scope_reviewable_submissions(request.user,month,BookSubmission.objects.filter(pk__in=book_ids)).values_list('pk',flat=True)) if staff and month.status in {'active','finalizing'} else set()
    from .photo_review import pending_review_photos
    photo_ids={p['photo'].pk for row in rows for p in row['prompts'] if p['photo']}
    reviewable_photos=set(pending_review_photos(request.user,month).filter(pk__in=photo_ids).values_list('pk',flat=True)) if staff and photo_ids else set()
    for row in rows:
        for prompt in row['prompts']:
            prompt['can_review_photo']=bool(prompt['photo'] and prompt['photo'].pk in reviewable_photos)
            prompt['can_review']=bool(prompt['submission'] and prompt['submission'].pk in reviewable)
    # Set only after this view has authorized the report and every supplied filter.
    if staff:request._navigation_validated_prompt_report = True
    params=request.GET.copy();params.pop('page',None)
    return render(request,'core/prompt_progress.html',{'month':month,'rows':rows,'staff_report':staff,'page_obj':page,
        'reader_options':allowed_readers,'team_options':teams,'filters':request.GET,'query':params.urlencode()})
