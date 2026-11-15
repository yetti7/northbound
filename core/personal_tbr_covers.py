"""Owner-only artwork overrides; locked selection evidence is never saved."""
import json
from django import forms
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction, DatabaseError
from .branding import image_asset
from .models import AuditEvent, ChallengeMonth, MonthEnrollment, PersonalTBR, PersonalTBRBook
from .permissions import can_view_challenge

SALT='northbound.personal-tbr-cover.v1'
RELATED=('personal_tbr__enrollment__month__group','personal_tbr__enrollment__participant__user')


def load(pk):
    return PersonalTBRBook.objects.select_related(*RELATED).get(pk=pk)


def can_manage(actor, book):
    enrollment=book.personal_tbr.enrollment
    return bool(actor.is_authenticated and actor.is_active and not actor.is_superuser
        and enrollment.participant.user_id==actor.pk and enrollment.participant.is_active
        and enrollment.participant.group_id==enrollment.month.group_id
        and enrollment.is_active and enrollment.month.group.is_active
        and enrollment.month.status in {'draft','upcoming','active'}
        and can_view_challenge(actor,enrollment.month))


def _state(book, actor):
    return {'actor':actor.pk,'entry':book.pk,'tbr':book.personal_tbr_id,
        'enrollment':book.personal_tbr.enrollment_id,'month':book.personal_tbr.enrollment.month_id,
        'group':book.personal_tbr.enrollment.month.group_id,'participant':book.personal_tbr.enrollment.participant_id,
        'cover':book.custom_cover.name,
        'identity':[book.position,book.title_snapshot,book.author_snapshot,book.catalog_book_id,book.catalog_edition_id]}


def cover_version(book, actor):
    return signing.dumps(_state(book,actor),salt=SALT)


class CoverForm(forms.Form):
    version=forms.CharField(widget=forms.HiddenInput())
    artwork=forms.FileField(required=False,label='Custom cover',
        widget=forms.FileInput(attrs={'accept':'image/png,image/jpeg,image/webp'}),
        help_text='PNG, JPEG or WebP, up to 5 MB. Artwork is normalized to PNG, at most 800 × 1200 px; proportions are preserved.')


def update_cover(*, actor, book, version, upload=None, remove=False):
    """Own the transaction so a failed database write can clean up its new file."""
    if connection.in_atomic_block:
        raise RuntimeError('Cover updates require their own transaction.')
    actor=get_user_model().objects.get(pk=actor.pk)
    current=load(book.pk)
    if not can_manage(actor,current):
        raise PermissionDenied('Only the eligible Reader may manage this Personal TBR cover.')
    if bool(upload)==bool(remove):
        raise ValidationError('Choose an image upload or Remove Custom Cover.')
    asset=image_asset(upload,'cover') if upload else None
    field=PersonalTBRBook._meta.get_field('custom_cover');storage=field.storage
    saved=''
    try:
        with transaction.atomic():
            e=current.personal_tbr.enrollment
            ChallengeMonth.objects.select_for_update().get(pk=e.month_id)
            MonthEnrollment.objects.select_for_update().get(pk=e.pk)
            PersonalTBR.objects.select_for_update().get(pk=current.personal_tbr_id)
            PersonalTBRBook.objects.select_for_update().get(pk=current.pk)
            current=load(current.pk)
            actor=get_user_model().objects.get(pk=actor.pk)
            if not can_manage(actor,current):
                raise PermissionDenied('Cover authority changed. Nothing was saved.')
            try:
                expected=signing.loads(version,salt=SALT,max_age=1800)
            except (signing.BadSignature, TypeError):
                raise ValidationError('This cover form expired. Reload before trying again.') from None
            if expected!=_state(current,actor):
                raise ValidationError('The cover or selected book changed. Reload before trying again.')
            old=current.custom_cover.name
            if remove and not old:
                raise ValidationError('This entry has no custom cover to remove.')
            if asset:
                saved=storage.save(field.generate_filename(current,asset.name),asset)
            # Deliberately update only the new presentation column. The ordinary
            # model save/full_clean lock remains unchanged and forbids identity edits.
            changed=PersonalTBRBook.objects.filter(pk=current.pk,custom_cover=old).update(custom_cover=saved)
            if changed!=1:
                raise ValidationError('Another cover update won. Reload before trying again.')
            AuditEvent.objects.create(actor=actor,group=current.personal_tbr.enrollment.month.group,
                action='personal_tbr.cover_removed' if remove else 'personal_tbr.cover_replaced' if old else 'personal_tbr.cover_uploaded',
                object_type='PersonalTBRBook',object_id=str(current.pk),
                summary=json.dumps({'artwork_only':True,'before':old,'after':saved},sort_keys=True))
    except Exception:
        # Never delete previous/shared artwork. UUID names belong only to this
        # attempted operation; cleanup happens after the DB transaction rolls back.
        if saved:
            try:
                if not PersonalTBRBook.objects.filter(custom_cover=saved).exists():
                    storage.delete(saved)
            except (OSError, NotImplementedError, DatabaseError):
                pass  # An unreferenced file is safer than deleting a live asset.
        raise
    return load(current.pk)
