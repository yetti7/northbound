from unittest.mock import patch
from django.utils import timezone
from django.test import TestCase
from django.urls import reverse
from django.core.exceptions import ValidationError, PermissionDenied
from . import test_matching_phase2a as fixture
from .models import (BookSubmission,BotmBook,BotmMatch,PersonalTBRMatch,AuditEvent,ModifierProvenance,
                     HardcoverSyncOutbox,CatalogBook,Membership,MonthEnrollment,ChallengeMonth)
from .administrative_matching import preview_credit,apply_credit
from .botm_matching import synchronize_submission as sync_botm
from .personal_tbr_matching import synchronize_submission as sync_tbr


class AdministrativeMatchingTests(TestCase):
    setUp=fixture.MatchingPhase2ATests.setUp
    actor=fixture.MatchingPhase2ATests.actor
    submission=fixture.MatchingPhase2ATests.submission
    botm=fixture.MatchingPhase2ATests.botm

    def setup_operation(self):
        self.admin=self.actor('host');self.target=self.botm()
        BotmBook.objects.create(month=self.month,position=2,title_snapshot='Another title',author_snapshot='Author',page_count_snapshot=100)
        self.s=self.submission(title='Different book',author='Different author')

    def data(self,domain='botm',operation='assign',**overrides):
        data=dict(domain=domain,entry_id=self.target.pk if domain=='botm' else self.tbr.books.first().pk,
                  operation=operation,reason='Reviewed the actual book and confirmed eligibility.',acknowledge=False)
        data.update(overrides);return data

    def execute(self,**data):
        impact,token=preview_credit(submission=self.s,actor=self.admin,**data)
        result=apply_credit(submission=self.s,actor=self.admin,token=token,**data)
        self.assertEqual(impact,result);self.s.refresh_from_db();return result

    def test_both_domains_identity_pages_and_audit_preserved(self):
        self.setup_operation();before=BookSubmission.objects.filter(pk=self.s.pk).values().get()
        impact,token=preview_credit(submission=self.s,actor=self.admin,**self.data())
        self.assertFalse(BotmMatch.objects.exists());self.assertFalse(AuditEvent.objects.filter(action='submission.credit_assign').exists())
        self.assertEqual(impact['after']['scored_pages'],408)
        apply_credit(submission=self.s,actor=self.admin,token=token,**self.data())
        self.execute(**self.data('tbr'))
        self.s.refresh_from_db();self.assertEqual((self.s.approved_pages,self.s.bonus_pages,self.s.final_scored_pages),(158,270,428))
        after=BookSubmission.objects.filter(pk=self.s.pk).values().get()
        for key,value in before.items():
            if key not in {'bonus_pages','final_scored_pages'}:self.assertEqual(after[key],value,key)
        self.assertEqual(AuditEvent.objects.filter(action='submission.credit_assign').count(),2)
        sync_botm(self.s);sync_tbr(self.s)
        self.assertTrue(BotmMatch.objects.get(submission=self.s).is_qualifying)
        self.assertTrue(PersonalTBRMatch.objects.get(submission=self.s).is_qualifying)
        self.assertFalse(HardcoverSyncOutbox.objects.exists())

    def test_reason_and_identity_conflict_acknowledgement(self):
        self.setup_operation()
        with self.assertRaises(ValidationError):preview_credit(submission=self.s,actor=self.admin,**self.data(reason=' '))
        self.s.catalog_book=self.book;self.s.save()
        other=CatalogBook.objects.create(provider='hardcover',provider_book_id='other',title='Other',author='Other')
        self.target.catalog_book=other;self.target.save()
        with self.assertRaisesMessage(ValidationError,'Acknowledge'):
            preview_credit(submission=self.s,actor=self.admin,**self.data())
        self.execute(**self.data(acknowledge=True))
        self.assertTrue(BotmMatch.objects.get(submission=self.s).decision_evidence['identity_conflict_acknowledged'])

    def test_reverse_restore_both_domains_remain_durable(self):
        self.setup_operation()
        for domain,model in [('botm',BotmMatch),('tbr',PersonalTBRMatch)]:
            self.execute(**self.data(domain));match=model.objects.get(submission=self.s);pk=match.pk
            original_evidence=match.decision_evidence
            self.execute(**self.data(domain,'reverse'));sync_botm(self.s);sync_tbr(self.s)
            match.refresh_from_db();self.assertFalse(match.is_qualifying);self.assertEqual(match.status,'rejected')
            self.assertEqual(match.decision_evidence['previous_decision'],original_evidence)
            self.execute(**self.data(domain,'restore'))
            match.refresh_from_db();self.assertTrue(match.is_qualifying);self.assertEqual(match.pk,pk)
        self.assertEqual(ModifierProvenance.objects.filter(submission=self.s).count(),2)
        self.assertEqual(AuditEvent.objects.filter(action='submission.credit_reverse').count(),2)

    def test_permissions_each_preview_and_apply(self):
        self.setup_operation();_,token=preview_credit(submission=self.s,actor=self.admin,**self.data())
        for role in ['floater','team_leader']:
            actor=self.actor(role)
            with self.assertRaises(PermissionDenied):preview_credit(submission=self.s,actor=actor,**self.data())
            with self.assertRaises(PermissionDenied):apply_credit(submission=self.s,actor=actor,token=token,**self.data())
            self.client.force_login(actor)
            route=reverse('submission-credit',args=[self.month.group.slug,self.month.pk,self.s.pk])
            self.assertEqual(self.client.get(route).status_code,403)
            self.assertEqual(self.client.post(route,{'domain':'botm','action':'apply'}).status_code,403)
        owner=self.actor('owner');preview_credit(submission=self.s,actor=owner,**self.data())
        from .models import ChallengeStaffAssignment
        ChallengeStaffAssignment.objects.filter(membership__user=self.admin).update(ended_at=timezone.now())
        with self.assertRaises(PermissionDenied):apply_credit(submission=self.s,actor=self.admin,token=token,**self.data())

    def test_stale_preview_repeat_concurrency_and_config_changes(self):
        self.setup_operation();data=self.data();_,token=preview_credit(submission=self.s,actor=self.admin,**data)
        BookSubmission.objects.filter(pk=self.s.pk).update(review_notes='Another reviewer changed it')
        # The operation state must include metadata as well as scores.
        with self.assertRaises(ValidationError):apply_credit(submission=self.s,actor=self.admin,token=token,**data)
        _,first=preview_credit(submission=self.s,actor=self.admin,**data)
        _,second=preview_credit(submission=self.s,actor=self.admin,**data)
        apply_credit(submission=self.s,actor=self.admin,token=first,**data)
        with self.assertRaises(ValidationError):apply_credit(submission=self.s,actor=self.admin,token=second,**data)
        self.assertEqual(AuditEvent.objects.filter(action='submission.credit_assign').count(),1)

    def test_duplicate_and_other_submission_credit_cannot_be_stolen(self):
        self.setup_operation();self.execute(**self.data())
        with self.assertRaises(ValidationError):preview_credit(submission=self.s,actor=self.admin,**self.data())
        other=self.submission(title='Another read')
        with self.assertRaises(ValidationError):preview_credit(submission=other,actor=self.admin,**self.data())
        self.assertEqual(BotmMatch.objects.filter(is_qualifying=True).count(),1)

    def test_withdrawal_suspends_and_reactivation_preserves_decision(self):
        self.setup_operation();self.execute(**self.data());match=BotmMatch.objects.get(submission=self.s)
        evidence=match.decision_evidence
        self.s.is_removed=True;self.s.save();sync_botm(self.s)
        match.refresh_from_db();self.assertFalse(match.is_qualifying)
        self.s.is_removed=False;self.s.save();sync_botm(self.s)
        match.refresh_from_db();self.assertTrue(match.is_qualifying);self.assertEqual(match.decision_evidence,evidence)

    def test_frozen_completion_reversal_is_blocked_without_writes(self):
        self.setup_operation();BotmBook.objects.filter(month=self.month,position=2).delete()
        self.execute(**self.data());before=BotmMatch.objects.values().get()
        with self.assertRaisesMessage(ValidationError,'frozen completion'):
            preview_credit(submission=self.s,actor=self.admin,**self.data(operation='reverse'))
        self.assertEqual(BotmMatch.objects.values().get(),before)
        self.assertEqual(AuditEvent.objects.filter(action='submission.credit_reverse').count(),0)

    def test_scope_lifecycle_and_ineligible_submission(self):
        self.setup_operation()
        other=ChallengeMonth.objects.create(group=self.month.group,name='Other',status='active')
        book=BotmBook.objects.create(month=other,position=1,title_snapshot='Other',author_snapshot='Writer',page_count_snapshot=100)
        with self.assertRaises(ValidationError):preview_credit(submission=self.s,actor=self.admin,**self.data(entry_id=book.pk))
        self.s.status='pending';self.s.save()
        with self.assertRaises(ValidationError):preview_credit(submission=self.s,actor=self.admin,**self.data())
        self.s.status='approved';self.s.save();self.month.transition_to('finalizing');self.month.transition_to('completed')
        with self.assertRaises(ValidationError):preview_credit(submission=self.s,actor=self.admin,**self.data())

    def test_unexpected_cross_domain_mutation_rolls_back(self):
        self.setup_operation();from .botm_matching import _finalize_submission_matches
        def bad(s,matches):
            _finalize_submission_matches(s,matches)
            BookSubmission.objects.filter(pk=s.pk).update(title='Unexpected mutation')
        with patch('core.botm_matching._finalize_submission_matches',side_effect=bad):
            with self.assertRaisesMessage(ValidationError,'protected'):
                preview_credit(submission=self.s,actor=self.admin,**self.data())
        self.s.refresh_from_db();self.assertEqual(self.s.title,'Different book');self.assertFalse(BotmMatch.objects.exists())

    def test_cross_reader_tbr_and_out_of_scope_host_are_denied(self):
        from django.contrib.auth import get_user_model
        from .models import PersonalTBR,PersonalTBRBook
        self.setup_operation()
        member=Membership.objects.create(group=self.month.group,user=get_user_model().objects.create_user('foreign-reader'),display_name='Other Reader')
        tbr=PersonalTBR.objects.create(enrollment=MonthEnrollment.objects.create(month=self.month,participant=member))
        book=PersonalTBRBook.objects.create(personal_tbr=tbr,position=1,title_snapshot='Foreign book',author_snapshot='Writer')
        tbr.confirmed_at=timezone.now();tbr.save()
        with self.assertRaises(ValidationError):preview_credit(submission=self.s,actor=self.admin,**self.data('tbr',entry_id=book.pk))
        from .models import ChallengeStaffAssignment
        ChallengeStaffAssignment.objects.filter(membership__user=self.admin).update(ended_at=timezone.now())
        with self.assertRaises(PermissionDenied):preview_credit(submission=self.s,actor=self.admin,**self.data())

    def test_frozen_tbr_completion_is_not_rewritten_or_reversed(self):
        self.setup_operation();self.execute(**self.data('tbr'))
        for book in self.tbr.books.exclude(position=1):
            other=self.submission(title=book.title_snapshot,author=book.author_snapshot,pages=100)
            sync_tbr(other)
        from .models import PersonalTBRCompletionAward
        before=list(PersonalTBRCompletionAward.objects.values())
        self.assertTrue(before[0]['is_qualifying'])
        with self.assertRaisesMessage(ValidationError,'frozen completion'):
            preview_credit(submission=self.s,actor=self.admin,**self.data('tbr','reverse'))
        self.assertEqual(list(PersonalTBRCompletionAward.objects.values()),before)
        self.assertTrue(PersonalTBRMatch.objects.get(submission=self.s).is_qualifying)

    def test_administrator_can_explicitly_restore_reviewer_rejection(self):
        from .matching_resolution import confirm_suggestion
        self.setup_operation()
        self.s.title='Dreadful Girls Halloween 99 Extended Edition';self.s.author='Rosalia Kings';self.s.save()
        sync_botm(self.s);match=BotmMatch.objects.get(submission=self.s)
        confirm_suggestion(match=match,actor=self.actor('floater'),decision='rejected')
        match.refresh_from_db();previous=match.decision_evidence
        self.execute(**self.data(operation='restore'))
        sync_botm(self.s);match.refresh_from_db()
        self.assertTrue(match.is_qualifying)
        self.assertEqual(match.decision_evidence['previous_decision'],previous)
        self.assertEqual(match.reviewer_id,self.admin.pk)

    def test_browser_preview_required_and_apply_flow(self):
        self.setup_operation();self.client.force_login(self.admin)
        url=reverse('submission-credit',args=[self.month.group.slug,self.month.pk,self.s.pk])
        response=self.client.get(url);self.assertContains(response,'Book of the Month');self.assertContains(response,'Personal TBR')
        payload={'domain':'botm','action':'preview','botm-operation':'assign','botm-entry':self.target.pk,'botm-reason':'Verified credit.'}
        response=self.client.post(url,payload);self.assertContains(response,'Correction preview')
        token=response.context['sections'][0]['form']['preview_token'].value()
        self.assertFalse(BotmMatch.objects.exists())
        payload.update({'action':'apply','botm-preview_token':token})
        self.assertContains(self.client.post(url,payload),'Confirm the preview')
        payload['botm-confirm']='on';self.assertEqual(self.client.post(url,payload).status_code,302)
        self.assertTrue(BotmMatch.objects.get(submission=self.s).is_qualifying)
