"""Percentage awards, legacy compatibility and additive migration preservation."""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from .forms import MonthThemeForm
from .models import (BookSubmission, ChallengeMonth, ChallengeStaffAssignment, Membership,
                     ModifierProvenance, MonthEnrollment, MonthTheme, ProgressCheckpoint,
                     ProgressCheckpointResult, ReadingGroup, Team, TeamAssignment, ThemeClaim)
from .recovery import RecoveryRequest
from .recovery_scoring import correct_unused_theme, recover_theme_claim, set_submission_removed
from .score_aggregation import challenge_score_totals
from .scoring import apply_submission_review, percentage_bonus_pages, refresh_submission_score


class PercentageThemeTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('percentage-host')
        self.root = get_user_model().objects.create_superuser('percentage-root', password='owner-password')
        self.group = ReadingGroup.objects.create(name='Percent Group', slug='percent')
        self.member = Membership.objects.create(group=self.group, user=self.user, display_name='Host')
        self.month = ChallengeMonth.objects.create(group=self.group, name='November',
            starts_on=date(2026, 11, 1), ends_on=date(2026, 11, 30), status='active')
        MonthEnrollment.objects.create(month=self.month, participant=self.member)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.member, role='host')
        self.team = Team.objects.create(month=self.month, name='Cedar')
        TeamAssignment.objects.create(month=self.month, participant=self.member, team=self.team)
        self.submission = BookSubmission.objects.create(month=self.month, participant=self.member,
            title='Book', author='Writer', book_format='ebook', completed_on=date(2026, 11, 10),
            submitted_pages=900)

    def theme(self, rate=25, **kwargs):
        return MonthTheme.objects.create(month=self.month, name=f'Theme {self.month.themes.count()}',
            starts_on=self.month.starts_on, ends_on=self.month.ends_on, bonus_percentage=rate, **kwargs)

    def review(self, pages=400, status='approved', decisions=None):
        self.submission.refresh_from_db()
        self.submission.approved_pages = pages
        self.submission.status = status
        claims = []
        for pk, decision in (decisions or {}).items():
            c = ThemeClaim.objects.get(pk=pk)
            c.status = decision
            claims.append(c)
        apply_submission_review(self.submission, claims, self.user, timezone.now())
        self.submission.refresh_from_db()

    def request(self, target, action):
        return RecoveryRequest(actor=self.root, tier=2, action=action,
            target_type=type(target).__name__, target_id=str(target.pk), target_label=str(target),
            reason='Correct the authorized training record', group=self.group, challenge=self.month)

    def test_configuration_defaults_validation_and_forms(self):
        t = self.theme(0)
        self.assertEqual((t.bonus_rule, t.bonus_percentage), ('percentage', 0))
        for value in [0, 1, 25, 100]:
            t.bonus_percentage = value
            t.save()
        for value in [-1, 101, Decimal('25.5'), '12.5']:
            t.bonus_percentage = value
            with self.subTest(value=value), self.assertRaises(ValidationError):
                t.save()
        t.refresh_from_db()
        form = MonthThemeForm(instance=t)
        self.assertNotIn('bonus_pages', form.fields)
        self.assertEqual(form.fields['bonus_percentage'].label, 'Theme Bonus (%)')
        legacy = self.theme(bonus_rule='legacy_fixed', bonus_pages=50)
        self.assertIn('bonus_pages', MonthThemeForm(instance=legacy).fields)
        self.assertNotIn('bonus_percentage', MonthThemeForm(instance=legacy).fields)

    def test_rate_locked_by_any_claim_and_recovery_cannot_bypass(self):
        t = self.theme()
        t.bonus_percentage = 30
        t.save()
        ThemeClaim.objects.create(theme=t, submission=self.submission)
        t.bonus_percentage = 40
        with self.assertRaises(ValidationError): t.save()
        t.refresh_from_db()
        t.description = 'Other fields remain editable'
        t.save()
        with self.assertRaises(ValidationError):
            correct_unused_theme(theme=t, values={'bonus_percentage': 40},
                recovery_request=self.request(t, 'theme.correct_unused'))
        t.refresh_from_db()
        self.assertEqual(t.bonus_percentage, 30)
        t.bonus_rule = 'legacy_fixed'
        with self.assertRaises(ValidationError): t.save()

    def test_exact_rounding_examples_and_invalid_basis(self):
        for pages, rate, expected in [(400,25,100),(333,25,83),(334,25,84),
                                     (1,25,0),(400,0,0),(400,100,400)]:
            with self.subTest(pages=pages, rate=rate):
                self.assertEqual(percentage_bonus_pages(pages,rate),expected)
        for pages in [None,0,-1]:
            with self.assertRaises(ValidationError): percentage_bonus_pages(pages,25)

    def test_stacking_approved_basis_corrections_and_unrelated_modifiers(self):
        claims = [ThemeClaim.objects.create(theme=self.theme(rate), submission=self.submission) for rate in [25,25]]
        legacy = ThemeClaim.objects.create(theme=self.theme(bonus_rule='legacy_fixed', bonus_pages=50), submission=self.submission)
        self.review(334, decisions={c.pk:'approved' for c in claims+[legacy]})
        self.assertEqual((self.submission.bonus_pages,self.submission.final_scored_pages),(218,552))
        standalone = ModifierProvenance.objects.create(month=self.month, participant=self.member,
            source_type='game_reward',source_reference='game:test',source_label='Game',amount=20,
            effective_date=date(2026,11,10))
        checkpoint = ProgressCheckpoint.objects.create(month=self.month, scheduled_at=timezone.now(),
            target_basis='fixed', fixed_target_pages=600)
        result = ProgressCheckpointResult.objects.create(checkpoint=checkpoint, participant=self.member,
            evaluated_at=timezone.now(), threshold_percentage=50, progress_basis='total', target_basis='fixed',
            target_pages=600, required_pages=300, progress_pages=552, outcome='met')
        before = dict(ProgressCheckpointResult.objects.values().get(pk=result.pk))
        # No changed claim decision: a page-only correction must still update awards.
        self.review(500)
        for c in claims:
            c.refresh_from_db()
            self.assertEqual((c.award_basis_pages,c.award_percentage,c.award_bonus_pages),(500,25,125))
            self.assertEqual(ModifierProvenance.objects.get(source_reference=f'theme_claim:{c.pk}').amount,125)
        legacy.refresh_from_db()
        self.assertEqual(legacy.approved_bonus_pages,50)
        self.assertEqual((self.submission.bonus_pages,self.submission.final_scored_pages),(300,800))
        self.assertEqual(challenge_score_totals(month=self.month)[self.member.pk]['total_pages'],820)
        self.assertEqual(self.team.approved_pages,820)
        standalone.refresh_from_db()
        self.assertEqual(standalone.amount,20)
        self.assertEqual(dict(ProgressCheckpointResult.objects.values().get(pk=result.pk)),before)
        self.review(500)
        self.assertEqual(self.submission.final_scored_pages,800)
        self.assertEqual(ModifierProvenance.objects.filter(source_type='theme_bonus').count(),3)

    def test_finalized_zero_rejection_reapproval_and_positive_zero_corrections(self):
        c = ThemeClaim.objects.create(theme=self.theme(),submission=self.submission)
        self.review(1,decisions={c.pk:'approved'})
        c.refresh_from_db()
        self.assertTrue(c.award_finalized)
        self.assertEqual((c.award_basis_pages,c.award_bonus_pages),(1,0))
        self.assertFalse(ModifierProvenance.objects.filter(submission=self.submission).exists())
        self.review(1,status='rejected')
        self.review(1,decisions={c.pk:'approved'})
        c.refresh_from_db()
        self.assertEqual((c.award_basis_pages,c.award_bonus_pages,c.approved_bonus_pages),(1,0,0))
        self.review(400)
        c.refresh_from_db()
        self.assertEqual((c.award_basis_pages,c.award_bonus_pages),(400,100))
        self.review(1)
        c.refresh_from_db()
        self.assertEqual((c.award_bonus_pages,c.approved_bonus_pages),(0,0))
        self.assertFalse(ModifierProvenance.objects.get(submission=self.submission).is_active)
        refresh_submission_score(self.submission)
        self.assertEqual(c.award_bonus_pages,0)

    def test_recovery_restore_and_reconciliation_preserve_snapshots(self):
        c = ThemeClaim.objects.create(theme=self.theme(),submission=self.submission)
        self.review(400,decisions={c.pk:'approved'})
        for removed in [True,False]:
            set_submission_removed(submission=self.submission,removed=removed,
                recovery_request=self.request(self.submission,'submission.recover'))
            c.refresh_from_db()
            self.assertEqual((c.award_basis_pages,c.award_bonus_pages),(400,100))
        for state in ['rejected','approved','approved']:
            recover_theme_claim(claim=c,status=state,
                recovery_request=self.request(c,'theme_claim.reconcile'))
        c.refresh_from_db()
        self.assertEqual((c.award_basis_pages,c.award_bonus_pages,c.approved_bonus_pages),(400,100,100))
        self.assertEqual(ModifierProvenance.objects.filter(submission=self.submission).count(),1)

    def test_recovery_first_percentage_approval_and_zero(self):
        for rate in [25,0]:
            c = ThemeClaim.objects.create(theme=self.theme(rate), submission=self.submission)
            self.review(400)
            recover_theme_claim(claim=c,status='approved',recovery_request=self.request(c,'theme_claim.reconcile'))
            c.refresh_from_db()
            self.assertTrue(c.award_finalized)
            self.assertEqual((c.award_percentage,c.award_basis_pages,c.award_bonus_pages),(rate,400,rate*4))

    def test_review_correction_rollback_keeps_base_awards_and_provenance_consistent(self):
        c = ThemeClaim.objects.create(theme=self.theme(), submission=self.submission)
        self.review(400,decisions={c.pk:'approved'})
        with patch('core.scoring.refresh_submission_score', side_effect=RuntimeError('rollback')):
            with self.assertRaises(RuntimeError): self.review(500)
        c.refresh_from_db();self.submission.refresh_from_db()
        self.assertEqual((self.submission.approved_pages,c.award_basis_pages,c.award_bonus_pages),(400,400,100))
        self.assertEqual(ModifierProvenance.objects.get(submission=self.submission).amount,100)

    def test_permissions_lifecycle_and_ui(self):
        t=self.theme()
        url=reverse('theme-edit',kwargs={'group_slug':self.group.slug,'month_pk':self.month.pk,'pk':t.pk})
        self.client.force_login(self.user)
        self.assertContains(self.client.get(url),'Theme Bonus (%)')
        self.assertContains(self.client.get(reverse('theme-list',kwargs={'group_slug':self.group.slug,'month_pk':self.month.pk})),'25% of Approved Pages')
        c=ThemeClaim.objects.create(theme=t,submission=self.submission)
        self.review(400,decisions={c.pk:'approved'})
        self.assertContains(self.client.get(reverse('submission-review',kwargs={'group_slug':self.group.slug,'month_pk':self.month.pk,'pk':self.submission.pk})),'25% (+100 pages)')
        self.month.transition_to('finalizing');self.month.transition_to('completed')
        response=self.client.get(reverse('submission-review',kwargs={'group_slug':self.group.slug,'month_pk':self.month.pk,'pk':self.submission.pk}))
        self.assertEqual(response.status_code,302)
        ordinary=get_user_model().objects.create_user('percentage-member')
        Membership.objects.create(group=self.group,user=ordinary,display_name='Member',role='owner')
        self.client.force_login(ordinary)
        self.assertEqual(self.client.get(url).status_code,403)

    def test_reports_and_refresh_do_not_recalculate_snapshots(self):
        c = ThemeClaim.objects.create(theme=self.theme(), submission=self.submission)
        self.review(400, decisions={c.pk: 'approved'})
        # Simulate an external catalog/data change: only intentional review may recalculate.
        BookSubmission.objects.filter(pk=self.submission.pk).update(approved_pages=500)
        before = dict(ThemeClaim.objects.values().get(pk=c.pk))
        challenge_score_totals(month=self.month)
        refresh_submission_score(self.submission)
        self.assertEqual(dict(ThemeClaim.objects.values().get(pk=c.pk)), before)
        self.assertEqual(ModifierProvenance.objects.get(submission=self.submission).amount, 100)

    def test_zero_recovery_reapproval_and_removal_are_finalized(self):
        c = ThemeClaim.objects.create(theme=self.theme(), submission=self.submission)
        self.review(1, decisions={c.pk: 'approved'})
        for state in ['rejected', 'approved', 'approved']:
            recover_theme_claim(claim=c, status=state,
                recovery_request=self.request(c, 'theme_claim.reconcile'))
        for removed in [True, False]:
            set_submission_removed(submission=self.submission, removed=removed,
                recovery_request=self.request(self.submission, 'submission.recover'))
        c.refresh_from_db()
        self.assertEqual((c.award_finalized, c.award_basis_pages, c.award_bonus_pages), (True, 1, 0))
        self.assertFalse(ModifierProvenance.objects.filter(submission=self.submission).exists())

    def test_form_validation_tampering_and_rate_lock_with_rejected_claim(self):
        t = self.theme()
        data = {'name': t.name, 'description': '', 'starts_on': '2026-11-01',
                'ends_on': '2026-11-30', 'bonus_percentage': 25, 'allow_stacking': True,
                'is_active': True, 'is_visible': True}
        for value in [-1, 101, '25.5']:
            form = MonthThemeForm({**data, 'bonus_percentage': value}, instance=t)
            self.assertFalse(form.is_valid())
            self.assertIn('bonus_percentage', form.errors)
        t.refresh_from_db()
        ThemeClaim.objects.create(theme=t, submission=self.submission, status='rejected')
        form = MonthThemeForm({**data, 'bonus_percentage': 99, 'description': 'Updated'}, instance=t)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        t.refresh_from_db()
        self.assertEqual((t.bonus_percentage, t.description), (25, 'Updated'))
        self.assertEqual(MonthTheme().bonus_percentage, 0)

    def test_correction_400_to_500_gives_625_and_legacy_unchanged(self):
        c = ThemeClaim.objects.create(theme=self.theme(), submission=self.submission)
        self.review(400, decisions={c.pk: 'approved'})
        self.review(500)
        c.refresh_from_db()
        self.assertEqual((c.award_bonus_pages, self.submission.final_scored_pages), (125, 625))
        self.review(500, status='rejected')
        self.review(500, decisions={c.pk: 'approved'})
        c.refresh_from_db()
        self.assertEqual((c.award_basis_pages, c.award_bonus_pages), (500, 125))

    def test_rate_stays_locked_after_claim_purge(self):
        t = self.theme()
        c = ThemeClaim.objects.create(theme=t, submission=self.submission)
        c.delete()
        t.refresh_from_db()
        self.assertTrue(t.bonus_rate_locked)
        t.bonus_percentage = 80
        with self.assertRaises(ValidationError): t.save()
        with self.assertRaises(ValidationError):
            correct_unused_theme(theme=t, values={'bonus_percentage': 80},
                recovery_request=self.request(t, 'theme.correct_unused'))

    def test_authorized_page_only_review_post_recalculates_and_audits(self):
        c = ThemeClaim.objects.create(theme=self.theme(), submission=self.submission)
        self.review(400, decisions={c.pk: 'approved'})
        self.client.force_login(self.user)
        url = reverse('submission-review', kwargs={'group_slug': self.group.slug,
            'month_pk': self.month.pk, 'pk': self.submission.pk})
        response = self.client.post(url, {'approved_pages': 500, 'status': 'approved',
            'verification_url': '', 'review_notes': 'Correct edition page count',
            'claims-TOTAL_FORMS': 1, 'claims-INITIAL_FORMS': 1,
            'claims-MIN_NUM_FORMS': 0, 'claims-MAX_NUM_FORMS': 1000,
            'claims-0-id': c.pk, 'claims-0-submission': self.submission.pk,
            'claims-0-status': 'approved'})
        self.assertEqual(response.status_code, 302)
        c.refresh_from_db(); self.submission.refresh_from_db()
        self.assertEqual((c.award_basis_pages, c.award_bonus_pages,
                          self.submission.final_scored_pages), (500, 125, 625))
        from .models import AuditEvent
        event = AuditEvent.objects.get(action='submission.approved', object_id=str(self.submission.pk))
        self.assertIn('500', event.summary)

    def test_rejected_awards_correct_independently_without_activation(self):
        claims = [ThemeClaim.objects.create(theme=self.theme(rate), submission=self.submission)
                  for rate in [25, 50]]
        legacy = ThemeClaim.objects.create(theme=self.theme(bonus_rule='legacy_fixed', bonus_pages=50),
                                          submission=self.submission)
        all_claims = claims + [legacy]
        self.review(400, decisions={c.pk: 'approved' for c in all_claims})
        source_ids = {c.pk: ModifierProvenance.objects.get(source_reference=f'theme_claim:{c.pk}').pk
                      for c in all_claims}
        self.review(400, decisions={c.pk: 'rejected' for c in all_claims})
        reviews = {c.pk: ThemeClaim.objects.get(pk=c.pk).reviewed_at for c in all_claims}
        self.review(500)
        for c, expected in zip(claims, [125, 250]):
            c.refresh_from_db()
            self.assertEqual((c.status, c.approved_bonus_pages, c.award_basis_pages,
                              c.award_bonus_pages), ('rejected', 0, 500, expected))
            source = ModifierProvenance.objects.get(pk=source_ids[c.pk])
            self.assertEqual((source.amount, source.is_active), (expected, False))
            self.assertEqual(c.reviewed_at, reviews[c.pk])
        self.assertEqual((self.submission.bonus_pages, self.submission.final_scored_pages), (0, 500))
        self.review(500, decisions={c.pk: 'approved' for c in all_claims})
        self.assertEqual((self.submission.bonus_pages, self.submission.final_scored_pages), (425, 925))
        self.review(500)
        for c in all_claims:
            self.assertEqual(ModifierProvenance.objects.get(source_reference=f'theme_claim:{c.pk}').pk,
                             source_ids[c.pk])
        self.assertEqual(ModifierProvenance.objects.filter(submission=self.submission).count(), 3)

    def test_rejected_zero_award_correction_and_reapproval(self):
        c = ThemeClaim.objects.create(theme=self.theme(), submission=self.submission)
        self.review(1, decisions={c.pk: 'approved'})
        self.review(1, decisions={c.pk: 'rejected'})
        self.review(400)
        c.refresh_from_db()
        self.assertEqual((c.status, c.award_finalized, c.award_bonus_pages), ('rejected', True, 100))
        self.assertFalse(ModifierProvenance.objects.filter(submission=self.submission).exists())
        self.review(400, decisions={c.pk: 'approved'})
        self.assertEqual(self.submission.final_scored_pages, 500)
        self.review(400, decisions={c.pk: 'rejected'})
        source = ModifierProvenance.objects.get(submission=self.submission)
        self.review(1)
        c.refresh_from_db(); source.refresh_from_db()
        self.assertEqual((c.award_basis_pages, c.award_bonus_pages, c.status), (1, 0, 'rejected'))
        self.assertFalse(source.is_active)
        self.review(1, decisions={c.pk: 'approved'})
        c.refresh_from_db()
        self.assertEqual((c.award_finalized, c.approved_bonus_pages, self.submission.final_scored_pages),
                         (True, 0, 1))
        self.assertEqual(ModifierProvenance.objects.get(submission=self.submission).pk, source.pk)

    def test_restore_establishes_first_awards_for_percentage_zero_and_legacy(self):
        claims = [ThemeClaim.objects.create(theme=self.theme(rate), submission=self.submission)
                  for rate in [25, 0]]
        legacy = ThemeClaim.objects.create(theme=self.theme(bonus_rule='legacy_fixed', bonus_pages=50),
                                          submission=self.submission)
        self.review(400)
        set_submission_removed(submission=self.submission, removed=True,
            recovery_request=self.request(self.submission, 'submission.recover'))
        for c in claims + [legacy]:
            recover_theme_claim(claim=c, status='approved',
                recovery_request=self.request(c, 'theme_claim.reconcile'))
        for c in claims:
            c.refresh_from_db()
            self.assertEqual((c.status, c.award_finalized, c.approved_bonus_pages), ('approved', False, 0))
        self.assertEqual(challenge_score_totals(month=self.month, participant_ids=[self.member.pk])[self.member.pk]['total_pages'], 0)
        set_submission_removed(submission=self.submission, removed=False,
            recovery_request=self.request(self.submission, 'submission.recover'))
        self.submission.refresh_from_db()
        self.assertEqual((self.submission.bonus_pages, self.submission.final_scored_pages), (150, 550))
        for c, expected in zip(claims, [100, 0]):
            c.refresh_from_db()
            self.assertEqual((c.status, c.award_finalized, c.award_basis_pages, c.award_bonus_pages),
                             ('approved', True, 400, expected))
        sources = list(ModifierProvenance.objects.filter(submission=self.submission).values_list('pk', flat=True))
        for _ in range(2):
            set_submission_removed(submission=self.submission, removed=True,
                recovery_request=self.request(self.submission, 'submission.recover'))
            set_submission_removed(submission=self.submission, removed=False,
                recovery_request=self.request(self.submission, 'submission.recover'))
            for c in claims + [legacy]:
                recover_theme_claim(claim=c, status='approved',
                    recovery_request=self.request(c, 'theme_claim.reconcile'))
        self.assertEqual(list(ModifierProvenance.objects.filter(submission=self.submission)
                              .values_list('pk', flat=True)), sources)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.final_scored_pages, 550)

    def test_reapproval_initializes_a_claim_approved_while_book_rejected(self):
        c = ThemeClaim.objects.create(theme=self.theme(), submission=self.submission)
        self.review(400, status='rejected')
        recover_theme_claim(claim=c, status='approved',
            recovery_request=self.request(c, 'theme_claim.reconcile'))
        c.refresh_from_db()
        self.assertFalse(c.award_finalized)
        self.review(400)
        c.refresh_from_db()
        self.assertEqual((c.award_finalized, c.award_bonus_pages, self.submission.final_scored_pages),
                         (True, 100, 500))

    def test_restore_initialization_rolls_back_with_recovery_failure(self):
        c = ThemeClaim.objects.create(theme=self.theme(), submission=self.submission)
        self.review(400)
        set_submission_removed(submission=self.submission, removed=True,
            recovery_request=self.request(self.submission, 'submission.recover'))
        recover_theme_claim(claim=c, status='approved',
            recovery_request=self.request(c, 'theme_claim.reconcile'))
        with patch('core.recovery_scoring._synchronize_reader_sources', side_effect=RuntimeError('rollback')):
            with self.assertRaises(RuntimeError):
                set_submission_removed(submission=self.submission, removed=False,
                    recovery_request=self.request(self.submission, 'submission.recover'))
        c.refresh_from_db(); self.submission.refresh_from_db()
        self.assertTrue(self.submission.is_removed)
        self.assertFalse(c.award_finalized)
        self.assertFalse(ModifierProvenance.objects.filter(submission=self.submission).exists())


