"""Phase 7C delegation uses current scope and existing signed services."""
from datetime import date
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from . import test_submission_editing as editing
from . import test_submission_hotfix as fixture
from . import test_matching_phase2a as matching
from .models import (BookSubmission, Membership, MonthEnrollment, ChallengeStaffAssignment,
    Team, TeamAssignment, BotmMatch, BotmBook, PersonalTBRMatch, AuditEvent, ThemeClaim, MonthTheme,
    ModifierProvenance, DiscordEventDelivery, ChallengeMonth)
from .administrative_matching import can_manage_credit, preview_credit, apply_credit
from .submission_corrections import correction_version, preview_correction, apply_previewed_correction
from .submission_review_matching import apply_unified_review, review_token
from .matching_resolution import review_submission_with_matches
from .permissions import can_review_submission

class DelegatedSubmissionPermissionsTests(TransactionTestCase):
    setUp = fixture.SubmissionHotfixTests.setUp
    actor = editing.SubmissionEditingTests.actor
    submission = editing.SubmissionEditingTests.submission
    botm = matching.MatchingPhase2ATests.botm

    def route(self,name,s):
        return reverse(name,args=[s.month.group.slug,s.month_id,s.pk])

    def operation(self,domain='botm',operation='assign'):
        if domain=='botm':
            book=self.botm()
            BotmBook.objects.create(month=self.month,position=2,title_snapshot='Other book',author_snapshot='Other',page_count_snapshot=100)
        else:book=self.tbr.books.first()
        return dict(domain=domain,operation=operation,entry_id=book.pk,reason='Verified historical evidence',acknowledge=False)

    def state(self):
        return {m.__name__:list(m.objects.order_by('pk').values()) for m in
            (BookSubmission,BotmMatch,PersonalTBRMatch,ThemeClaim,ModifierProvenance,AuditEvent,DiscordEventDelivery)}

    def test_each_delegated_role_both_domain_preview_apply_reverse_restore(self):
        s=matching.MatchingPhase2ATests.submission(self,title='Different book',author='Writer',pages=350)
        actors=[self.actor(role) for role in ('host','floater','team_leader','owner','platform')]
        for domain in ('botm','tbr'):
            data=self.operation(domain)
            model=BotmMatch if domain=='botm' else PersonalTBRMatch
            for index,actor in enumerate(actors):
                data['operation']='assign' if index==0 else 'restore'
                before=self.state()
                impact,token=preview_credit(submission=s,actor=actor,**data)
                self.assertEqual(before,self.state());self.assertTrue(can_manage_credit(actor,s))
                self.client.force_login(actor)
                self.assertContains(self.client.get(self.route('submission-manage',s)+'?section=qualifications'),'Exceptional Credit Override')
                apply_credit(submission=s,actor=actor,token=token,**data)
                self.assertTrue(model.objects.get(submission=s).is_qualifying)
                data['operation']='reverse'
                impact,token=preview_credit(submission=s,actor=actor,**data)
                apply_credit(submission=s,actor=actor,token=token,**data)
                self.assertFalse(model.objects.get(submission=s).is_qualifying)
            data['operation']='restore'
            _,token=preview_credit(submission=s,actor=actor,**data)
            apply_credit(submission=s,actor=actor,token=token,**data)
        s.refresh_from_db();self.assertEqual(s.approved_pages,350)
        self.assertEqual(AuditEvent.objects.filter(action='submission.credit_assign').count(),2)

    def test_reader_unrelated_group_inactive_actor_denied_get_post_service(self):
        s=self.submission();data=self.operation()
        other=get_user_model().objects.create_user('unrelated')
        from .models import ReadingGroup
        group=ReadingGroup.objects.create(name='Unrelated',slug='unrelated')
        Membership.objects.create(group=group,user=other,role='owner')
        inactive=self.actor('host');get_user_model().objects.filter(pk=inactive.pk).update(is_active=False);inactive.refresh_from_db()
        for actor in (self.reader.user,other,inactive):
            self.assertFalse(can_manage_credit(actor,s));self.assertFalse(can_review_submission(actor,s))
            with self.assertRaises(PermissionDenied):preview_credit(submission=s,actor=actor,**data)
            self.client.force_login(actor)
            for route in ('submission-credit','submission-editor','submission-review'):
                self.assertIn(self.client.get(self.route(route,s)).status_code,(302,403))
                self.assertIn(self.client.post(self.route(route,s),{}).status_code,(302,403))

    def test_team_transfer_and_revoked_leader_invalidate_credit_and_book_previews(self):
        s=self.submission();a=self.actor('team_leader');data=self.operation()
        _,credit=preview_credit(submission=s,actor=a,**data)
        version=correction_version(s,a);changes={'page_count':400}
        preview=preview_correction(submission=s,actor=a,changes=changes,version=version)
        TeamAssignment.objects.filter(month=self.month,participant=self.reader).update(ended_at=timezone.now())
        before=self.state()
        with self.assertRaises(PermissionDenied):apply_credit(submission=s,actor=a,token=credit,**data)
        with self.assertRaises(PermissionDenied):apply_previewed_correction(submission=s,actor=a,changes=changes,version=version,preview_token=preview['token'],confirm_approved=True)
        self.assertEqual(before,self.state())
        TeamAssignment.objects.create(month=self.month,team=self.team,participant=self.reader)
        _,token=preview_credit(submission=s,actor=a,**data)
        ChallengeStaffAssignment.objects.filter(membership__user=a).update(ended_at=timezone.now())
        with self.assertRaises(PermissionDenied):apply_credit(submission=s,actor=a,token=token,**data)

    def test_floater_revocation_and_departed_membership_invalidate_previews(self):
        s=self.submission();a=self.actor('floater');data=self.operation()
        _,token=preview_credit(submission=s,actor=a,**data)
        staffing=ChallengeStaffAssignment.objects.get(membership__user=a)
        staffing.ended_at=timezone.now();staffing.save()
        with self.assertRaises(PermissionDenied):apply_credit(submission=s,actor=a,token=token,**data)
        staffing.ended_at=None;staffing.save();_,token=preview_credit(submission=s,actor=a,**data)
        Membership.objects.filter(user=a).update(is_active=False)
        with self.assertRaises(PermissionDenied):apply_credit(submission=s,actor=a,token=token,**data)

    def test_participating_host_scope_is_rechecked_after_preview(self):
        s=self.submission();a=self.actor('host');data=self.operation()
        _,token=preview_credit(submission=s,actor=a,**data)
        member=Membership.objects.get(user=a)
        MonthEnrollment.objects.create(month=self.month,participant=member)
        team=Team.objects.create(month=self.month,name='Host team')
        TeamAssignment.objects.create(month=self.month,team=team,participant=member)
        self.assertFalse(can_manage_credit(a,s))
        with self.assertRaises(PermissionDenied):apply_credit(submission=s,actor=a,token=token,**data)
        TeamAssignment.objects.create(month=self.month,team=team,participant=self.reader)
        self.assertTrue(can_manage_credit(a,s))
        _,token=preview_credit(submission=s,actor=a,**data)
        MonthEnrollment.objects.filter(month=self.month,participant=member).update(is_active=False)
        with self.assertRaises(PermissionDenied):apply_credit(submission=s,actor=a,token=token,**data)

    def test_cross_challenge_and_group_credit_endpoints_denied(self):
        s=self.submission();a=self.actor('floater');self.client.force_login(a)
        foreign=ChallengeMonth.objects.create(group=self.month.group,name='Other',status='active')
        other=BookSubmission.objects.create(month=foreign,participant=self.reader,title='Other',author='Other',book_format='ebook',completed_on=date(2026,11,3),submitted_pages=100,status='pending')
        self.assertEqual(self.client.get(self.route('submission-manage',other)).status_code,403)
        self.assertEqual(self.client.post(self.route('submission-credit',other),{}).status_code,403)
        url=reverse('submission-credit',args=['not-this-group',s.month_id,s.pk])
        self.assertEqual(self.client.post(url,{}).status_code,404)

    def test_lifecycle_and_departed_reader_do_not_gain_new_credit(self):
        s=self.submission();a=self.actor('team_leader');data=self.operation()
        _,token=preview_credit(submission=s,actor=a,**data)
        MonthEnrollment.objects.filter(month=self.month,participant=self.reader).update(is_active=False)
        with self.assertRaises(PermissionDenied):apply_credit(submission=s,actor=a,token=token,**data)
        # Challenge-wide staff retain historical authority but cannot grant credit.
        host=self.actor('host')
        with self.assertRaises(ValidationError):preview_credit(submission=s,actor=host,**data)
        Membership.objects.filter(pk=self.reader.pk).update(is_active=False)
        with self.assertRaises(ValidationError):preview_credit(submission=s,actor=host,**data)
        MonthEnrollment.objects.filter(month=self.month,participant=self.reader).update(is_active=True)
        for status in ('upcoming','completed','archived'):
            ChallengeMonth.objects.filter(pk=self.month.pk).update(status=status)
            s.refresh_from_db();s.month.refresh_from_db()
            with self.assertRaises(ValidationError):preview_credit(submission=s,actor=host,**data)

    def test_approved_review_forged_scoring_changes_fail_for_all_roles(self):
        s=self.submission()
        for role in ('host','floater','team_leader','platform'):
            a=self.actor(role);self.client.force_login(a)
            for fields in ({'approved_pages':401},{'status':'rejected'}):
                before=self.state()
                data={'matching_version':review_token(s,a),'status':'approved','approved_pages':350,'verification_url':'','review_notes':'Attempt',**fields}
                response=self.client.post(self.route('submission-review',s),data)
                self.assertEqual(response.status_code,200);self.assertContains(response,'signed Edit Book correction preview')
                self.assertEqual(before,self.state())

    def test_approved_service_guard_and_notes_do_not_recalculate(self):
        s=self.submission();a=self.actor('team_leader');original=self.state()
        s.approved_pages=401
        with self.assertRaisesMessage(ValidationError,'signed Edit Book'):
            review_submission_with_matches(s,[],a,timezone.now())
        self.assertEqual(original,self.state());s.refresh_from_db()
        with self.assertRaises(ValidationError):apply_unified_review(s,[],a,timezone.now(),version=review_token(s,a),confirmations=['botm'])
        theme=MonthTheme.objects.create(month=self.month,name='Theme',starts_on=self.month.starts_on,ends_on=self.month.ends_on)
        claim=ThemeClaim.objects.create(submission=s,theme=theme,status='pending');claim.status='approved'
        with self.assertRaises(ValidationError):review_submission_with_matches(s,[claim],a,timezone.now())
        s.review_notes='Verified evidence'
        with patch('core.scoring.refresh_submission_score',side_effect=AssertionError('No recalculation')):
            result=review_submission_with_matches(s,[],a,timezone.now())
        self.assertEqual(result.approved_pages,350);self.assertEqual(result.review_notes,'Verified evidence')
        self.assertTrue(AuditEvent.objects.filter(action='submission.review_changed').exists())

    def test_leader_signed_page_preview_stale_noop_and_confirm(self):
        s=self.submission();a=self.actor('team_leader');version=correction_version(s,a)
        changes={'page_count':400};before=self.state()
        preview=preview_correction(submission=s,actor=a,changes=changes,version=version)
        self.assertEqual(before,self.state())
        with self.assertRaises(ValidationError):
            apply_previewed_correction(submission=s,actor=a,changes=changes,version=version,preview_token=preview['token'])
        result=apply_previewed_correction(submission=s,actor=a,changes=changes,version=version,preview_token=preview['token'],confirm_approved=True)
        s.refresh_from_db();self.assertEqual(s.approved_pages,400)
        self.assertTrue(AuditEvent.objects.filter(action='submission.corrected').exists())
        before=self.state()
        with self.assertRaises(ValidationError):
            apply_previewed_correction(submission=s,actor=a,changes=changes,version=version,preview_token=preview['token'],confirm_approved=True)
        with self.assertRaises(ValidationError):
            preview_correction(submission=s,actor=a,changes=changes,version=correction_version(s,a))
        self.assertEqual(before,self.state())

    def test_botm_legacy_endpoint_leader_and_floater_return_to_workspace(self):
        from .botm_matching import synchronize_submission
        book=self.botm();s=self.submission();s.title=book.title_snapshot+' Extended Edition';s.author=book.author_snapshot;s.save()
        synchronize_submission(s);match=BotmMatch.objects.get(submission=s)
        a=self.actor('team_leader');self.client.force_login(a)
        url=reverse('botm-match-review',args=[self.month.group.slug,self.month.pk,match.pk,'confirm'])
        response=self.client.post(url,{})
        self.assertEqual(response.status_code,302);self.assertIn('section=qualifications',response['Location'])
        match.refresh_from_db();self.assertTrue(match.is_qualifying)
        TeamAssignment.objects.filter(participant=self.reader,month=self.month).update(ended_at=timezone.now())
        self.assertEqual(self.client.post(url,{}).status_code,403)

    def test_delegated_reassignment_requires_explicit_reversal_and_no_duplicate(self):
        a=self.actor('floater');data=self.operation()
        first=matching.MatchingPhase2ATests.submission(self,title='Reviewed first book',author='Writer')
        second=matching.MatchingPhase2ATests.submission(self,title='Reviewed replacement book',author='Writer')
        _,token=preview_credit(submission=first,actor=a,**data)
        apply_credit(submission=first,actor=a,token=token,**data)
        before=self.state()
        with self.assertRaises(ValidationError):preview_credit(submission=second,actor=a,**data)
        self.assertEqual(before,self.state())
        reverse={**data,'operation':'reverse'}
        _,token=preview_credit(submission=first,actor=a,**reverse)
        apply_credit(submission=first,actor=a,token=token,**reverse)
        _,token=preview_credit(submission=second,actor=a,**data)
        apply_credit(submission=second,actor=a,token=token,**data)
        self.assertEqual(BotmMatch.objects.filter(is_qualifying=True).count(),1)
        self.assertTrue(BotmMatch.objects.get(submission=second).is_qualifying)
        self.assertFalse(BotmMatch.objects.get(submission=first).is_qualifying)
