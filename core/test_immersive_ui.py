from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from . import test_immersive as fixture
from .models import BookSubmission, ImmersiveRequest, ModifierProvenance, ChallengeMonth, TeamAssignment, AuditEvent


class ImmersiveInterfaceTests(TestCase):
    setUp=fixture.ImmersiveTests.setUp
    actor=fixture.ImmersiveTests.actor
    submission=fixture.ImmersiveTests.submission
    enable=fixture.ImmersiveTests.enable
    request=fixture.ImmersiveTests.request
    approve=fixture.ImmersiveTests.approve
    source=fixture.ImmersiveTests.source

    def route(self,name,pk=None,operation=None):
        args=[self.month.group.slug,self.month.pk]
        if pk is not None:args.append(pk)
        if operation:args.append(operation)
        return reverse(name,args=args)

    def test_settings_permissions_rate_and_guidance(self):
        url=self.route('immersive-settings')
        self.assertEqual(self.client.get(url).status_code,403)
        self.client.force_login(self.host)
        self.assertContains(self.client.get(self.route('challenge-settings')),'Immersive Reading')
        self.assertContains(self.client.get(url),'new requests only')
        self.assertEqual(self.client.post(url,{'immersive_enabled':'on','immersive_bonus_percent':50}).status_code,302)
        self.month.refresh_from_db();self.assertEqual(self.month.immersive_bonus_percent,50)
        response=self.client.post(url,{'immersive_bonus_percent':201})
        self.assertEqual(response.status_code,200)
        self.assertIn('immersive_bonus_percent',response.context['form'].errors)
        self.assertTrue(AuditEvent.objects.filter(action='immersive.configured').exists())

    def payload(self,**changes):
        values={'title':self.book.title,'author':self.book.author,'book_format':'ebook',
            'completed_on':'2026-11-03','submitted_pages':350,'immersive_request':'on'}
        values.update(changes);return values

    def test_initial_checkbox_manual_atomic_failure_and_disabled_forgery(self):
        self.enable(50);url=self.route('submission-create')
        self.assertContains(self.client.get(url),'I read and listened to this book')
        self.assertContains(self.client.get(url),'additional 50%')
        with patch('core.immersive.request_immersive',side_effect=ValidationError('Synthetic failure')):
            response=self.client.post(url,self.payload())
        self.assertEqual(response.status_code,200)
        self.assertFalse(BookSubmission.objects.exists());self.assertFalse(ImmersiveRequest.objects.exists())
        self.assertEqual(self.client.post(url,self.payload()).status_code,302)
        item=BookSubmission.objects.get();claim=ImmersiveRequest.objects.get()
        self.assertEqual((item.status,claim.status,claim.captured_percent),('pending','pending',50))
        self.enable(enabled=False)
        self.assertNotContains(self.client.get(url),'I read and listened to this book')
        self.client.post(url,self.payload(title='Another'))
        self.assertEqual(BookSubmission.objects.count(),1)

    def test_initial_hardcover_autoapproval_remains_independent(self):
        from django.core import signing
        self.enable()
        selection=signing.dumps({'selected':self.edition.pk,'scoring':self.edition.pk,'method':'hardcover'},salt='northbound.catalog-selection')
        response=self.client.post(self.route('submission-create'),self.payload(catalog_selection=selection))
        self.assertEqual(response.status_code,302)
        self.assertEqual(BookSubmission.objects.get().status,'approved')
        self.assertEqual(ImmersiveRequest.objects.get().status,'pending')
        self.assertFalse(ModifierProvenance.objects.filter(source_type='immersive').exists())

    def test_existing_request_status_duplicates_and_permissions(self):
        self.enable();item=self.submission();url=self.route('submission-immersive',item.pk)
        self.assertContains(self.client.get(url),'No request')
        self.assertEqual(self.client.post(url).status_code,302)
        self.assertContains(self.client.get(url),'Pending')
        self.assertEqual(self.client.post(url).status_code,200)
        self.assertEqual(ImmersiveRequest.objects.count(),1)
        other=self.actor('moderator');self.client.force_login(other)
        self.assertEqual(self.client.get(url).status_code,403)
        self.assertEqual(self.client.post(url).status_code,403)

    def test_withdraw_confirmation_post_csrf_and_resubmission(self):
        self.enable();claim=self.request();url=self.route('immersive-action',claim.pk,'withdraw')
        self.assertContains(self.client.get(url),'Confirm Withdraw')
        claim.refresh_from_db();self.assertEqual(claim.status,'pending')
        self.client.post(url,{})
        claim.refresh_from_db();self.assertEqual(claim.status,'pending')
        csrf=Client(enforce_csrf_checks=True);csrf.force_login(self.reader.user)
        self.assertEqual(csrf.post(url,{'confirm':'on'}).status_code,403)
        self.assertEqual(self.client.post(url,{'confirm':'on','reason':'Mistake'}).status_code,302)
        self.assertContains(self.client.get(self.route('submission-immersive',claim.submission_id)),'Withdrawn')
        self.assertEqual(self.client.post(self.route('submission-immersive',claim.submission_id)).status_code,302)
        self.assertEqual(ImmersiveRequest.objects.count(),2)

    def test_shared_queue_independent_review_and_idempotency(self):
        self.enable(50);claim=self.request();pending=self.request(self.submission(status='pending'))
        self.client.force_login(self.host)
        queue=self.client.get(self.route('review-queue'))
        self.assertEqual(list(queue.context['immersive_requests'].values_list('pk',flat=True)),[claim.pk])
        self.assertContains(queue,'175 bonus pages')
        url=self.route('immersive-action',claim.pk,'approve')
        self.assertContains(self.client.get(url),'Confirm Approve')
        for _ in range(2):self.assertEqual(self.client.post(url,{'confirm':'on'}).status_code,302)
        self.assertEqual(ModifierProvenance.objects.filter(source_type='immersive').count(),1)
        self.assertEqual(self.source(claim).amount,175)
        pending.refresh_from_db();self.assertEqual(pending.status,'pending')

    def test_reject_reason_and_new_request(self):
        self.enable();claim=self.request();self.client.force_login(self.host)
        url=self.route('immersive-action',claim.pk,'reject')
        self.client.post(url,{'confirm':'on'})
        claim.refresh_from_db();self.assertEqual(claim.status,'pending')
        self.assertEqual(self.client.post(url,{'confirm':'on','reason':'Not eligible'}).status_code,302)
        claim.refresh_from_db();self.assertEqual(claim.review_reason,'Not eligible')
        self.client.force_login(self.reader.user)
        self.assertContains(self.client.get(self.route('submission-immersive',claim.submission_id)),'Rejected')
        self.assertEqual(self.client.post(self.route('submission-immersive',claim.submission_id)).status_code,302)

    def test_team_scope_completed_review_and_archived_restrictions(self):
        self.enable();claim=self.request();leader=self.actor('team_leader');self.client.force_login(leader)
        url=self.route('immersive-action',claim.pk,'approve')
        self.assertEqual(self.client.get(url).status_code,200)
        TeamAssignment.objects.filter(participant=self.reader).update(ended_at=timezone.now())
        self.assertEqual(self.client.post(url,{'confirm':'on'}).status_code,403)
        self.client.force_login(self.host)
        ChallengeMonth.objects.filter(pk=self.month.pk).update(status='completed')
        response=self.client.get(self.route('review-queue'))
        self.assertEqual(response.status_code,200);self.assertFalse(response.context['submissions'].exists())
        self.assertContains(response,'Immersive Reading')
        self.assertContains(self.client.get(self.month.get_absolute_url()),'Review Queue')
        self.assertEqual(self.client.post(url,{'confirm':'on'}).status_code,302)
        ChallengeMonth.objects.filter(pk=self.month.pk).update(status='archived')
        self.assertEqual(self.client.get(self.route('immersive-action',claim.pk,'revoke')).status_code,403)
        self.assertEqual(self.client.get(self.route('review-queue')).status_code,302)

    def test_revocation_confirmation_reader_denial_and_history(self):
        self.enable();claim=self.approve(self.request());url=self.route('immersive-action',claim.pk,'revoke')
        self.assertEqual(self.client.post(url,{'confirm':'on','reason':'Incorrect'}).status_code,403)
        self.client.force_login(self.host)
        self.assertContains(self.client.get(url),'preserves its approval history')
        self.client.post(url,{'reason':'Incorrect'})
        self.assertTrue(self.source(claim).is_active)
        self.assertEqual(self.client.post(url,{'confirm':'on','reason':'Incorrect'}).status_code,302)
        self.assertFalse(self.source(claim).is_active)
        self.assertContains(self.client.get(self.route('submission-immersive',claim.submission_id)),'Revoked')

    def test_authentication_and_configuration_scope(self):
        self.enable();claim=self.request()
        self.client.logout()
        for url in (self.route('immersive-settings'),self.route('submission-immersive',claim.submission_id),self.route('immersive-action',claim.pk,'withdraw')):
            self.assertEqual(self.client.get(url).status_code,302)
            self.assertEqual(self.client.post(url,{'confirm':'on'}).status_code,302)
        floater=self.actor('floater');self.client.force_login(floater)
        self.assertEqual(self.client.get(self.route('immersive-settings')).status_code,403)
        self.assertEqual(self.client.post(self.route('immersive-action',claim.pk,'approve'),{'confirm':'on'}).status_code,302)
        self.client.force_login(self.host)
        ChallengeMonth.objects.filter(pk=self.month.pk).update(status='completed')
        self.assertEqual(self.client.post(self.route('immersive-settings'),{'immersive_bonus_percent':100}).status_code,403)

    def test_other_methods_cross_group_and_owner_queue(self):
        self.enable();claim=self.request()
        self.assertEqual(self.client.get(self.route('submission-immersive',claim.submission_id).replace('/hotfix/','/other/')).status_code,404)
        self.assertEqual(self.client.put(self.route('submission-immersive',claim.submission_id)).status_code,405)
        owner=self.actor('owner');self.client.force_login(owner)
        self.assertContains(self.client.get(self.route('review-queue')),'Immersive Reading')
        self.assertEqual(self.client.post(self.route('immersive-action',claim.pk,'approve'),{'confirm':'on'}).status_code,302)


class ImmersiveEditingCompatibilityTests(TransactionTestCase):
    setUp=ImmersiveInterfaceTests.setUp
    actor=ImmersiveInterfaceTests.actor
    submission=ImmersiveInterfaceTests.submission
    enable=ImmersiveInterfaceTests.enable
    request=ImmersiveInterfaceTests.request

    def test_unrelated_edit_preserves_pending_request_and_captured_rate(self):
        from .test_submission_editing import SubmissionEditingTests
        self.enable(50);item=self.submission();claim=self.request(item)
        self.enable(200);self.client.force_login(self.host)
        data=SubmissionEditingTests.payload(self,item,self.host,title='Corrected title')
        self.assertEqual(self.client.post(self.edit_url,data).status_code,302)
        claim.refresh_from_db()
        self.assertEqual((claim.status,claim.captured_percent),('pending',50))
        self.assertEqual(ImmersiveRequest.objects.count(),1)