class PercentageMigrationTests(TransactionTestCase):
    def test_additive_migration_preserves_legacy_awards_caches_and_checkpoints(self):
        executor=MigrationExecutor(connection)
        before=[('core','0059_hardcover_oauth_state_use')]
        executor.migrate(before)
        apps=executor.loader.project_state(before).apps
        def model(n):return apps.get_model('core',n)
        try:
            user=apps.get_model('auth','User').objects.create(username='migration-reader')
            group=model('ReadingGroup').objects.create(name='Migration',slug='migration')
            member=model('Membership').objects.create(group=group,user=user,display_name='Reader',role='member')
            month=model('ChallengeMonth').objects.create(group=group,name='Historical',status='completed',starts_on=date(2026,11,1),ends_on=date(2026,11,30))
            book=model('BookSubmission').objects.create(month=month,participant=member,title='Book',author='Writer',book_format='ebook',completed_on=date(2026,11,10),submitted_pages=400,approved_pages=400,status='approved',bonus_pages=50,final_scored_pages=450)
            theme=model('MonthTheme').objects.create(month=month,name='Legacy',starts_on=month.starts_on,ends_on=month.ends_on,bonus_pages=50)
            claim=model('ThemeClaim').objects.create(submission=book,theme=theme,status='approved',approved_bonus_pages=50)
            zero_theme=model('MonthTheme').objects.create(month=month,name='Zero',starts_on=month.starts_on,ends_on=month.ends_on,bonus_pages=0)
            zero=model('ThemeClaim').objects.create(submission=book,theme=zero_theme,status='approved',approved_bonus_pages=0)
            pending=model('ThemeClaim').objects.create(submission=book,theme=model('MonthTheme').objects.create(month=month,name='Pending',starts_on=month.starts_on,ends_on=month.ends_on,bonus_pages=75))
            model('ModifierProvenance').objects.create(month=month,participant=member,submission=book,source_type='theme_bonus',source_reference=f'theme_claim:{claim.pk}',source_label='Legacy',amount=50,effective_date=book.completed_on)
            checkpoint=model('ProgressCheckpoint').objects.create(month=month,scheduled_at=timezone.now(),evaluation_state='evaluated',threshold_percentage=50,progress_basis='total',target_basis='fixed',fixed_target_pages=600)
            model('ProgressCheckpointResult').objects.create(checkpoint=checkpoint,participant=member,evaluated_at=timezone.now(),threshold_percentage=50,progress_basis='total',target_basis='fixed',target_pages=600,required_pages=300,progress_pages=450,outcome='met')
            preserved={n:list(model(n).objects.values()) for n in ['BookSubmission','ModifierProvenance','ProgressCheckpointResult']}
            executor=MigrationExecutor(connection);executor.migrate([('core','0060_theme_percentage_awards')])
            for name,rows in preserved.items():
                self.assertEqual(list(globals()[name].objects.values()),rows)
            for pk,amount in [(theme.pk,50),(zero_theme.pk,0)]:
                t=MonthTheme.objects.get(pk=pk)
                self.assertEqual((t.bonus_rule,t.bonus_pages),('legacy_fixed',amount))
            for pk,amount in [(claim.pk,50),(zero.pk,0),(pending.pk,0)]:
                c=ThemeClaim.objects.get(pk=pk)
                self.assertEqual(c.approved_bonus_pages,amount)
                self.assertFalse(c.award_finalized)
            self.assertEqual(challenge_score_totals(month=ChallengeMonth.objects.get(pk=month.pk))[member.pk]['total_pages'],450)
        finally:
            executor=MigrationExecutor(connection);executor.migrate(executor.loader.graph.leaf_nodes())
