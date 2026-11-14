from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods
from .models import ChallengeMonth, ScavengerPhotoSubmission
from .permissions import membership_for
from .photo_forms import PhotoSubmissionForm, PhotoWithdrawalForm
from .photo_submissions import (can_submit_photo, can_edit_pending_photo, create_photo_submission,
    edit_pending_photo, transition_photo_reservation)
from .prompt_reservations import category_available


def _owned(request, group_slug, month_pk, pk):
    return get_object_or_404(ScavengerPhotoSubmission.objects.select_related(
        'month__group','participant__user','prompt_claim__prompt'),
        pk=pk,month_id=month_pk,month__group__slug=group_slug,participant__user=request.user)


def _editable(request, photo):
    if not can_edit_pending_photo(request.user,photo) or not category_available(photo.month,'scavenger'):
        raise PermissionDenied('Your Photo submission is not currently editable.')


def _safe_payload(request):
    if request.FILES or {'participant','participant_id','reader','reader_id','month','month_id',
        'challenge','challenge_id','team','team_id','status','reviewer_verified','reviewed_by','earned_points'} & set(request.POST):
        raise PermissionDenied('Only the Photo form fields may be submitted. Photo uploads are not supported.')


@login_required
@never_cache
@require_http_methods(['GET','POST'])
def photo_create(request, group_slug, month_pk):
    month=get_object_or_404(ChallengeMonth.objects.select_related('group'),pk=month_pk,group__slug=group_slug)
    if not can_submit_photo(request.user,month):
        raise PermissionDenied('Photo submissions are unavailable for this Reader or Challenge.')
    _safe_payload(request)
    member=membership_for(request.user,month.group)
    form=PhotoSubmissionForm(request.POST if request.method=='POST' else None,month=month,member=member)
    if request.method=='POST' and form.is_valid():
        try:
            photo=create_photo_submission(month_id=month.pk,actor=request.user,prompt_id=form.cleaned_data['prompt'].pk,
                reader_confirmed=form.cleaned_data['reader_confirmed'],discord_message_url=form.cleaned_data['discord_message_url'])
        except ValidationError as exc:form.add_error(None,' '.join(exc.messages))
        else:
            messages.success(request,'Your Scavenger Hunt Photo submission is Pending review. No points have been awarded.')
            return redirect('photo-detail',group_slug=group_slug,month_pk=month_pk,pk=photo.pk)
    return render(request,'core/photo_form.html',{'month':month,'form':form,'photo_mode':True})


@login_required
@never_cache
@require_http_methods(['GET','POST'])
def photo_edit(request, group_slug, month_pk, pk):
    photo=_owned(request,group_slug,month_pk,pk);_editable(request,photo);_safe_payload(request)
    form=PhotoSubmissionForm(request.POST if request.method=='POST' else None,month=photo.month,member=photo.participant,
        photo=photo,initial={'prompt':photo.prompt_claim.prompt_id,'discord_message_url':photo.discord_message_url,'version':photo.version})
    if request.method=='POST' and form.is_valid():
        try:
            edit_pending_photo(photo_id=photo.pk,actor=request.user,version=form.cleaned_data['version'],
                prompt_id=form.cleaned_data['prompt'].pk,discord_message_url=form.cleaned_data['discord_message_url'])
        except ValidationError as exc:form.add_error(None,' '.join(exc.messages))
        else:
            messages.success(request,'Your Pending Photo submission was updated.')
            return redirect('photo-detail',group_slug=group_slug,month_pk=month_pk,pk=pk)
    return render(request,'core/photo_form.html',{'month':photo.month,'photo':photo,'form':form})


@login_required
@never_cache
@require_GET
def photo_detail(request, group_slug, month_pk, pk):
    from .photo_actions import scope_readable_photos,can_correct_photo,can_change_photo_credit,correction_authority
    month=get_object_or_404(ChallengeMonth.objects.select_related('group'),pk=month_pk,group__slug=group_slug)
    photo=get_object_or_404(scope_readable_photos(request.user,month).select_related('month__group','participant__user','prompt_claim__prompt'),pk=pk)
    from .photo_audit import photo_timeline
    return render(request,'core/photo_detail.html',{'month':photo.month,'photo':photo,
        'photo_events':photo_timeline(request,photo),
        'can_correct':can_correct_photo(request.user,photo),
        'can_void':photo.status=='approved' and can_change_photo_credit(request.user,photo,'void'),
        'can_restore':photo.status=='voided' and can_change_photo_credit(request.user,photo,'restore'),
        'editable':can_edit_pending_photo(request.user,photo) and category_available(photo.month,'scavenger'),
        'withdraw_form':PhotoWithdrawalForm(initial={'version':photo.version})})


@login_required
@never_cache
@require_http_methods(['POST'])
def photo_withdraw(request, group_slug, month_pk, pk):
    photo=_owned(request,group_slug,month_pk,pk);_editable(request,photo);_safe_payload(request)
    form=PhotoWithdrawalForm(request.POST)
    if form.is_valid():
        try:
            transition_photo_reservation(photo_id=photo.pk,actor=request.user,version=form.cleaned_data['version'],status='withdrawn')
        except ValidationError as exc:form.add_error(None,' '.join(exc.messages))
        else:
            messages.success(request,'Photo submission withdrawn. The prompt is available for a new attempt.')
            return redirect('photo-detail',group_slug=group_slug,month_pk=month_pk,pk=pk)
    return render(request,'core/photo_detail.html',{'month':photo.month,'photo':photo,'editable':True,'withdraw_form':form},status=400)


