"""Small Reader interfaces; staff correction and review permissions stay separate."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from .models import BookSubmission, HardcoverConnection
from . import reader_submissions as service
from .reader_book_selection import picker_groups, preview_selection
from .submission_activity import submission_timeline


def _submission(group_slug,month_pk,pk):
    return get_object_or_404(BookSubmission.objects.select_related('month__group','participant__user',
        'catalog_book','catalog_edition','scoring_catalog_edition'),pk=pk,month_id=month_pk,month__group__slug=group_slug)


def _url(s, name='reader-submission'):
    return reverse(name,args=[s.month.group.slug,s.month_id,s.pk])


@login_required
def detail(request, group_slug, month_pk, pk):
    s=_submission(group_slug,month_pk,pk)
    if not service.owns(request.user,s):
        raise PermissionDenied
    error=''
    if request.method=='POST':
        try:
            service.request_qualification(submission=s,actor=request.user,domain=request.POST.get('domain'),
                token=request.POST.get('target'),state=request.POST.get('version'))
        except PermissionDenied:
            raise
        except (ValidationError,DatabaseError) as exc:
            error=' '.join(exc.messages) if isinstance(exc,ValidationError) else 'Another update prevented saving. Reload before trying again.'
        else:
            messages.success(request,'Qualification request saved for review. No credit has been awarded.')
            return redirect(_url(s))
        s=service.load(s.pk)
    elif request.method!='GET':
        return HttpResponse('Use GET or POST.',status=405)
    return render(request,'core/reader_submission.html',{'submission':s,'month':s.month,'error':error,
        'can_reader_edit':service.eligible(request.user,s,editing=True),'qualification_groups':service.qualifications(s,request.user),
        'request_version':service.version(s,request.user),'timeline':submission_timeline(request.user,s)})


@login_required
def edit(request,group_slug,month_pk,pk):
    s=_submission(group_slug,month_pk,pk)
    if not service.eligible(request.user,s,editing=True):
        raise PermissionDenied('Only your eligible Pending submission can be edited.')
    if request.method=='POST' and request.POST.get('action') in {'search','link','editions','edition'}:
        from .views import _submission_catalog_action
        return _submission_catalog_action(request,s.month)
    feedback=[]
    if request.method=='POST':
        # Feedback is read-only. Real selection evidence is checked under lock on save.
        preview_data={key:request.POST.get(key,'') for key in ('title','author','catalog_selection')}
        try:
            feedback=preview_selection(data=preview_data,month=s.month,participant=s.participant,actor=request.user,allow_finalizing=True)
            for item in feedback:
                if request.POST.get(item['domain']+'_choice'):
                    item['message']='Your selection will request qualification review when you Save Submission. No credit is guaranteed.'
        except ValidationError:
            if request.POST.get('action')=='intent-preview':
                return JsonResponse({'ok':False,'message':'Check the Hardcover selection or use manual entry.'},status=400)
        if request.POST.get('action')=='intent-preview':
            return JsonResponse({'ok':True,'feedback':feedback})
    form=service.PendingReaderForm(request.POST or None,month=s.month,instance=s)
    if request.method=='POST':
        if request.POST.get('action')=='qualification-preview' or 'qualification-preview' in request.GET:
            form.is_valid()
        elif form.is_valid():
            try:
                saved=service.save_pending(submission=s,actor=request.user,data=request.POST)
            except PermissionDenied:
                # A reviewer may have decided the book while the form was open.
                return HttpResponse('This submission is no longer eligible for Reader editing. Nothing was saved.',status=409)
            except (ValidationError,DatabaseError) as exc:
                form.add_error(None,' '.join(exc.messages) if isinstance(exc,ValidationError) else 'Another update prevented saving. Reload before trying again.')
            else:
                messages.success(request,'Submission saved. It remains Pending for review.')
                return redirect(_url(saved))
    elif request.method!='GET':
        return HttpResponse('Use GET or POST.',status=405)
    form.initial['version']=service.version(service.load(s.pk),request.user)
    selection_data=request.POST.copy() if request.method=='POST' else {}
    if request.method=='GET':
        for domain in service.DOMAINS:
            selected=service.requested_entry_id(s,domain)
            if not selected:
                model,field=service.DOMAINS[domain]
                original=getattr(s,'selected_'+domain+'_book_id')
                if original and model.objects.filter(submission=s,status='pending_review',**{field+'_id':original}).exists():
                    selected=original
            if selected:
                selection_data['selected_'+domain]=selected

    return render(request,'core/reader_submission_edit.html',{'form':form,'submission':s,'month':s.month,
        'cancel_url':_url(s),'picker_preview_url':_url(s,'reader-submission-edit'),'qualification_feedback':feedback,'hardcover_available':HardcoverConnection.objects.filter(group=s.month.group,is_valid=True).exists(),
        'challenge_book_groups':picker_groups(month=s.month,participant=s.participant,actor=request.user,
            data=selection_data,allow_finalizing=True)})
