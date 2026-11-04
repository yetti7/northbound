from unittest.mock import patch
from datetime import date

from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from . import test_challenge_registration as fixture
from .models import (ChallengeMonth, MonthEnrollment, RegistrationAgreementAcceptance,
    ChallengeSignupAnswer, ChallengeSignupQuestion, DEFAULT_REGISTRATION_AGREEMENT, TeamAssignment)
from .participation import activate_participation
from .registration_agreements import configure_agreement, agreement_token, agreement_version


class RegistrationAgreementTests(TestCase):
    setUp = fixture.ChallengeRegistrationTests.setUp
    register_url = fixture.ChallengeRegistrationTests.register_url
    confirm_url = fixture.ChallengeRegistrationTests.confirm_url
    settings_url = fixture.ChallengeRegistrationTests.settings_url
    detail_url = fixture.ChallengeRegistrationTests.detail_url
    question = fixture.ChallengeRegistrationTests.question

    def enable(self, text='Rules version one', enabled=True):
        self.month=configure_agreement(month=self.month,actor=self.host_user,enabled=enabled,text=text)

    def data(self, **changes):
        data={'agreement_accepted':'on','agreement_version':agreement_token(self.month,self.reader_user)}
        data.update(changes);return data

    def register(self, **changes):
        self.client.force_login(self.reader_user)
        response=self.client.post(self.register_url(),self.data(**changes))
        self.assertContains(response,'Registration Summary')
        return self.client.post(self.confirm_url())

    def test_defaults_disabled_and_default_wording(self):
        self.assertFalse(self.month.registration_agreement_enabled)
        self.assertEqual(self.month.registration_agreement_text,DEFAULT_REGISTRATION_AGREEMENT)
        self.client.force_login(self.reader_user)
        self.assertNotContains(self.client.get(self.register_url()),'Challenge Agreement')
        self.client.post(self.register_url(),{});self.client.post(self.confirm_url())
        self.assertTrue(MonthEnrollment.objects.filter(participant=self.reader).exists())
        self.assertFalse(RegistrationAgreementAcceptance.objects.exists())

    def test_checkbox_required_and_no_prechecked_acceptance(self):
        self.enable();self.client.force_login(self.reader_user)
        response=self.client.get(self.register_url())
        self.assertContains(response,'I have read and agree to the terms above.')
        self.assertNotContains(response,'checked')
        response=self.client.post(self.register_url(),self.data(agreement_accepted=''))
        self.assertIn('agreement_accepted',response.context['agreement_form'].errors)
        self.client.post(self.confirm_url())
        self.assertFalse(MonthEnrollment.objects.filter(participant=self.reader).exists())
        self.assertFalse(RegistrationAgreementAcceptance.objects.exists())

    def test_successful_acceptance_matches_exact_text_reader_and_version(self):
        text='Rule A\nRule B <strong>plain text</strong>'
        self.enable(text);self.assertEqual(self.register().status_code,302)
        record=RegistrationAgreementAcceptance.objects.get()
        self.assertEqual((record.reader,record.month,record.agreement_text,record.agreement_version),
            (self.reader,self.month,text,agreement_version(self.month)))
        self.assertIsNotNone(record.accepted_at)
        self.assertEqual(record.enrollment.participant,self.reader)
        record.agreement_text='Rewrite'
        with self.assertRaises(ValidationError):record.save()
        with self.assertRaises(ValidationError):record.delete()
        self.client.post(self.confirm_url())
        self.assertEqual(RegistrationAgreementAcceptance.objects.count(),1)

    def test_signed_acceptance_is_bound_to_reader_and_challenge(self):
        self.enable();self.client.force_login(self.reader_user)
        data=self.data(agreement_version=agreement_token(self.month,self.other_user))
        response=self.client.post(self.register_url(),data)
        self.assertContains(response,'agreement changed')
        self.client.post(self.confirm_url())
        self.assertFalse(RegistrationAgreementAcceptance.objects.exists())
        self.assertFalse(MonthEnrollment.objects.filter(participant=self.reader).exists())

    def test_settings_are_independent_of_question_locks(self):
        question=self.question('Locked question','short_text')
        enrollment=MonthEnrollment.objects.create(month=self.month,participant=self.reader)
        ChallengeSignupAnswer.objects.create(enrollment=enrollment,question=question,value='Existing answer')
        self.client.force_login(self.host_user)
        response=self.client.post(self.settings_url(),{'action':'save_agreement_settings',
            'agreement-registration_agreement_enabled':'on','agreement-registration_agreement_text':'Revised rules'})
        self.assertEqual(response.status_code,302)
        self.month.refresh_from_db();self.assertTrue(self.month.registration_agreement_enabled)
        self.assertContains(self.client.get(self.settings_url()),'Registration configuration locked')
        self.assertContains(self.client.get(self.settings_url()),'Save Registration Agreement')
        question.refresh_from_db();self.assertEqual(question.wording,'Locked question')
        self.assertEqual(ChallengeSignupAnswer.objects.get().value,'Existing answer')

    def test_existing_registration_edit_and_reactivation_need_no_new_acceptance(self):
        enrollment=MonthEnrollment.objects.create(month=self.month,participant=self.reader,origin='self')
        self.enable();self.client.force_login(self.reader_user)
        response=self.client.get(reverse('challenge-registration-edit',args=[self.group.slug,self.month.pk]))
        self.assertNotContains(response,'Challenge Agreement')
        self.assertEqual(self.client.get(self.register_url()).status_code,302)
        enrollment.is_active=False;enrollment.inactive_reason='withdrawn';enrollment.save()
        self.client.post(self.register_url(),{});self.client.post(self.confirm_url())
        enrollment.refresh_from_db();self.assertTrue(enrollment.is_active)
        self.assertFalse(RegistrationAgreementAcceptance.objects.exists())

    def test_edit_preserves_old_acceptance_and_new_reader_gets_current_text(self):
        self.enable();self.register();record=RegistrationAgreementAcceptance.objects.get()
        self.enable('New wording')
        record.refresh_from_db();self.assertEqual(record.agreement_text,'Rules version one')
        self.client.force_login(self.other_user)
        self.assertContains(self.client.get(self.register_url()),'New wording')
        data={'agreement_accepted':'on','agreement_version':agreement_token(self.month,self.other_user)}
        self.client.post(self.register_url(),data);self.client.post(self.confirm_url())
        self.assertEqual(RegistrationAgreementAcceptance.objects.get(reader__user=self.other_user).agreement_text,'New wording')

    def test_stale_form_and_stale_confirmation_do_not_record_unseen_terms(self):
        self.enable();data=self.data();self.enable('Changed before review')
        self.client.force_login(self.reader_user)
        response=self.client.post(self.register_url(),data)
        self.assertContains(response,'agreement changed')
        self.assertFalse(response.context['agreement_form']['agreement_accepted'].value())
        self.assertFalse(MonthEnrollment.objects.filter(participant=self.reader).exists())
        self.client.post(self.register_url(),self.data())
        self.enable('Changed before final confirmation')
        self.client.post(self.confirm_url())
        self.assertFalse(RegistrationAgreementAcceptance.objects.exists())
        self.assertFalse(MonthEnrollment.objects.filter(participant=self.reader).exists())

    def test_enabling_after_draft_requires_new_review_and_disabling_needs_no_acceptance(self):
        self.client.force_login(self.reader_user);self.client.post(self.register_url(),{})
        self.enable();self.client.post(self.confirm_url())
        self.assertFalse(MonthEnrollment.objects.filter(participant=self.reader).exists())
        self.enable(enabled=False)
        self.client.post(self.register_url(),{});self.client.post(self.confirm_url())
        self.assertTrue(MonthEnrollment.objects.filter(participant=self.reader).exists())
        self.assertFalse(RegistrationAgreementAcceptance.objects.exists())

    def test_failure_after_acceptance_rolls_back_enrollment_answers_and_acceptance(self):
        self.enable();self.client.force_login(self.reader_user)
        self.client.post(self.register_url(),self.data())
        with patch('core.forms.ChallengeRegistrationForm.save_answers',side_effect=ValidationError('Synthetic failure')):
            self.client.post(self.confirm_url())
        self.assertFalse(RegistrationAgreementAcceptance.objects.exists())
        self.assertFalse(MonthEnrollment.objects.filter(participant=self.reader).exists())

    def test_staff_visibility_and_html_escaping(self):
        self.enable('<script>alert("x")</script>')
        self.client.force_login(self.reader_user)
        self.assertContains(self.client.get(self.register_url()), '&lt;script&gt;')
        self.assertNotContains(self.client.get(self.register_url()), '<script>alert(')
        self.register()
        enrollment=MonthEnrollment.objects.get(month=self.month,participant=self.reader)
        self.client.force_login(self.host_user);response=self.client.get(self.detail_url(enrollment))
        self.assertContains(response,'Accepted');self.assertContains(response,'Version')
        self.assertContains(response,'&lt;script&gt;');self.assertNotContains(response,'<script>alert(')
        for user in (self.reader_user,self.floater_user,self.leader_user):
            self.client.force_login(user);self.assertEqual(self.client.get(self.detail_url(enrollment)).status_code,403)
        self.client.force_login(self.host_user)
        legacy=MonthEnrollment.objects.create(month=self.month,participant=self.other)
        self.assertContains(self.client.get(self.detail_url(legacy)),'Not recorded')

    def test_alternate_staff_enrollment_and_team_assignment_cannot_bypass(self):
        self.enable()
        with self.assertRaises(ValidationError):
            activate_participation(month=self.month,participant=self.reader,actor=self.host_user,origin='staff')
        self.client.force_login(self.host_user)
        for name,payload in [('month-participant-add',{'participant':self.reader.pk}),('team-assignment-create',{'participant':self.reader.pk,'team':self.team.pk})]:
            response=self.client.post(reverse(name,args=[self.group.slug,self.month.pk]),payload)
            self.assertEqual(response.status_code,200)
            self.assertContains(response,'must self-register')
        self.assertFalse(MonthEnrollment.objects.filter(participant=self.reader).exists())
        self.assertFalse(TeamAssignment.objects.filter(participant=self.reader).exists())
        self.register();self.client.force_login(self.host_user)
        self.assertEqual(self.client.post(reverse('team-assignment-create',args=[self.group.slug,self.month.pk]),{'participant':self.reader.pk,'team':self.team.pk}).status_code,302)

    def test_deadlines_lifecycle_permissions_and_csrf(self):
        self.enable();self.client.force_login(self.reader_user)
        self.client.post(self.register_url(),self.data())
        ChallengeMonth.objects.filter(pk=self.month.pk).update(registration_is_open=False)
        self.client.post(self.confirm_url());self.assertFalse(RegistrationAgreementAcceptance.objects.exists())
        self.assertEqual(self.client.post(self.settings_url(),{'action':'save_agreement_settings'}).status_code,403)
        csrf=Client(enforce_csrf_checks=True);csrf.force_login(self.reader_user)
        self.assertEqual(csrf.post(self.register_url(),self.data()).status_code,403)
        ChallengeMonth.objects.filter(pk=self.month.pk).update(status='completed')
        self.client.force_login(self.host_user)
        self.assertEqual(self.client.post(self.settings_url(),{'action':'save_agreement_settings'}).status_code,403)

    def test_questions_are_separate_and_required_answers_still_apply(self):
        question=self.question('Required','short_text',required=True)
        self.enable();self.client.force_login(self.reader_user)
        response=self.client.post(self.register_url(),self.data())
        self.assertIn(f'question_{question.pk}',response.context['form'].errors)
        self.assertEqual(self.month.signup_questions.count(),1)
        self.register(**{f'question_{question.pk}':'Answer'})
        self.assertEqual(ChallengeSignupAnswer.objects.get().value,'Answer')
        self.assertEqual(RegistrationAgreementAcceptance.objects.count(),1)


