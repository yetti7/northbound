"""Agreement configuration and signed, immutable registration acceptance."""
import hashlib
import json

from django import forms
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from .models import ChallengeMonth, MonthEnrollment, RegistrationAgreementAcceptance, AuditEvent
from .permissions import can_manage_months, can_operate_challenge

SALT = 'northbound.registration-agreement.v1'


def agreement_version(month):
    return hashlib.sha256(month.registration_agreement_text.encode('utf-8')).hexdigest()


def agreement_token(month, actor):
    return signing.dumps({'month': month.pk, 'actor': actor.pk, 'version': agreement_version(month)}, salt=SALT)


def validate_acceptance(*, month, actor, accepted, version):
    if accepted is not True:
        raise ValidationError('Read and agree to the Challenge Agreement before completing registration.')
    try:
        if not isinstance(version, str):
            raise signing.BadSignature('Missing agreement version')
        payload = signing.loads(version, salt=SALT, max_age=86400)
    except (signing.BadSignature, TypeError):
        raise ValidationError('The agreement changed or the form expired. Review the current agreement and check the box again.') from None
    if payload != {'month': month.pk, 'actor': actor.pk, 'version': agreement_version(month)}:
        raise ValidationError('The agreement changed. Review the current agreement and check the box again.')


class AgreementAcceptanceForm(forms.Form):
    agreement_version = forms.CharField(widget=forms.HiddenInput())
    agreement_accepted = forms.BooleanField(label='I have read and agree to the terms above.',
        error_messages={'required': 'You must read and agree to the terms above before registering.'})

    def __init__(self, *args, month, actor, existing=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.month, self.actor = month, actor
        self.required_agreement = month.registration_agreement_enabled and not existing
        if not self.required_agreement:
            self.fields.clear()
        else:
            self.initial['agreement_version'] = agreement_token(month, actor)

    def clean(self):
        values = super().clean()
        if self.required_agreement and 'agreement_accepted' in values:
            try:
                validate_acceptance(month=self.month, actor=self.actor,
                    accepted=values['agreement_accepted'], version=values.get('agreement_version'))
            except ValidationError as exc:
                self.add_error(None, exc)
                # Render current wording with a fresh token and unchecked checkbox.
                self.data = self.data.copy()
                self.data['agreement_version'] = agreement_token(self.month, self.actor)
                self.data['agreement_accepted'] = ''
        return values


class AgreementSettingsForm(forms.ModelForm):
    class Meta:
        model = ChallengeMonth
        fields = ('registration_agreement_enabled', 'registration_agreement_text')
        labels = {'registration_agreement_enabled': 'Enable Registration Agreement', 'registration_agreement_text': 'Agreement Text'}
        widgets = {'registration_agreement_text': forms.Textarea(attrs={'rows': 6})}

    def clean(self):
        values = super().clean()
        if values.get('registration_agreement_enabled') and not values.get('registration_agreement_text', '').strip():
            self.add_error('registration_agreement_text', 'Enter agreement text before enabling the agreement.')
        return values


@transaction.atomic
def configure_agreement(*, month, actor, enabled, text):
    month = ChallengeMonth.objects.select_for_update().select_related('group').get(pk=month.pk)
    if not actor.is_authenticated or not get_user_model().objects.filter(pk=actor.pk, is_active=True).exists() or not (
        can_operate_challenge(actor, month) or can_manage_months(actor, month.group)):
        raise PermissionDenied('Challenge registration configuration authority is required.')
    if month.status not in {'draft', 'upcoming', 'active'}:
        raise ValidationError('Registration configuration is read-only in this Challenge stage.')
    if type(enabled) is not bool or not isinstance(text, str) or (enabled and not text.strip()):
        raise ValidationError('Enabled agreements require agreement text.')
    before = {'enabled': month.registration_agreement_enabled, 'text': month.registration_agreement_text}
    after = {'enabled': enabled, 'text': text}
    if before != after:
        month.registration_agreement_enabled, month.registration_agreement_text = enabled, text
        month.save(update_fields=['registration_agreement_enabled', 'registration_agreement_text'])
        AuditEvent.objects.create(actor=actor, group=month.group, action='registration.agreement_configured',
            object_type='ChallengeMonth', object_id=str(month.pk), summary=json.dumps({'before': before, 'after': after}))
    return month


@transaction.atomic
def record_acceptance(*, enrollment, actor, accepted, version):
    """Called by the participation service within its creation transaction."""
    if not actor.is_authenticated or not actor.is_active or actor.pk != enrollment.participant.user_id:
        raise PermissionDenied('Only the registering Reader may accept the agreement.')
    month = enrollment.month
    if not month.registration_agreement_enabled:
        raise ValidationError('Registration Agreement is not enabled.')
    validate_acceptance(month=month, actor=actor, accepted=accepted, version=version)
    record = RegistrationAgreementAcceptance.objects.create(month=month, reader=enrollment.participant,
        enrollment=enrollment, agreement_text=month.registration_agreement_text, agreement_version=agreement_version(month))
    AuditEvent.objects.create(actor=actor, group=month.group, action='registration.agreement_accepted',
        object_type='RegistrationAgreementAcceptance', object_id=str(record.pk),
        summary=json.dumps({'enrollment_id': enrollment.pk, 'version': record.agreement_version}))
    return record
