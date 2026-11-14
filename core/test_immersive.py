from datetime import date
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from . import test_submission_editing as fixture
from .immersive import (bonus_pages, configure_immersive, create_with_immersive, request_immersive,
    review_immersive, review_queue_requests, withdraw_immersive)
from .models import (AuditEvent, BookSubmission, BotmBook, ChallengeMonth, ChallengeStaffAssignment,
    ImmersiveRequest, ModifierProvenance, MonthEnrollment, MonthTheme, ThemeClaim)
from .scoring import apply_submission_review, refresh_submission_score
from .score_aggregation import challenge_score_totals
from .submission_corrections import correct_submission, correction_version


class ImmersiveTests(TestCase):
    actor = fixture.SubmissionEditingTests.actor
    submission = fixture.SubmissionEditingTests.submission

    def setUp(self):
        fixture.SubmissionEditingTests.setUp(self)
        self.host = self.actor()

    def enable(self, percent=100, enabled=True):
        self.month = configure_immersive(month=self.month, actor=self.host, enabled=enabled, percent=percent)

    def request(self, item=None):
        return request_immersive(submission=item or self.submission(), actor=self.reader.user)

    def approve(self, request):
        return review_immersive(request=request, actor=self.host, decision='approved', reason='Read and listened')

    def source(self, request):
        return ModifierProvenance.objects.get(source_type='immersive', source_reference=f'immersive_request:{request.pk}')

    def test_defaults_configuration_bounds_permissions_and_audit(self):
        self.assertFalse(self.month.immersive_enabled)
        self.assertEqual(self.month.immersive_bonus_percent,100)
        with self.assertRaises(PermissionDenied):
            configure_immersive(month=self.month,actor=self.reader.user,enabled=True,percent=50)
        for percent in (-1,201,50.5,True):
            with self.assertRaises(ValidationError): self.enable(percent)
        self.enable(200)
        self.assertTrue(AuditEvent.objects.filter(action='immersive.configured').exists())
        with transaction.atomic(), self.assertRaises(IntegrityError):
            ChallengeMonth.objects.filter(pk=self.month.pk).update(immersive_bonus_percent=201)

    def test_percentage_snapshots_and_chaos_card_changes(self):
        self.enable(50); first=self.request()
        self.enable(200); second=self.request()
        self.approve(first);self.approve(second)
        self.assertEqual((self.source(first).amount,self.source(second).amount),(175,700))
        self.enable(0)
        refresh_submission_score(first.submission)
        first.refresh_from_db()
        self.assertEqual((first.captured_percent,first.awarded_pages),(50,175))
        first.captured_percent=100
        with self.assertRaises(ValidationError): first.save()

    def test_round_half_up_zero_and_maximum_provenance(self):
        for base,percent,expected in [(400,50,200),(400,100,400),(400,200,800),(350,50,175),(1,50,1),(351,50,176),(350,0,0)]:
            self.assertEqual(bonus_pages(base,percent),expected)
        for percent in (0,200):
            self.enable(percent); request=self.approve(self.request())
            source=self.source(request)
            self.assertEqual(source.amount,350*percent//100)
            self.assertTrue(source.is_active)
            self.assertEqual(request.awarded_pages,source.amount)
        source.source_type='game_reward';source.source_reference='zero-other';source.amount=0
        with transaction.atomic(), self.assertRaises(IntegrityError):
            source.pk=None;source.save(force_insert=True)

    def test_manual_hardcover_independent_approval_and_initial_creation(self):
        self.enable()
        for catalog in (False,True):
            item,request=create_with_immersive(actor=self.reader.user,
                create_submission=lambda:self.submission(catalog=catalog))
            self.assertEqual(request.status,'pending')
            self.assertFalse(ModifierProvenance.objects.filter(source_type='immersive',submission=item).exists())
            self.assertEqual(self.approve(request).awarded_pages,350)
        with self.assertRaises(ValidationError):
            request_immersive(submission=item,actor=self.reader.user)

    def test_pending_and_rejected_books_wait_until_approved(self):
        self.enable()
        for status in ('pending','rejected'):
            item=self.submission(status=status);request=self.request(item)
            self.assertFalse(review_queue_requests(actor=self.host,month=self.month).filter(pk=request.pk).exists())
            with self.assertRaises(ValidationError): self.approve(request)
            request.refresh_from_db();self.assertEqual(request.status,'pending')
            item.status='approved';item.approved_pages=350
            apply_submission_review(item,[],self.host,timezone.now())
            self.assertTrue(review_queue_requests(actor=self.host,month=self.month).filter(pk=request.pk).exists())
            self.approve(request)

    def test_duplicates_protected_in_service_and_database(self):
        self.enable();request=self.request()
        with self.assertRaises(ValidationError): self.request(request.submission)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            ImmersiveRequest.objects.create(submission=request.submission,requester=self.reader.user,captured_percent=100)
        self.approve(request)
        with self.assertRaises(ValidationError): self.request(request.submission)
        self.assertEqual(ImmersiveRequest.objects.count(),1)

    def test_rejection_withdrawal_resubmission_and_current_rates(self):
        self.enable(50);request=self.request()
        review_immersive(request=request,actor=self.host,decision='rejected',reason='Not both formats')
        self.enable(100);new=self.request(request.submission)
        self.assertEqual(new.captured_percent,100)
        withdraw_immersive(request=new,actor=self.reader.user,reason='Mistake')
        withdraw_immersive(request=new,actor=self.reader.user)
        self.enable(200);last=self.request(request.submission)
        self.assertEqual(last.captured_percent,200)
        self.assertFalse(ModifierProvenance.objects.filter(source_type='immersive').exists())
        self.assertTrue(AuditEvent.objects.filter(action='immersive.withdrawn').exists())

    def test_revocation_preserves_approval_and_never_restores(self):
        self.enable();request=self.approve(self.request());reviewed_at=request.reviewed_at
        with self.assertRaises(PermissionDenied):
            review_immersive(request=request,actor=self.reader.user,decision='revoked',reason='No')
        request=review_immersive(request=request,actor=self.host,decision='revoked',reason='Incorrect attestation')
        refresh_submission_score(request.submission)
        self.assertFalse(self.source(request).is_active)
        self.assertEqual(request.reviewed_at,reviewed_at)
        self.assertEqual(request.status,'revoked')
        with self.assertRaises(ValidationError):self.approve(request)
        with self.assertRaises(ValidationError):withdraw_immersive(request=request,actor=self.reader.user)

    def test_book_rejection_removal_reinstatement_and_audit(self):
        self.enable(50);request=self.approve(self.request());item=request.submission
        for removed,status in [(False,'rejected'),(False,'approved'),(True,'approved'),(False,'approved')]:
            item.is_removed=removed;item.status=status;item.save()
            refresh_submission_score(item)
            source=self.source(request)
            self.assertEqual(source.is_active,not removed and status=='approved')
        for action in ('suspended','restored'):
            self.assertTrue(AuditEvent.objects.filter(action=f'immersive.{action}').exists())
        self.assertEqual(ModifierProvenance.objects.filter(source_type='immersive').count(),1)

    def test_disabled_feature_preserves_pending_and_approved_awards(self):
        self.enable(50);request=self.request();self.enable(200,enabled=False)
        self.approve(request)
        with self.assertRaises(ValidationError):self.request()
        item=request.submission;item.approved_pages=400;item.save();refresh_submission_score(item)
        self.assertEqual(self.source(request).amount,200)

    def test_completed_finalizing_review_and_archived_readonly(self):
        self.enable()
        for status in ('finalizing','completed','archived'):
            ChallengeMonth.objects.filter(pk=self.month.pk).update(status='active')
            self.month.refresh_from_db()
            request=self.request()
            ChallengeMonth.objects.filter(pk=self.month.pk).update(status=status)
            self.month.refresh_from_db()
            with self.assertRaises(ValidationError):self.request()
            if status=='archived':
                with self.assertRaises(PermissionDenied):self.approve(request)
                with self.assertRaises(PermissionDenied):withdraw_immersive(request=request,actor=self.reader.user)
                self.assertFalse(review_queue_requests(actor=self.host,month=self.month).exists())
            else:
                self.assertTrue(review_queue_requests(actor=self.host,month=self.month).filter(pk=request.pk).exists())
                self.approve(request)

    def test_staff_roles_scopes_and_revoked_authority(self):
        self.enable();request=self.request()
        for role in ('owner','platform','floater','team_leader','moderator'):
            actor=self.actor(role)
            if role=='moderator':
                with self.assertRaises(PermissionDenied):review_immersive(request=request,actor=actor,decision='approved')
                continue
            self.assertTrue(review_queue_requests(actor=actor,month=self.month).filter(pk=request.pk).exists())
            if role in ('owner','platform','floater'):
                extra=self.request()
                self.assertEqual(review_immersive(request=extra,actor=actor,decision='approved').status,'approved')
            if role=='team_leader':
                self.assertEqual(review_immersive(request=self.request(),actor=actor,decision='approved').status,'approved')
                from .models import TeamAssignment
                TeamAssignment.objects.filter(participant=self.reader).update(ended_at=timezone.now())
                self.assertFalse(review_queue_requests(actor=actor,month=self.month).filter(pk=request.pk).exists())
                with self.assertRaises(PermissionDenied):review_immersive(request=request,actor=actor,decision='approved')
        ChallengeStaffAssignment.objects.filter(membership__user=self.host).update(ended_at=timezone.now())
        with self.assertRaises(PermissionDenied):self.approve(request)

    def test_reader_ownership_deadline_and_staff_self_approval_denied(self):
        self.enable();item=self.submission()
        with self.assertRaises(PermissionDenied):request_immersive(submission=item,actor=self.host)
        request=self.request(item)
        ChallengeStaffAssignment.objects.create(month=self.month,membership=self.reader,role='host')
        with self.assertRaises(PermissionDenied):review_immersive(request=request,actor=self.reader.user,decision='approved')
        MonthEnrollment.objects.filter(month=self.month,participant=self.reader).update(is_active=False)
        with self.assertRaises(ValidationError):self.request()

    def test_idempotent_additive_scoring_and_queue_metadata(self):
        self.enable(50);request=self.request();item=request.submission
        self.month.botm_enabled=True
        self.month.save(update_fields=['botm_enabled'])
        BotmBook.objects.create(month=self.month,position=1,title_snapshot=item.title,author_snapshot=item.author,page_count_snapshot=350,bonus_pages=30)
        from .botm_matching import synchronize_submission
        synchronize_submission(item)
        theme=MonthTheme.objects.create(month=self.month,name='Fixed',starts_on=self.month.starts_on,ends_on=self.month.ends_on,bonus_pages=25)
        ThemeClaim.objects.create(submission=item,theme=theme,status='approved',approved_bonus_pages=25)
        queued=review_queue_requests(actor=self.host,month=self.month).get(pk=request.pk)
        self.assertEqual((queued.entry_type,queued.calculated_bonus),('immersive',175))
        self.approve(request)
        count=AuditEvent.objects.filter(action__startswith='immersive.').count()
        self.approve(request);refresh_submission_score(item)
        self.assertEqual(AuditEvent.objects.filter(action__startswith='immersive.').count(),count)
        item.refresh_from_db();self.assertEqual(item.bonus_pages,20+30+25+175)
        score=challenge_score_totals(month=self.month,participant_ids=[self.reader.pk])[self.reader.pk]
        self.assertEqual(score['total_pages'],350+20+30+25+175)

    def test_participation_withdrawal_and_reactivation_preserve_award(self):
        from .participation import deactivate_participation, activate_participation
        self.enable();request=self.approve(self.request())
        enrollment=MonthEnrollment.objects.get(month=self.month,participant=self.reader)
        deactivate_participation(enrollment=enrollment,actor=self.host,reason='withdrawn')
        self.assertTrue(self.source(request).is_active)
        activate_participation(month=self.month,participant=self.reader,actor=self.host,origin='staff')
        self.assertTrue(self.source(request).is_active)

    def test_normal_book_removal_suspends_award_atomically(self):
        from django.urls import reverse
        self.enable();request=self.approve(self.request());item=request.submission
        self.client.force_login(self.host)
        url=reverse('submission-remove',args=[self.month.group.slug,self.month.pk,item.pk])
        with patch('core.scoring.refresh_submission_score',side_effect=ValidationError('Synthetic lock failure')):
            with self.assertRaises(ValidationError):self.client.post(url,{'reason':'Remove'})
        item.refresh_from_db();self.assertFalse(item.is_removed)
        self.assertTrue(self.source(request).is_active)
        self.assertEqual(self.client.post(url,{'reason':'Remove'}).status_code,302)
        request.refresh_from_db();self.assertEqual((request.status,request.is_active),('approved',False))
        self.assertFalse(self.source(request).is_active)

    def test_invalid_approved_base_is_not_reviewable(self):
        self.enable();item=self.submission(status='pending');request=self.request(item)
        BookSubmission.objects.filter(pk=item.pk).update(status='approved',approved_pages=None)
        self.assertFalse(review_queue_requests(actor=self.host,month=self.month).filter(pk=request.pk).exists())
        with self.assertRaises(ValidationError):self.approve(request)

    def test_failure_rolls_back_request_decision_and_award(self):
        self.enable();request=self.request()
        with patch('core.scoring.refresh_submission_score',side_effect=ValidationError('Synthetic failure')):
            with self.assertRaises(ValidationError):self.approve(request)
        request.refresh_from_db();self.assertEqual(request.status,'pending')
        self.assertFalse(ModifierProvenance.objects.filter(source_type='immersive').exists())


class ImmersiveCorrectionTests(TransactionTestCase):
    setUp = ImmersiveTests.setUp
    actor = ImmersiveTests.actor
    submission = ImmersiveTests.submission
    enable = ImmersiveTests.enable
    request = ImmersiveTests.request
    approve = ImmersiveTests.approve
    source = ImmersiveTests.source

    def test_phase4a_page_correction_retains_approval_rate_and_theme_history(self):
        self.enable(50)
        item=self.submission()
        theme=MonthTheme.objects.create(month=self.month,name='Percent',starts_on=self.month.starts_on,
            ends_on=self.month.ends_on,bonus_rule='percentage',bonus_percentage=25)
        claim=ThemeClaim.objects.create(submission=item,theme=theme,status='approved')
        apply_submission_review(item,[claim],self.host,timezone.now())
        request=self.approve(self.request(item));approved_at=request.reviewed_at
        self.enable(200,enabled=False)
        item=correct_submission(submission=item,actor=self.host,version=correction_version(item,self.host),changes={'page_count':401})
        request.refresh_from_db()
        self.assertEqual((request.captured_percent,request.awarded_pages,request.status,request.reviewed_at),(50,201,'approved',approved_at))
        self.assertEqual(self.source(request).amount,201)
        self.assertTrue(AuditEvent.objects.filter(action='immersive.recalculated').exists())
        self.assertEqual(ImmersiveRequest.objects.count(),1)
        self.assertEqual(ModifierProvenance.objects.filter(source_type='immersive').count(),1)
        self.assertTrue(ThemeClaim.objects.filter(pk=claim.pk,award_percentage=25).exists())

    def test_atomic_initial_creation_failure_leaves_no_submission(self):
        self.enable(enabled=False)
        count=BookSubmission.objects.count()
        with self.assertRaises(ValidationError):
            create_with_immersive(actor=self.reader.user,create_submission=lambda:self.submission())
        self.assertEqual(BookSubmission.objects.count(),count)


class ImmersiveMigrationTests(TransactionTestCase):
    def test_sqlite_upgrade_preserves_existing_submission_and_provenance(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        executor=MigrationExecutor(connection)
        try:
            executor.migrate([('core','0062_backfill_selection_display_metadata')])
            apps=executor.loader.project_state([('core','0062_backfill_selection_display_metadata')]).apps
            User=apps.get_model('auth','User')
            user=User.objects.create(username='migration-reader')
            group=apps.get_model('core','ReadingGroup').objects.create(name='Existing',slug='existing')
            member=apps.get_model('core','Membership').objects.create(group=group,user=user,display_name='Reader')
            month=apps.get_model('core','ChallengeMonth').objects.create(group=group,name='Existing',status='active')
            submission=apps.get_model('core','BookSubmission').objects.create(month=month,participant=member,
                title='Historical',author='Author',book_format='ebook',completed_on=date(2026,11,1),
                submitted_pages=400,approved_pages=400,status='approved',bonus_pages=50,final_scored_pages=450)
            source=apps.get_model('core','ModifierProvenance').objects.create(month=month,participant=member,submission=submission,
                source_type='legacy_modifier',source_reference='existing:1',source_label='Existing reward',amount=50,effective_date=date(2026,11,1))
            before=apps.get_model('core','ModifierProvenance').objects.values().get(pk=source.pk)
            executor=MigrationExecutor(connection)
            executor.migrate([('core','0063_immersive_reading')])
            migrated = executor.loader.project_state([('core','0063_immersive_reading')]).apps
            self.assertEqual(migrated.get_model('core','ModifierProvenance').objects.values().get(pk=source.pk),before)
            upgraded=migrated.get_model('core','BookSubmission').objects.get(pk=submission.pk)
            self.assertEqual((upgraded.approved_pages,upgraded.bonus_pages,upgraded.final_scored_pages),(400,50,450))
            upgraded_month=migrated.get_model('core','ChallengeMonth').objects.get(pk=month.pk)
            self.assertEqual((upgraded_month.immersive_enabled,upgraded_month.immersive_bonus_percent),(False,100))
            self.assertFalse(migrated.get_model('core','ImmersiveRequest').objects.exists())
            with transaction.atomic(), self.assertRaises(IntegrityError):
                migrated.get_model('core','ModifierProvenance').objects.filter(pk=source.pk).update(amount=0)
        finally:
            restore = MigrationExecutor(connection)
            restore.migrate(restore.loader.graph.leaf_nodes())