class RegistrationAgreementMigrationTests(TransactionTestCase):
    def test_sqlite_existing_registration_answers_preserved_without_backfill(self):
        executor=MigrationExecutor(connection)
        executor.migrate([('core','0063_immersive_reading')])
        try:
            apps=executor.loader.project_state([('core','0063_immersive_reading')]).apps
            user=apps.get_model('auth','User').objects.create(username='agreement-old')
            group=apps.get_model('core','ReadingGroup').objects.create(name='Old',slug='old')
            reader=apps.get_model('core','Membership').objects.create(group=group,user=user,display_name='Old Reader')
            month=apps.get_model('core','ChallengeMonth').objects.create(group=group,name='Old')
            question=apps.get_model('core','ChallengeSignupQuestion').objects.create(month=month,wording='Existing question',question_type='short_text',position=1)
            enrollment=apps.get_model('core','MonthEnrollment').objects.create(month=month,participant=reader)
            answer=apps.get_model('core','ChallengeSignupAnswer').objects.create(enrollment=enrollment,question=question,value='Historical response')
            answer_before=apps.get_model('core','ChallengeSignupAnswer').objects.values().get(pk=answer.pk)
            before=apps.get_model('core','MonthEnrollment').objects.values().get(pk=enrollment.pk)
            MigrationExecutor(connection).migrate([('core','0064_registration_agreements')])
            new=ChallengeMonth.objects.get(pk=month.pk)
            self.assertFalse(new.registration_agreement_enabled)
            self.assertEqual(new.registration_agreement_text,DEFAULT_REGISTRATION_AGREEMENT)
            self.assertEqual(MonthEnrollment.objects.values().get(pk=enrollment.pk),before)
            self.assertFalse(RegistrationAgreementAcceptance.objects.exists())
            self.assertEqual(ChallengeSignupAnswer.objects.values().get(pk=answer.pk),answer_before)
        finally:
            MigrationExecutor(connection).migrate([('core','0064_registration_agreements')])
