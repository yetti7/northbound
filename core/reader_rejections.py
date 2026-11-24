"""Private, local attention records. These functions never decide or score a submission."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from .models import ReaderRejectionNotice, AuditEvent


def validate_reason(reason):
    if not isinstance(reason,str) or not reason.strip() or not any(c.isalnum() for c in reason):
        raise ValidationError('Explain the reason for this rejection. It will be visible to the Reader.')
    if len(reason.strip())>4000:raise ValidationError('Keep the rejection reason within 4000 characters.')
    return reason.strip()


def outstanding(user):
    if not user.is_authenticated or not user.is_active:return ReaderRejectionNotice.objects.none()
    return ReaderRejectionNotice.objects.filter(reader_id=user.pk,acknowledged_at__isnull=True,resolved_at__isnull=True)


def record(*,category,target,book=None,photo=None,actor,reason,label,event=None):
    reason=validate_reason(reason)
    month=photo.month if photo is not None else book.month
    member=photo.participant if photo is not None else book.participant
    key=f'{category}:{target.pk}'
    event=event or AuditEvent.objects.create(actor=actor,group=month.group,action='reader.rejection',
        object_type=type(target).__name__,object_id=str(target.pk),
        summary='Reader-facing rejection: '+category)
    links={'book':book,'photo':photo}
    field={'theme':'theme_claim','immersive':'immersive_request','bingo':'prompt_claim','scavenger':'prompt_claim'}.get(category)
    if field:links[field]=target
    return ReaderRejectionNotice.objects.get_or_create(event=event,category=category,target_key=key,
        defaults={'reader_id':member.user_id,'month':month,'target_label':label[:600],
                  'reason':reason,'rejected_at':event.created_at,**links})[0]


@transaction.atomic
def acknowledge(user,pk):
    notice=ReaderRejectionNotice.objects.select_for_update().filter(pk=pk,reader_id=user.pk).first()
    if not user.is_active or notice is None:raise PermissionDenied('Only the recipient may acknowledge this notice.')
    if notice.acknowledged_at is None:
        notice.acknowledged_at=timezone.now();notice.save(update_fields=['acknowledged_at'])
    return notice


@transaction.atomic
def resolve_replacement(user,pk,*,category,new_target,book=None,photo=None):
    """Explicit old-notice → successful new-attempt link; never infer from titles."""
    try:pk=int(pk)
    except (TypeError,ValueError):raise ValidationError('Invalid replacement notice.') from None
    notice=ReaderRejectionNotice.objects.select_for_update().filter(pk=pk,reader_id=user.pk,category=category,resolved_at__isnull=True).first()
    if notice is None:raise ValidationError('This rejection cannot be resolved by this attempt.')
    if category=='immersive':
        valid=(notice.immersive_request_id and notice.immersive_request.status=='rejected'
               and notice.book_id==book.pk and new_target.submission_id==book.pk
               and new_target.requester_id==user.pk)
    elif category=='photo':
        valid=(notice.photo_id and notice.photo.status=='rejected' and notice.month_id==photo.month_id
               and notice.photo.prompt_claim.prompt_id==photo.prompt_claim.prompt_id
               and photo.participant.user_id==user.pk)
    else:valid=False
    if not valid:raise ValidationError('The new attempt does not replace this rejected target.')
    notice.resolved_at=timezone.now();notice.resolution_type='resubmitted';notice.replacement_key=f'{category}:{new_target.pk}'
    notice.save(update_fields=['resolved_at','resolution_type','replacement_key'])


def replacement_notice(user,pk,category,*,book=None,month=None):
    try:key=int(pk)
    except (TypeError,ValueError):raise PermissionDenied('Invalid replacement notice.') from None
    notice=ReaderRejectionNotice.objects.filter(pk=key,reader_id=user.pk,category=category,resolved_at__isnull=True).select_related('photo__prompt_claim','immersive_request').first()
    if notice is None or (book and notice.book_id!=book.pk) or (month and notice.month_id!=month.pk):
        raise PermissionDenied('Only the recipient may use this replacement notice.')
    return notice


def resolve_target_correction(actor,category,target,book):
    """Existing authorized correction succeeded for this Reader's exact target."""
    if actor.pk!=book.participant.user_id:return
    ReaderRejectionNotice.objects.filter(reader_id=actor.pk,category=category,
        target_key=f'{category}:{target.pk}',resolved_at__isnull=True).update(
            resolved_at=timezone.now(),resolution_type='corrected')
