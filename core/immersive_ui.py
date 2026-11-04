"""Submission-scoped UI adapters; business decisions remain in immersive.py."""
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError
from django.http import HttpResponseForbidden, HttpResponseNotAllowed, Http404
from django.shortcuts import get_object_or_404, redirect, render

from .forms import ImmersiveSettingsForm
from .immersive import (can_request_immersive, can_review_immersive, configure_immersive,
    request_immersive, review_immersive, review_queue_requests, withdraw_immersive)
from .models import BookSubmission, ChallengeMonth, ImmersiveRequest, Membership
from .permissions import can_manage_months, can_operate_challenge, can_review_submission, can_view_challenge, membership_for


class ActionForm(forms.Form):
    reason = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 3}))
    confirm = forms.BooleanField(label='I confirm this action')


def _readable(user, submission):
    member = membership_for(user, submission.month.group)
    return user.is_active and can_view_challenge(user, submission.month) and (submission.participant.user_id == user.pk or
        can_review_submission(user, submission) or bool(member and member.role == Membership.Role.OWNER))


def _load_submission(group_slug, month_pk, pk):
    return get_object_or_404(BookSubmission.objects.select_related('month__group', 'participant__user'),
        pk=pk, month_id=month_pk, month__group__slug=group_slug)


@login_required
def settings_view(request, group_slug, month_pk):
    month=get_object_or_404(ChallengeMonth.objects.select_related('group'),pk=month_pk,group__slug=group_slug)
    if not request.user.is_active or not (can_operate_challenge(request.user,month) or can_manage_months(request.user,month.group)):
        return HttpResponseForbidden('Challenge configuration authority is required.')
    if month.status not in {'draft','upcoming','active'}:
        return HttpResponseForbidden('This Challenge is not configurable.')
    if request.method not in {'GET','POST'}: return HttpResponseNotAllowed(['GET','POST'])
    form=ImmersiveSettingsForm(request.POST if request.method=='POST' else None,instance=month)
    if request.method=='POST' and form.is_valid():
        try:
            configure_immersive(month=month,actor=request.user,enabled=form.cleaned_data['immersive_enabled'],percent=form.cleaned_data['immersive_bonus_percent'])
        except PermissionDenied: return HttpResponseForbidden('Configuration authority is no longer available.')
        except (ValidationError,DatabaseError): form.add_error(None,'Settings could not be saved. Reload and check the current Challenge state.')
        else:
            messages.success(request,'Immersive Reading settings saved.')
            return redirect('challenge-settings',group_slug=group_slug,month_pk=month_pk)
    return render(request,'core/immersive_settings.html',{'month':month,'form':form})


@login_required
def submission_view(request, group_slug, month_pk, pk):
    submission=_load_submission(group_slug,month_pk,pk)
    if not _readable(request.user,submission): return HttpResponseForbidden('Submission access is required.')
    if request.method not in {'GET','POST'}:return HttpResponseNotAllowed(['GET','POST'])
    error=''
    if request.method=='POST':
        try: request_immersive(submission=submission,actor=request.user)
        except PermissionDenied:return HttpResponseForbidden('Only the eligible submitting Reader may request this bonus.')
        except (ValidationError,DatabaseError): error='The request could not be saved. It may already exist, or eligibility may have changed. Reload before retrying.'
        else:
            messages.success(request,'Immersive request submitted for separate staff approval.')
            return redirect('submission-immersive',group_slug=group_slug,month_pk=month_pk,pk=pk)
    claims=list(submission.immersive_requests.select_related('reviewed_by','revoked_by').order_by('-requested_at','-pk'))
    reviewable=set(review_queue_requests(actor=request.user,month=submission.month).values_list('pk',flat=True))
    for claim in claims:
        claim.can_review=claim.pk in reviewable
        claim.can_revoke=claim.status=='approved' and can_review_immersive(submission=submission,actor=request.user)
        claim.can_withdraw=claim.status=='pending' and claim.requester_id==request.user.pk and submission.month.status!='archived'
    return render(request,'core/submission_immersive.html',{'submission':submission,'month':submission.month,
        'claims':claims,'can_request':can_request_immersive(submission=submission,actor=request.user),'error':error})


@login_required
def action_view(request, group_slug, month_pk, pk, operation):
    if operation not in {'approve','reject','withdraw','revoke'}:raise Http404
    claim=get_object_or_404(ImmersiveRequest.objects.select_related('submission__month__group','submission__participant__user'),
        pk=pk,submission__month_id=month_pk,submission__month__group__slug=group_slug)
    submission=claim.submission
    if not _readable(request.user, submission): return HttpResponseForbidden('Submission access is required.')
    if operation=='withdraw':
        allowed=request.user.is_active and claim.requester_id==request.user.pk and submission.month.status!='archived' and claim.status in {'pending','withdrawn'}
    else:
        allowed=can_review_immersive(submission=submission,actor=request.user)
        if operation!='revoke':
            allowed=allowed and (review_queue_requests(actor=request.user,month=submission.month).filter(pk=pk).exists() or claim.status=={'approve':'approved','reject':'rejected'}[operation])
        else:allowed=allowed and claim.status in {'approved','revoked'}
    if not allowed:return HttpResponseForbidden('This action is not available within your current review scope or request state.')
    if request.method not in {'GET','POST'}:return HttpResponseNotAllowed(['GET','POST'])
    form=ActionForm(request.POST if request.method=='POST' else None)
    if operation in {'reject','revoke'}:form.fields['reason'].required=True
    if request.method=='POST' and form.is_valid():
        try:
            if operation=='withdraw':withdraw_immersive(request=claim,actor=request.user,reason=form.cleaned_data['reason'])
            else:review_immersive(request=claim,actor=request.user,decision={'approve':'approved','reject':'rejected','revoke':'revoked'}[operation],reason=form.cleaned_data['reason'])
        except PermissionDenied:return HttpResponseForbidden('Your authority has changed. Nothing was saved.')
        except (ValidationError,DatabaseError):form.add_error(None,'The action could not be completed. Reload to check the latest request and book state before retrying.')
        else:
            messages.success(request,{'approve':'Immersive award approved.','reject':'Immersive request rejected.','withdraw':'Immersive request withdrawn.','revoke':'Immersive award revoked.'}[operation])
            return redirect('submission-immersive',group_slug=group_slug,month_pk=month_pk,pk=submission.pk)
    return render(request,'core/immersive_action.html',{'claim':claim,'submission':submission,'month':submission.month,'form':form,'operation':operation})
