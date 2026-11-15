"""Presentation consolidation keeps existing operation and authorization boundaries."""
from urllib.parse import urlsplit,parse_qs
from unittest.mock import patch
from django.core import signing
from django.test import TransactionTestCase,RequestFactory
from django.urls import reverse,resolve
from django.utils import timezone
from . import test_submission_hotfix as fixture
from . import test_submission_editing as editing
from .models import (BookSubmission, AuditEvent, ThemeClaim, BotmMatch, PersonalTBRMatch, ModifierProvenance,
    DiscordEventDelivery, Team, TeamAssignment, MonthEnrollment)
from .submission_corrections import correction_version
from .navigation_context import contextual_url


class SubmissionWorkspaceTests(TransactionTestCase):
    setUp=fixture.SubmissionHotfixTests.setUp
    actor=editing.SubmissionEditingTests.actor
    submission=editing.SubmissionEditingTests.submission

    def route(self,name,s):
        return reverse(name,args=[self.month.group.slug,self.month.pk,s.pk])

    def state(self):
        return {m.__name__:list(m.objects.order_by('pk').values()) for m in
            (BookSubmission,AuditEvent,ThemeClaim,BotmMatch,PersonalTBRMatch,ModifierProvenance,DiscordEventDelivery)}

    def login(self,role='host'):
        actor=self.actor(role);self.client.force_login(actor);return actor

    def data(self,s,actor,**changes):
        values={'version':correction_version(s,actor),'title':s.title,'author':s.author,
            'submitted_pages':s.approved_pages or s.submitted_pages,'completed_on':s.completed_on.isoformat(),'themes':[]}
        values.update(changes);return values

    def test_five_sections_share_one_summary_and_gets_never_write(self):
        s=self.submission();self.login();before=self.state()
        for section,label in [('overview','Overview'),('edit','Edit Book'),('qualifications','Qualifications'),('review','Review &amp; Scoring'),('history','History')]:
            response=self.client.get(self.route('submission-manage',s)+'?section='+section)
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.context['workspace']['active'],section)
            self.assertContains(response,'aria-label="Submission summary"',count=1)
            self.assertContains(response,'aria-current="page"',count=1)
            for key in ('overview','edit','qualifications','review','history'):self.assertContains(response,'section='+key)
            self.assertEqual(self.state(),before)
        self.assertEqual(self.client.post(self.route('submission-manage',s)+'?section=review',{}).status_code,405)
        self.assertEqual(self.state(),before)

    def test_legacy_editor_review_credit_focused_and_history_urls_remain_adapters(self):
        s=self.submission();self.login()
        for name,active in [('submission-editor','edit'),('submission-edit','edit'),('submission-fix-book','edit'),
            ('submission-fix-pages','edit'),('submission-review','review'),('submission-credit','qualifications'),('submission-history','history')]:
            response=self.client.get(self.route(name,s))
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.context['workspace']['active'],active)
            self.assertContains(response,'Submission Management')
        self.assertNotContains(self.client.get(self.route('submission-editor',s)),'name="book_format"')
        self.assertContains(self.client.get(self.route('submission-editor',s)),'Manual format correction is not supported')

    def test_editor_posts_to_existing_endpoint_and_preserves_signed_preview(self):
        s=self.submission();actor=self.login()
        response=self.client.get(self.route('submission-manage',s)+'?section=edit')
        self.assertEqual(urlsplit(response.context['workspace']['edit_post_url']).path,self.route('submission-editor',s))
        data=self.data(s,actor,submitted_pages=400)
        before=self.state();response=self.client.post(self.route('submission-editor',s),data)
        self.assertEqual(response.context['workspace']['active'],'edit');self.assertTrue(response.context['preview'])
        self.assertEqual(self.state(),before)
        token=response.context['form']['preview_token'].value();self.assertTrue(token)
        self.assertContains(response,'data-unsaved-state')
        data.update(preview_token=token,confirm_approved='on')
        response=self.client.post(self.route('submission-editor',s),data)
        self.assertEqual(response.status_code,302);self.assertEqual(parse_qs(urlsplit(response['Location']).query)['section'],['edit'])
        s.refresh_from_db();self.assertEqual(s.approved_pages,400)
        self.assertEqual(AuditEvent.objects.filter(action='submission.corrected',object_id=str(s.pk)).count(),1)

    def test_errors_keep_values_in_edit_and_summary_uses_saved_values(self):
        s=self.submission();actor=self.login();before=self.state()
        response=self.client.post(self.route('submission-editor',s),self.data(s,actor,author='Unsaved author',completed_on='2026-12-01'))
        self.assertEqual(response.context['workspace']['active'],'edit')
        self.assertEqual(response.context['form']['author'].value(),'Unsaved author')
        self.assertEqual(response.context['workspace']['submission'].author,self.book.author)
        self.assertTrue(response.context['form'].errors)
        self.assertEqual(self.state(),before)

    def test_review_errors_keep_matching_token_and_do_not_change_summary_status(self):
        s=self.submission(status='pending');self.login()
        response=self.client.get(self.route('submission-manage',s)+'?section=review')
        token=response.context['matching_form']['matching_version'].value()
        response=self.client.post(self.route('submission-review',s),{'matching_version':token,'status':'approved','approved_pages':0,'review_notes':'Keep my entered notes'})
        self.assertEqual(response.context['workspace']['active'],'review')
        self.assertEqual(response.context['form']['review_notes'].value(),'Keep my entered notes')
        self.assertEqual(response.context['matching_form']['matching_version'].value(),token)
        self.assertEqual(response.context['workspace']['submission'].status,'pending')

    def test_signed_origin_survives_section_links_post_actions_preview_and_success(self):
        s=self.submission();actor=self.login()
        origin=RequestFactory().get(reverse('review-queue',args=['hotfix',self.month.pk]),{'page':'3','q':'reading'})
        origin.user=actor;origin.resolver_match=resolve(origin.path)
        url=contextual_url(origin,self.route('submission-manage',s)+'?section=edit')
        token=parse_qs(urlsplit(url).query)['nav'][0]
        response=self.client.get(url)
        for item in response.context['workspace']['navigation']:
            self.assertEqual(parse_qs(urlsplit(item['url']).query)['nav'],[token])
        action=response.context['workspace']['edit_post_url']
        self.assertEqual(parse_qs(urlsplit(action).query)['nav'],[token])
        data=self.data(s,actor,submitted_pages=401)
        response=self.client.post(action,data)
        self.assertEqual(response.context['workspace']['active'],'edit')
        data.update(preview_token=response.context['form']['preview_token'].value(),confirm_approved='on')
        response=self.client.post(action,data)
        self.assertEqual(parse_qs(urlsplit(response['Location']).query)['nav'],[token])
        self.assertEqual(parse_qs(urlsplit(response['Location']).query)['section'],['edit'])

    def test_owner_without_review_staffing_sees_no_review_or_candidate_post_controls(self):
        s=self.submission();self.login('owner')
        response=self.client.get(self.route('submission-manage',s)+'?section=review')
        self.assertEqual(response.status_code,200);self.assertNotContains(response,'name="approved_pages"')
        self.assertContains(response,'not available within your current permissions')
        response=self.client.get(self.route('submission-manage',s)+'?section=qualifications')
        self.assertNotContains(response,'Confirm existing candidate')
        self.assertNotContains(response,'name="request_version"')
        self.assertEqual(self.client.post(self.route('submission-review',s),{}).status_code,403)

    def test_leader_has_scoped_book_actions_but_no_exceptional_credit_expansion(self):
        s=self.submission();self.login('team_leader')
        response=self.client.get(self.route('submission-manage',s)+'?section=qualifications')
        self.assertEqual(response.status_code,200)
        self.assertNotContains(response,'Exceptional Credit Correction')
        self.assertEqual(self.client.post(self.route('submission-credit',s),{}).status_code,403)
        TeamAssignment.objects.filter(participant=self.reader,month=self.month).update(ended_at=timezone.now())
        self.assertEqual(self.client.get(self.route('submission-manage',s)).status_code,403)
        self.assertEqual(self.client.get(self.route('submission-editor',s)).status_code,403)

    def test_participating_host_other_team_denied_without_private_summary(self):
        s=self.submission();actor=self.login()
        from .models import Membership
        member=Membership.objects.get(user=actor,group=self.month.group)
        MonthEnrollment.objects.create(month=self.month,participant=member)
        own=Team.objects.create(month=self.month,name='Host Team');other=Team.objects.create(month=self.month,name='Restricted Team')
        TeamAssignment.objects.create(month=self.month,team=own,participant=member)
        TeamAssignment.objects.create(month=self.month,team=other,participant=self.reader)
        response=self.client.get(self.route('submission-manage',s))
        self.assertEqual(response.status_code,403);self.assertNotContains(response,'Restricted Team',status_code=403)

    def test_platform_floater_and_nonparticipating_host_access_and_reader_isolation(self):
        s=self.submission()
        for role in ('platform','floater','host'):
            self.login(role)
            self.assertEqual(self.client.get(self.route('submission-manage',s)).status_code,200)
        self.client.force_login(self.reader.user)
        for section in ('overview','edit','qualifications','review','history'):
            self.assertEqual(self.client.get(self.route('submission-manage',s)+'?section='+section).status_code,403)
        response=self.client.get(self.route('reader-submission',s))
        self.assertEqual(response.status_code,200)
        self.assertNotContains(response,'data-submission-workspace')
        self.assertNotContains(response,'Exceptional Credit Correction')
        self.assertEqual(self.client.get(self.route('submission-history',s)).status_code,200)

    def test_history_pagination_stays_in_section_and_has_no_other_reader_events(self):
        s=self.submission();actor=self.login()
        for n in range(30):AuditEvent.objects.create(group=self.month.group,actor=actor,action='submission.reader_edited',object_type='BookSubmission',object_id=str(s.pk),summary='{}')
        AuditEvent.objects.create(group=self.month.group,actor=actor,action='submission.created',object_type='BookSubmission',object_id='999999',summary='Private other book')
        response=self.client.get(self.route('submission-manage',s)+'?section=history')
        self.assertIn('section=history',response.context['workspace']['history_next'])
        self.assertIn('page=2',response.context['workspace']['history_next'])
        self.assertNotContains(response,'Private other book')
        self.assertEqual(self.client.get(response.context['workspace']['history_next']).status_code,200)

    def test_review_queue_primary_link_and_no_redundant_manage_launcher(self):
        s=self.submission(status='pending');self.login()
        response=self.client.get(reverse('review-queue',args=['hotfix',self.month.pk]))
        self.assertContains(response,'section=review')
        self.assertContains(response,'Review &amp; Scoring')
        self.assertNotContains(response,'>Manage Submission</a>')

    def test_unknown_section_and_cross_context_fail_without_writes(self):
        s=self.submission();self.login();before=self.state()
        self.assertEqual(self.client.get(self.route('submission-manage',s)+'?section=unknown').status_code,400)
        self.assertEqual(self.client.get(reverse('submission-manage',args=['foreign',self.month.pk,s.pk])).status_code,404)
        self.assertEqual(self.state(),before)

    def test_existing_credit_correction_prefills_scoped_target_without_writes(self):
        s=self.submission();self.login();before=self.state()
        response=self.client.get(self.route('submission-manage',s)+'?section=qualifications')
        rows=response.context['workspace']['groups'][1]['rows'];self.assertTrue(rows[0]['correction_url'])
        response=self.client.get(rows[0]['correction_url'])
        form=response.context['sections'][1]['form']
        self.assertEqual(form['operation'].value(),'reverse')
        self.assertEqual(str(form['entry'].value()),str(s.personal_tbr_matches.get().personal_tbr_book_id))
        self.assertEqual(self.state(),before)
        self.assertContains(response,'Generate Preview')
        self.assertNotContains(response,'Confirm Credit Correction')

    def test_qualification_reader_request_review_keeps_its_separate_post(self):
        from datetime import date
        from .models import PersonalTBRBook,normalize_book_identity
        from .reader_submissions import request_qualification,version
        from .matching_intent import selection_token
        book=self.tbr.books.first()
        PersonalTBRBook.objects.filter(pk=book.pk).update(catalog_book=None,catalog_edition=None,title_snapshot='Death Valley',author_snapshot='Karina Halle',normalized_title=normalize_book_identity('Death Valley'),normalized_author=normalize_book_identity('Karina Halle'))
        book.refresh_from_db()
        s=BookSubmission.objects.create(month=self.month,participant=self.reader,title='Death Valley',author='Karin Hale',book_format='ebook',completed_on=date(2026,11,3),submitted_pages=384,approved_pages=384,status='approved')
        match=request_qualification(submission=s,actor=self.reader.user,domain='tbr',token=selection_token(book,self.reader.user),state=version(s,self.reader.user))
        self.login();response=self.client.get(self.route('submission-manage',s)+'?section=qualifications')
        self.assertContains(response,'Approve Qualification');self.assertNotContains(response,'Confirm existing candidate')
        state=response.context['workspace']['reader_request_version'];before=self.state()
        self.assertEqual(before,self.state())
        response=self.client.post(self.route('submission-review',s),{'action':'reader-qualification-review','domain':'tbr','match_id':match.pk,'request_version':state,'decision':'confirmed'})
        self.assertEqual(response.status_code,302);self.assertEqual(parse_qs(urlsplit(response['Location']).query)['section'],['qualifications'])
        s.refresh_from_db();match.refresh_from_db();self.assertEqual(s.approved_pages,384);self.assertTrue(match.is_qualifying)
        after=self.state()
        self.client.post(self.route('submission-review',s),{'action':'reader-qualification-review','domain':'tbr','match_id':match.pk,'request_version':state,'decision':'confirmed'})
        self.assertEqual(after,self.state())

    def test_review_policy_for_approved_pages_remains_existing_behavior(self):
        s=self.submission();self.login()
        response=self.client.get(self.route('submission-manage',s)+'?section=review')
        self.assertContains(response,'use <a href=')
        self.assertContains(response,'Save Review')
        token=response.context['matching_form']['matching_version'].value()
        response=self.client.post(self.route('submission-review',s),{'matching_version':token,'status':'approved','approved_pages':401,'verification_url':'','review_notes':'Existing review policy'})
        self.assertEqual(response.status_code,302)
        s.refresh_from_db();self.assertEqual(s.approved_pages,401)
        self.assertFalse(AuditEvent.objects.filter(action='submission.corrected').exists())

    def test_workspace_void_and_restore_keep_separate_confirmation_routes(self):
        team=Team.objects.create(month=self.month,name='Current Team');TeamAssignment.objects.create(month=self.month,team=team,participant=self.reader)
        s=self.submission();self.login()
        response=self.client.get(self.route('submission-manage',s))
        void_url=response.context['workspace']['void_url']
        self.assertEqual(urlsplit(void_url).path,self.route('submission-remove',s))
        response=self.client.post(void_url,{'reason':'Synthetic void'})
        self.assertEqual(response.status_code,302);s.refresh_from_db();self.assertTrue(s.is_removed)
        response=self.client.get(self.route('submission-manage',s))
        restore_url=response.context['workspace']['restore_url']
        page=self.client.get(restore_url);self.assertEqual(page.status_code,200)
        self.assertTrue(page.context['eligible'])
        response=self.client.post(restore_url,{'confirmation':page.context['confirmation'],'reason':'Synthetic restore'})
        self.assertEqual(response.status_code,302);s.refresh_from_db();self.assertFalse(s.is_removed)

    def test_workspace_void_validation_keeps_reason_and_no_partial_writes(self):
        from django.core.exceptions import ValidationError
        s=self.submission();self.login();before=self.state()
        with patch('core.views.BookSubmission.save',side_effect=ValidationError('Synthetic validation failure')):
            response=self.client.post(self.route('submission-remove',s)+'?workspace=1',{'reason':'Keep my reason'})
        self.assertEqual(response.status_code,400)
        self.assertContains(response,'Keep my reason',status_code=400)
        self.assertContains(response,'data-unsaved-state',status_code=400)
        self.assertEqual(self.state(),before)

    def test_workspace_review_success_returns_to_review_section(self):
        s=self.submission(status='pending');self.login()
        page=self.client.get(self.route('submission-manage',s)+'?section=review')
        response=self.client.post(self.route('submission-review',s),{'workspace_return':'1',
            'matching_version':page.context['matching_form']['matching_version'].value(),'status':'approved',
            'approved_pages':s.submitted_pages,'verification_url':'','review_notes':'Reviewed'})
        self.assertEqual(response.status_code,302)
        self.assertEqual(parse_qs(urlsplit(response['Location']).query)['section'],['review'])
