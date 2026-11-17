"""Challenge options only; use isolated participation fixtures, never live enrollments."""
from django.test import TestCase, Client
from django.urls import reverse
from . import test_phase2_participation as fixtures
from .models import MonthEnrollment, AuditEvent


class ChallengeOptionsTests(TestCase):
    setUp = fixtures.SelfRegistrationAndParticipationTests.setUp
    make_month = fixtures.SelfRegistrationAndParticipationTests.make_month

    def enroll(self, month=None, participant=None):
        return MonthEnrollment.objects.create(month=month or self.active,participant=participant or self.reader)

    def withdraw_url(self, month=None):
        return reverse('challenge-withdraw',args=[self.group.slug,(month or self.active).pk])

    def test_gear_relocates_only_withdraw_and_keeps_submit(self):
        self.enroll();self.client.force_login(self.reader_user)
        response=self.client.get(self.active.get_absolute_url())
        self.assertContains(response,'data-challenge-options')
        self.assertContains(response,'aria-label="Challenge options"')
        self.assertContains(response,'aria-expanded="false"')
        self.assertContains(response,'Withdraw from Challenge')
        body=response.content.decode()
        actions=body.split('class="action-row challenge-identity-actions"',1)[1].split('</div>',1)[0]
        self.assertNotIn(self.withdraw_url(),actions)
        self.assertIn('Submit Book',actions)
        self.assertContains(response,'href="'+self.withdraw_url()+'"')

    def test_no_gear_without_enrollment_or_for_platform_administrator(self):
        for actor in (self.reader_user,self.platform_owner):
            self.client.force_login(actor)
            self.assertNotContains(self.client.get(self.active.get_absolute_url()),'data-challenge-options')

    def test_no_gear_after_withdrawal_or_when_lifecycle_is_closed(self):
        self.enroll(self.finalizing)
        self.client.force_login(self.reader_user)
        for month in (self.finalizing,self.completed,self.archived):
            self.assertNotContains(self.client.get(month.get_absolute_url()),'data-challenge-options')
        enrollment=self.enroll();enrollment.is_active=False;enrollment.inactive_reason='withdrawn';enrollment.save()
        self.assertNotContains(self.client.get(self.active.get_absolute_url()),'data-challenge-options')

    def test_confirmation_and_cancel_are_readonly_and_identify_challenge(self):
        enrollment=self.enroll();self.client.force_login(self.reader_user)
        before=list(AuditEvent.objects.values())
        response=self.client.get(self.withdraw_url())
        self.assertContains(response,'Withdraw from '+self.active.name+'?')
        self.assertContains(response,'method="post"')
        self.assertContains(response,'csrfmiddlewaretoken')
        self.assertContains(response,'Cancel')
        self.assertContains(response,'href="'+self.active.get_absolute_url()+'"')
        self.client.get(self.active.get_absolute_url())  # Cancel navigation.
        enrollment.refresh_from_db();self.assertTrue(enrollment.is_active)
        self.assertEqual(before,list(AuditEvent.objects.values()))

    def test_final_post_uses_existing_withdrawal_and_audit(self):
        enrollment=self.enroll();self.client.force_login(self.reader_user)
        self.assertRedirects(self.client.post(self.withdraw_url()),self.active.get_absolute_url())
        enrollment.refresh_from_db();self.assertFalse(enrollment.is_active)
        self.assertEqual(enrollment.inactive_reason,'withdrawn')
        self.assertTrue(AuditEvent.objects.filter(action='participation.self_withdrew',actor=self.reader_user).exists())

    def test_csrf_and_other_users_cannot_withdraw_someone_else(self):
        enrollment=self.enroll()
        csrf=Client(enforce_csrf_checks=True);csrf.force_login(self.reader_user)
        self.assertEqual(csrf.post(self.withdraw_url()).status_code,403)
        self.client.force_login(self.other_user)
        self.assertEqual(self.client.post(self.withdraw_url()).status_code,404)
        self.client.force_login(self.platform_owner)
        self.assertEqual(self.client.post(self.withdraw_url()).status_code,403)
        enrollment.refresh_from_db();self.assertTrue(enrollment.is_active)

    def test_staff_primary_actions_are_preserved(self):
        self.enroll(participant=self.host);self.client.force_login(self.host_user)
        response=self.client.get(self.active.get_absolute_url())
        for label in ('Submit Book','Review Queue','Challenge Settings','Withdraw from Challenge'):
            self.assertContains(response,label)

    def test_registration_closed_does_not_remove_existing_withdrawal_eligibility(self):
        self.enroll(self.closed);self.client.force_login(self.reader_user)
        self.assertContains(self.client.get(self.closed.get_absolute_url()),'data-challenge-options')
