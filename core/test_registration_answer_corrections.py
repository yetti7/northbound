import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import test_challenge_registration as fixture
from .models import (
    AuditEvent, BookSubmission, ChallengeMonth, ChallengeSignupAnswer, ChallengeStaffAssignment,
    Membership, ModifierProvenance, MonthEnrollment, ReadingGroup, Team, TeamAssignment, UserProfile,
)
from .permissions import can_correct_registration_answers
from .registration_answers import RegistrationAnswerCorrectionForm, correct_registration_answers


class RegistrationAnswerCorrectionTests(TestCase):
    setUp = fixture.ChallengeRegistrationTests.setUp
    question = fixture.ChallengeRegistrationTests.question
    detail_url = fixture.ChallengeRegistrationTests.detail_url

    def prepare(self):
        self.text = self.question("Reading goal", "short_text", True, position=1)
        self.number = self.question("Optional number", "number", position=2)
        self.choice = self.question("Reading format", "single_choice", choices=["Print", "Audio"], position=3)
        self.multi = self.question("Preferences", "multiple_choice", choices=["Fantasy", "Mystery"], position=4)
        self.missing = self.question("No saved answer", "short_text", position=5)
        self.enrollment = MonthEnrollment.objects.create(month=self.month, participant=self.reader)
        for question,value in ((self.text,"Original private value"),(self.number,""),(self.choice,"Print"),(self.multi,["Fantasy"])):
            ChallengeSignupAnswer.objects.create(enrollment=self.enrollment,question=question,value=value)
        TeamAssignment.objects.create(month=self.month,participant=self.reader,team=self.team)
        self.book = BookSubmission.objects.create(month=self.month, participant=self.reader, title="Book", author="Author",
            book_format="ebook", completed_on=self.month.starts_on, submitted_pages=100, approved_pages=100, status="approved")

    def payload(self, actor, **changes):
        form = RegistrationAnswerCorrectionForm(enrollment=self.enrollment, actor=actor)
        return {
            "version": form["version"].value(), f"question_{self.text.pk}": "Corrected private value",
            f"question_{self.number.pk}": "", f"question_{self.choice.pk}": "Audio",
            f"question_{self.multi.pk}": ["Fantasy","Mystery"], **changes,
        }

    def test_authorized_roles_open_closed_and_no_capability_delegation_needed(self):
        self.prepare()
        self.moderator.permission_overrides = {}
        self.moderator.save(update_fields=["permission_overrides"])
        for actor in (self.platform_owner,self.owner_user,self.moderator_user,self.host_user):
            for opened in (True,False):
                with self.subTest(role=actor.pk,registration_open=opened):
                    ChallengeMonth.objects.filter(pk=self.month.pk).update(registration_is_open=opened)
                    self.enrollment.refresh_from_db()
                    self.client.force_login(actor)
                    self.assertContains(self.client.get(self.detail_url(self.enrollment)),"Save Corrections")
                    response=self.client.post(self.detail_url(self.enrollment),self.payload(actor))
                    self.assertEqual(response.status_code,302)
                    self.assertEqual(ChallengeSignupAnswer.objects.get(enrollment=self.enrollment,question=self.text).value,"Corrected private value")
                    self.assertEqual(ChallengeMonth.objects.get(pk=self.month.pk).registration_is_open,opened)

    def test_all_precompletion_stages_and_completed_archived_lock_at_endpoint_and_service(self):
        self.prepare()
        for status in ("draft","upcoming","active","finalizing","completed","archived"):
            ChallengeMonth.objects.filter(pk=self.month.pk).update(status=status)
            self.enrollment.refresh_from_db()
            self.client.force_login(self.platform_owner)
            page=self.client.get(self.detail_url(self.enrollment))
            self.assertEqual(page.status_code,200)
            if status in ("completed","archived"):
                self.assertNotContains(page,"Save Corrections")
                before=list(self.enrollment.signup_answers.values())
                self.assertEqual(self.client.post(self.detail_url(self.enrollment),self.payload(self.platform_owner)).status_code,403)
                with self.assertRaises(PermissionDenied):
                    correct_registration_answers(enrollment=self.enrollment,actor=self.platform_owner,data=self.payload(self.platform_owner))
                self.assertEqual(list(self.enrollment.signup_answers.values()),before)
            else:
                self.assertContains(page,"Save Corrections")
                self.assertEqual(self.client.post(self.detail_url(self.enrollment),self.payload(self.platform_owner)).status_code,302)

    def test_team_leader_floater_reader_and_capability_only_member_cannot_correct(self):
        self.prepare()
        MonthEnrollment.objects.create(month=self.month,participant=self.leader)
        TeamAssignment.objects.create(month=self.month,participant=self.leader,team=self.team)
        ChallengeStaffAssignment.objects.create(month=self.month,membership=self.leader,team=self.team,role="team_leader")
        self.other.permission_overrides={"manage_months":True}
        self.other.save(update_fields=["permission_overrides"])
        for actor in (self.reader_user,self.other_user,self.floater_user,self.leader_user):
            with self.subTest(actor=actor.pk):
                self.client.force_login(actor)
                self.assertEqual(self.client.post(self.detail_url(self.enrollment),self.payload(actor)).status_code,403)
                with self.assertRaises(PermissionDenied):
                    correct_registration_answers(enrollment=self.enrollment,actor=actor,data=self.payload(actor))
        self.assertEqual(self.enrollment.signup_answers.get(question=self.text).value,"Original private value")

    def test_cross_group_host_scope_and_participating_host_cannot_bypass_as_owner(self):
        self.prepare()
        outsider=get_user_model().objects.create_user("other-owner")
        other_group=ReadingGroup.objects.create(name="Other",slug="registration-other-group")
        Membership.objects.create(group=other_group,user=outsider,role="owner",display_name="Other")
        for actor in (outsider,):
            self.client.force_login(actor)
            self.assertEqual(self.client.post(self.detail_url(self.enrollment),self.payload(actor)).status_code,403)
        # Group administration and Host participation remain distinct.
        self.host.role="owner"
        self.host.save(update_fields=["role"])
        MonthEnrollment.objects.create(month=self.month,participant=self.host)
        other_team=Team.objects.create(month=self.month,name="Host team")
        assignment=TeamAssignment.objects.create(month=self.month,participant=self.host,team=other_team)
        self.client.force_login(self.host_user)
        self.assertEqual(self.client.get(self.detail_url(self.enrollment)).status_code,403)
        self.assertEqual(self.client.post(self.detail_url(self.enrollment),self.payload(self.host_user)).status_code,403)
        assignment.ended_at=timezone.now()
        assignment.save(update_fields=["ended_at"])
        TeamAssignment.objects.create(month=self.month,participant=self.host,team=self.team)
        self.assertEqual(self.client.post(self.detail_url(self.enrollment),self.payload(self.host_user)).status_code,302)
        wrong_url=reverse("challenge-registration-detail",args=[other_group.slug,self.month.pk,self.enrollment.pk])
        self.assertEqual(self.client.get(wrong_url).status_code,404)

    def test_audit_tracks_changed_questions_and_preserves_enrollment_team_and_scores(self):
        self.prepare()
        self.client.force_login(self.host_user)
        self.client.get(self.detail_url(self.enrollment))
        before={m.__name__:list(m.objects.values()) for m in (
            MonthEnrollment,TeamAssignment,BookSubmission,ModifierProvenance,ChallengeMonth,UserProfile,ChallengeStaffAssignment)}
        answer_ids=list(self.enrollment.signup_answers.order_by("pk").values_list("id",flat=True))
        self.client.force_login(self.host_user)
        self.assertEqual(self.client.post(self.detail_url(self.enrollment),self.payload(self.host_user)).status_code,302)
        for model in (MonthEnrollment,TeamAssignment,BookSubmission,ModifierProvenance,ChallengeMonth,UserProfile,ChallengeStaffAssignment):
            self.assertEqual(list(model.objects.values()),before[model.__name__])
        self.assertEqual(list(self.enrollment.signup_answers.order_by("pk").values_list("id",flat=True)),answer_ids)
        self.assertFalse(self.enrollment.signup_answers.filter(question=self.missing).exists())
        event=AuditEvent.objects.get(action="registration.answers_admin_corrected")
        data=json.loads(event.summary)
        self.assertEqual(event.actor_id,self.host_user.pk)
        self.assertEqual(event.group_id,self.group.pk)
        self.assertEqual(data["reader_account_id"],self.reader_user.pk)
        self.assertEqual(data["challenge_id"],self.month.pk)
        self.assertEqual({row["question_id"] for row in data["changed_answers"]},{self.text.pk,self.choice.pk,self.multi.pk})
        self.assertNotIn("Original private value",event.summary)
        self.assertNotIn("Corrected private value",event.summary)

    def test_noop_does_not_touch_answers_or_create_audit(self):
        self.prepare()
        data=self.payload(self.owner_user,**{f"question_{self.text.pk}":"Original private value",
            f"question_{self.choice.pk}":"Print",f"question_{self.multi.pk}":["Fantasy"]})
        before=list(self.enrollment.signup_answers.values())
        form,changed=correct_registration_answers(enrollment=self.enrollment,actor=self.owner_user,data=data)
        self.assertFalse(form.errors)
        self.assertFalse(changed)
        self.assertEqual(list(self.enrollment.signup_answers.values()),before)
        self.assertFalse(AuditEvent.objects.filter(action="registration.answers_admin_corrected").exists())

    def test_invalid_definitions_and_crafted_nonanswer_fields_are_rejected(self):
        self.prepare()
        for invalid in ({f"question_{self.text.pk}":""},{f"question_{self.number.pk}":"not numeric"},
                        {f"question_{self.choice.pk}":"Other"},{f"question_{self.multi.pk}":["Invalid"]},
                        {f"question_{self.missing.pk}":"Create missing answer"},{"is_active":"true"},
                        {"discord_username":"Changed"},{"approved_pages":"999"}):
            with self.subTest(invalid=invalid):
                before=list(self.enrollment.signup_answers.values())
                form,changed=correct_registration_answers(enrollment=self.enrollment,actor=self.platform_owner,
                                                          data=self.payload(self.platform_owner,**invalid))
                self.assertTrue(form.errors)
                self.assertFalse(changed)
                self.assertEqual(list(self.enrollment.signup_answers.values()),before)

    def test_stale_tampered_actor_bound_tokens_and_revoked_authority(self):
        self.prepare()
        stale=self.payload(self.host_user)
        answer=self.enrollment.signup_answers.get(question=self.text)
        answer.value="Reader changed this"
        answer.save(update_fields=["value","updated_at"])
        for data,actor in ((stale,self.host_user),(self.payload(self.host_user,version="tampered"),self.host_user),
                           (self.payload(self.owner_user),self.host_user)):
            form,changed=correct_registration_answers(enrollment=self.enrollment,actor=actor,data=data)
            self.assertTrue(form.errors)
            self.assertFalse(changed)
        self.host.is_active=False
        self.host.save(update_fields=["is_active"])
        with self.assertRaises(PermissionDenied):
            correct_registration_answers(enrollment=self.enrollment,actor=self.host_user,data=self.payload(self.host_user))

    def test_audit_failure_rolls_back_all_answer_updates(self):
        self.prepare()
        before=list(self.enrollment.signup_answers.values())
        with patch("core.registration_answers.AuditEvent.objects.create",side_effect=RuntimeError("audit failed")):
            with self.assertRaises(RuntimeError):
                correct_registration_answers(enrollment=self.enrollment,actor=self.owner_user,data=self.payload(self.owner_user))
        self.assertEqual(list(self.enrollment.signup_answers.values()),before)

    def test_participants_links_are_scoped_without_expanding_planning_visibility(self):
        self.prepare()
        self.moderator.permission_overrides = {}
        self.moderator.save(update_fields=["permission_overrides"])
        self.client.force_login(self.moderator_user)
        roster = reverse("month-participant-list", args=[self.group.slug, self.month.pk])
        self.assertContains(self.client.get(roster), self.detail_url(self.enrollment))
        self.client.force_login(self.reader_user)
        self.assertNotContains(self.client.get(roster), "Correct Registration Answers")
        self.client.force_login(self.host_user)
        MonthEnrollment.objects.create(month=self.month, participant=self.host)
        other_team = Team.objects.create(month=self.month, name="Other correction team")
        TeamAssignment.objects.create(month=self.month, participant=self.host, team=other_team)
        self.assertNotContains(self.client.get(roster), self.detail_url(self.enrollment))