@login_required
@never_cache
@require_http_methods(['GET','POST'])
def photo_review(request, group_slug, month_pk, pk):
    from .photo_review import scope_reviewable_photos,photo_review_version,review_photo_submission
    from .photo_forms import PhotoReviewForm
    month=get_object_or_404(ChallengeMonth.objects.select_related('group'),pk=month_pk,group__slug=group_slug)
    # Scope before materializing the optional private Discord URL or review metadata.
    photo=get_object_or_404(scope_reviewable_photos(request.user,month).select_related(
        'month__group','participant__user','accepted_team_assignment__team','prompt_claim__prompt'),pk=pk)
    form=PhotoReviewForm(request.POST if request.method=='POST' else None,
        initial={'version':photo_review_version(photo,request.user)})
    if request.method=='POST':
        if request.FILES:raise PermissionDenied('Photo uploads are not supported.')
        if form.is_valid():
            try:
                review_photo_submission(photo_id=photo.pk,actor=request.user,version=form.cleaned_data['version'],
                    decision=form.cleaned_data['decision'],reviewer_verified=form.cleaned_data['reviewer_verified'])
            except ValidationError as exc:form.add_error(None,' '.join(exc.messages))
            else:
                messages.success(request,'Photo '+('approved and Scavenger Hunt credit recorded.' if form.cleaned_data['decision']=='approved' else 'rejected. The prompt reservation has been released.'))
                return redirect('review-queue',group_slug=group_slug,month_pk=month_pk)
    return render(request,'core/photo_review.html',{'month':month,'photo':photo,'form':form,'review_pending':photo.status=='pending'})


@login_required
@never_cache
@require_http_methods(['GET','POST'])
def photo_correct(request,group_slug,month_pk,pk):
    from .photo_actions import can_correct_photo,management_version,correct_photo,correction_preview
    from .photo_forms import PhotoStaffCorrectionForm
    photo=get_object_or_404(ScavengerPhotoSubmission.objects.select_related('month__group','participant__user','prompt_claim__prompt').defer('discord_message_url'),pk=pk,month_id=month_pk,month__group__slug=group_slug)
    if not can_correct_photo(request.user,photo):raise PermissionDenied('Current Photo correction authority is required.')
    if request.FILES:raise PermissionDenied('Uploads are not supported.')
    form=PhotoStaffCorrectionForm(request.POST if request.method=='POST' else None,photo=photo,
        initial={'version':management_version(photo,request.user,'correct'),'prompt':photo.prompt_claim.prompt_id,'discord_message_url':photo.discord_message_url})
    preview=None
    if request.method=='POST' and form.is_valid():
        if request.POST.get('action')=='preview':
            # Validate the submitted version before presenting even a blocked preview.
            from .photo_actions import _validate
            try:
                _validate(form.cleaned_data['version'],photo,request.user,'correct')
                preview=correction_preview(photo,request.user,prompt_id=form.cleaned_data['prompt'].pk,discord_message_url=form.cleaned_data['discord_message_url'])
                form.data=form.data.copy();form.data['preview_token']=preview['token']
            except ValidationError as exc:form.add_error(None,' '.join(exc.messages))
        else:
            try:
                correct_photo(photo_id=pk,actor=request.user,version=form.cleaned_data['version'],prompt_id=form.cleaned_data['prompt'].pk,
                    discord_message_url=form.cleaned_data['discord_message_url'],preview=form.cleaned_data.get('preview_token'))
            except ValidationError as exc:form.add_error(None,' '.join(exc.messages))
            else:
                messages.success(request,'Photo correction saved. Any confirmed prompt-credit replacement is recorded in the activity history.')
                return redirect('photo-detail',group_slug=group_slug,month_pk=month_pk,pk=pk)
    return render(request,'core/photo_correct.html',{'month':photo.month,'photo':photo,'form':form,'preview':preview})


@login_required
@never_cache
@require_http_methods(['GET','POST'])
def photo_credit_action(request,group_slug,month_pk,pk,action):
    from .photo_actions import can_change_photo_credit,management_version,change_photo_credit
    from .photo_forms import PhotoCreditActionForm
    photo=get_object_or_404(ScavengerPhotoSubmission.objects.select_related('month__group','participant__user','prompt_claim').defer('discord_message_url'),pk=pk,month_id=month_pk,month__group__slug=group_slug)
    if not can_change_photo_credit(request.user,photo,action):raise PermissionDenied('Current scoped credit authority is required.')
    form=PhotoCreditActionForm(request.POST if request.method=='POST' else None,initial={'version':management_version(photo,request.user,action)})
    if request.method=='POST' and form.is_valid():
        try:change_photo_credit(photo_id=pk,actor=request.user,version=form.cleaned_data['version'],action=action,reason=form.cleaned_data['reason'])
        except ValidationError as exc:form.add_error(None,' '.join(exc.messages))
        else:
            messages.success(request,'Photo '+('voided' if action=='void' else 'restored')+'. Frozen award amounts were preserved.')
            return redirect(photo.month)
    return render(request,'core/photo_credit_action.html',{'month':photo.month,'photo':photo,'form':form,'action':action})
