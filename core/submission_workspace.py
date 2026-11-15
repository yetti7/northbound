"""Permission-aware submission presentation; operations stay in their existing services."""
from copy import copy
from urllib.parse import urlencode, urlsplit, parse_qsl
from django.shortcuts import render
from django.urls import reverse, resolve
from .models import BookSubmission, MonthEnrollment, TeamAssignment, BotmMatch, PersonalTBRMatch
from .permissions import can_edit_submission, can_review_submission, can_operate_challenge, host_scope_allows_reader
from .administrative_matching import can_manage_credit
from .navigation_context import contextual_url, unpack

SECTIONS=(('overview','Overview'),('edit','Edit Book'),('qualifications','Qualifications'),
          ('review','Review & Scoring'),('history','History'))


def can_enter(actor, submission):
    # Identical to the existing Manage GET authorization. Not a new staff predicate.
    return bool(can_edit_submission(actor,submission) or can_manage_credit(actor,submission)
                or (actor.is_active and actor.is_superuser))


def operation_url(request,name,args):
    """Preserve only an authenticated, same-context signed origin on a form action."""
    url=reverse(name,args=args)
    token=request.GET.get('nav','')
    destination=copy(request);destination.resolver_match=resolve(url)
    if token and unpack(destination,token):
        url+='?'+urlencode({'nav':token})
    return url


def section_url(request,submission,section='overview',**query):
    url=reverse('submission-manage',args=[submission.month.group.slug,submission.month_id,submission.pk])
    return contextual_url(request,url+'?'+urlencode({'section':section,**query}))


def query_url(url,**query):
    parts=urlsplit(url);values=dict(parse_qsl(parts.query));values.update(query)
    return parts._replace(query=urlencode(values)).geturl()


def state_return_url(request,submission):
    if can_enter(request.user,submission):
        return section_url(request,submission)
    from .submission_activity import submission_timeline
    if submission_timeline(request.user,submission) is not None:
        return operation_url(request,'submission-history',[submission.month.group.slug,submission.month_id,submission.pk])
    return submission.month.get_absolute_url()


def immersive_context(actor, submission):
    from .immersive_ui import _readable
    from .immersive import can_request_immersive, can_review_immersive, review_queue_requests
    if not _readable(actor,submission):
        return {'claims':[],'can_request':False,'readable':False}
    claims=list(submission.immersive_requests.select_related('reviewed_by','revoked_by').order_by('-requested_at','-pk'))
    reviewable=set(review_queue_requests(actor=actor,month=submission.month).filter(submission=submission).values_list('pk',flat=True))
    for claim in claims:
        claim.can_review=claim.pk in reviewable
        claim.can_revoke=claim.status=='approved' and can_review_immersive(submission=submission,actor=actor)
        claim.can_withdraw=claim.status=='pending' and claim.requester_id==actor.pk and submission.month.status!='archived'
    return {'claims':claims,'can_request':can_request_immersive(submission=submission,actor=actor),'readable':True}


