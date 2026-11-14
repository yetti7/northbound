"""Scoped, atomic corrections to existing Challenge registration responses."""
import json

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db import transaction

from .forms import ChallengeRegistrationForm
from .models import AuditEvent, ChallengeMonth, MonthEnrollment
from .permissions import can_correct_registration_answers

SALT = "northbound.registration-answer-correction"


def correction_state(enrollment):
    return {
        "questions": list(enrollment.month.signup_questions.order_by("pk").values(
            "id", "wording", "question_type", "is_required", "choices",
        )),
        "answers": list(enrollment.signup_answers.order_by("pk").values("id", "question_id", "value", "updated_at")),
    }


class RegistrationAnswerCorrectionForm(ChallengeRegistrationForm):
    def __init__(self, *args, enrollment, actor, **kwargs):
        from django import forms
        super().__init__(*args, month=enrollment.month, profile=None,
                         enrollment=enrollment, include_discord=False, **kwargs)
        self.actor = actor
        self.state = correction_state(enrollment)
        existing_ids = {row["question_id"] for row in self.state["answers"]}
        self.questions = [question for question in self.questions if question.pk in existing_ids]
        self.fields = {f"question_{question.pk}": self.fields[f"question_{question.pk}"] for question in self.questions}
        self.fields["version"] = forms.CharField(widget=forms.HiddenInput())
        self.expected = {"actor": actor.pk, "enrollment": enrollment.pk,
                         "state": json.loads(json.dumps(self.state, default=str))}
        self.initial["version"] = signing.dumps(self.expected, salt=SALT)

    def clean(self):
        from django import forms
        data = super().clean()
        if set(self.data) - set(self.fields) - {"csrfmiddlewaretoken"}:
            raise forms.ValidationError("Only existing registration answers may be corrected.")
        try:
            supplied = signing.loads(data.get("version", ""), salt=SALT, max_age=1800)
        except (signing.BadSignature, TypeError):
            raise forms.ValidationError("This correction expired. Reload the registration before saving.") from None
        if supplied != self.expected:
            raise forms.ValidationError("The registration answers changed. Reload before saving.")
        return data


@transaction.atomic
def correct_registration_answers(*, enrollment, actor, data):
    month = ChallengeMonth.objects.select_for_update().get(pk=enrollment.month_id)
    enrollment = MonthEnrollment.objects.select_for_update().select_related("participant__user").get(
        pk=enrollment.pk, month=month,
    )
    enrollment.month = month
    actor = get_user_model().objects.get(pk=actor.pk)
    if not can_correct_registration_answers(actor, enrollment):
        raise PermissionDenied("Current registration correction authority and an editable Challenge are required.")
    answers = {answer.question_id: answer for answer in enrollment.signup_answers.select_for_update()}
    form = RegistrationAnswerCorrectionForm(data, enrollment=enrollment, actor=actor)
    if not form.is_valid():
        return form, False
    changed = []
    for question in form.questions:
        answer = answers[question.pk]
        value = form.answer_value(question)
        if answer.value == value:
            continue
        answer.value = value
        answer.save(update_fields=["value", "updated_at"])
        changed.append({"question_id": question.pk, "question": question.wording})
    if changed:
        AuditEvent.objects.create(
            actor=actor, group=month.group, action="registration.answers_admin_corrected",
            object_type="MonthEnrollment", object_id=str(enrollment.pk),
            summary=json.dumps({
                "challenge_id": month.pk, "reader_id": enrollment.participant_id,
                "reader_account_id": enrollment.participant.user_id,
                "enrollment_id": enrollment.pk, "changed_answers": changed,
            }, sort_keys=True),
        )
    return form, bool(changed)