def workspace_context(request,submission,section):
    from .submission_activity import submission_timeline
    from .submission_review_matching import matching_cards
    from .reader_submissions import request_data, version
    actor=request.user;month=submission.month
    args=[month.group.slug,month.pk,submission.pk]
    review=can_review_submission(actor,submission)
    editable=can_edit_submission(actor,submission)
    active=not submission.is_removed and month.status in {'active','finalizing'}
    credit=can_manage_credit(actor,submission)
    cards=matching_cards(submission)
    card_map={(c['domain'],c['entry'].pk):c for c in cards}
    groups=[];requests=[]
    for domain,label,model,field in [('botm','Book of the Month',BotmMatch,'botm_book'),('tbr','Personal TBR',PersonalTBRMatch,'personal_tbr_book')]:
        matches=list(model.objects.filter(submission=submission).select_related(field,'reviewer'))
        rows=[]
        can_adjudicate=review and active and (domain=='tbr' or (can_operate_challenge(actor,month) and host_scope_allows_reader(actor,month,submission.participant_id)))
        for match in matches:
            book=getattr(match,field);requested=bool(request_data(match));card=card_map.get((domain,book.pk))
            row={'match':match,'entry':book,'requested':requested,'card':card,'withdrawn':bool(match.decision_evidence.get('reader_withdrawal'))}
            if match.status=='pending_review' and can_adjudicate and not requested:
                route='botm-match-review' if domain=='botm' else 'personal-tbr-match-review'
                if submission.status=='approved' and card and card['state']!='conflict' and not match.conflict_reason:
                    row['confirm_url']=operation_url(request,route,[month.group.slug,month.pk,match.pk,'confirm'])
                row['reject_url']=operation_url(request,route,[month.group.slug,month.pk,match.pk,'reject'])
            if credit and active and submission.status=='approved' and getattr(month,'botm_enabled' if domain=='botm' else 'tbr_enabled'):
                operation='reverse' if match.is_qualifying else 'restore' if match.status=='rejected' and match.method=='manual_review' else None
                if operation:
                    row['correction_url']=query_url(operation_url(request,'submission-credit',args),domain=domain,entry=book.pk,operation=operation)+'#credit-'+domain
                    row['correction_label']='Review Qualification Reversal' if operation=='reverse' else 'Review Qualification Restoration'
            if requested and match.status=='pending_review' and review and active:
                from .matching_resolution import identity_stamp
                stale=request_data(match).get('fingerprint')!=identity_stamp(submission,book)
                requests.append({'domain':domain,'label':label,'match':match,'book':book,
                    'can_approve':submission.status=='approved' and not stale and bool(card) and card['state']!='conflict' and not match.conflict_reason,
                    'can_reject':not stale,'blocked_reason':'The requested identity changed; authorized correction review is required.' if stale else ''})
            rows.append(row)
        suggestions=[c for c in cards if c['domain']==domain and not any(getattr(m,field+'_id')==c['entry'].pk for m in matches)]
        status=('Qualified' if any(m.is_qualifying for m in matches) else 'Pending review' if any(m.status=='pending_review' for m in matches)
                else 'Rejected / withdrawn' if any(m.status=='rejected' for m in matches) else 'No qualification')
        groups.append({'domain':domain,'label':label,'enabled':getattr(month,'botm_enabled' if domain=='botm' else 'tbr_enabled'),'rows':rows,'suggestions':suggestions,'status':status})
    assignment=TeamAssignment.objects.filter(month=month,participant=submission.participant,ended_at__isnull=True).select_related('team').first()
    enrollment=MonthEnrollment.objects.filter(month=month,participant=submission.participant).select_related('credited_team').first()
    team=assignment.team if assignment else enrollment.credited_team if enrollment else None
    immersive=immersive_context(actor,submission)
    return {'active':section,'navigation':[{'key':key,'label':label,'url':section_url(request,submission,key)} for key,label in SECTIONS],
        'overview_url':section_url(request,submission),'edit_url':section_url(request,submission,'edit'),
        'qualifications_url':section_url(request,submission,'qualifications'),'review_url':section_url(request,submission,'review'),
        'history_url':section_url(request,submission,'history'),'review_post_url':operation_url(request,'submission-review',args),
        'edit_post_url':operation_url(request,'submission-editor',args),'credit_url':operation_url(request,'submission-credit',args),
        'immersive_post_url':operation_url(request,'submission-immersive',args),
        'void_url':operation_url(request,'submission-remove',args)+('&' if '?' in operation_url(request,'submission-remove',args) else '?')+'workspace=1',
        'restore_url':operation_url(request,'submission-restore',args)+('&' if '?' in operation_url(request,'submission-restore',args) else '?')+'workspace=1',
        'can_edit':editable,'can_review':review and active,'can_credit':credit,'credit_enabled':credit and active and submission.status=='approved' and (month.botm_enabled or month.tbr_enabled),
        'team':team,'groups':groups,'reader_requests':requests,'reader_request_version':version(submission,actor) if requests else '',
        'immersive':immersive,'timeline':submission_timeline(actor,submission,request.GET.get('page')),
        'pending_themes':submission.theme_claims.filter(status='pending').count() if review else None,
        'conflicts':any(c['state']=='conflict' for c in cards),
        'read_only':not active,'can_view_notes':review or actor.is_superuser}


def render_workspace(request,template,context,section):
    context=dict(context)
    saved=BookSubmission.objects.select_related('month__group','participant__user','catalog_book','catalog_edition').get(pk=context['submission'].pk)
    context['workspace']=workspace_context(request,saved,section)
    context['workspace']['submission']=saved
    args=[saved.month.group.slug,saved.month_id,saved.pk]
    for key,route in [('focused_book_url','submission-fix-book'),('focused_pages_url','submission-fix-pages'),('advanced_url','submission-edit')]:
        context[key]=operation_url(request,route,args)
    timeline=context['workspace']['timeline']
    if timeline:
        page=timeline['page']
        if page.has_previous():context['workspace']['history_previous']=section_url(request,saved,'history',page=page.previous_page_number())
        if page.has_next():context['workspace']['history_next']=section_url(request,saved,'history',page=page.next_page_number())
    context.setdefault('month',context['submission'].month)
    return render(request,template,context)
