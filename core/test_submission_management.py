"""Focused management uses the existing correction engine and permissions."""
import json
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import datetime, timezone as utc
from django.test import TransactionTestCase
from django.urls import reverse
from . import test_submission_hotfix as fixture
from . import test_submission_editing as editing
from . import test_submission_association as association
from .models import BookSubmission, AuditEvent, MonthTheme, ReadingGroup, PersonalTBRMatch, TeamAssignment, MonthEnrollment
from .submission_corrections import correction_version


class SubmissionManagementTests(TransactionTestCase):
    setUp = fixture.SubmissionHotfixTests.setUp
    actor = editing.SubmissionEditingTests.actor
    submission = editing.SubmissionEditingTests.submission
    selection = association.SubmissionAssociationTests.selection
    metadata = association.SubmissionAssociationTests.metadata
    safety_state = association.SubmissionAssociationTests.safety_state

    def route_url(self, name, item):
        return reverse(name, args=['hotfix', self.month.pk, item.pk])

    def data(self, item, actor, workflow, **changes):
        values = {'version': correction_version(item, actor)}
        if workflow == 'book':
            values.update(title=item.title, author=item.author, edition='', association_mode='')
        else:
            values.update(submitted_pages=item.approved_pages, themes=[])
        values.update(changes)
        return values

    def test_panel_forms_permissions_and_existing_tbr_adjudication(self):
        item=self.submission();host=self.actor();self.client.force_login(host)
        response=self.client.get(self.route_url('submission-manage',item))
        for label in ['Edit Submission','Review Credit','Advanced Options']:
            self.assertContains(response,label)
        self.assertContains(response,'<summary>View History</summary>',html=True)
        pages=self.client.get(self.route_url('submission-fix-pages',item))
        self.assertNotContains(pages,'data-edit-association')
        self.assertNotIn('title',pages.context['form'].fields)
        self.assertNotIn('edition',pages.context['form'].fields)
        book=self.client.get(self.route_url('submission-fix-book',item))
        self.assertNotIn('submitted_pages',book.context['form'].fields)
        self.assertNotIn('themes',book.context['form'].fields)
        self.assertContains(book,'Link Only')
        match=item.personal_tbr_matches.get()
        PersonalTBRMatch.objects.filter(pk=match.pk).update(status='pending_review',is_qualifying=False)
        response=self.client.get(self.route_url('submission-manage',item))
        self.assertContains(response,'Confirm existing candidate')
        owner=self.actor('owner');self.client.force_login(owner)
        self.assertNotContains(self.client.get(self.route_url('submission-manage',item)),'Confirm existing candidate')
        self.client.force_login(self.reader.user)
        for name in ['submission-manage','submission-fix-book','submission-fix-pages']:
            self.assertEqual(self.client.get(self.route_url(name,item)).status_code,403)
        self.client.force_login(host)
        self.assertEqual(self.client.get(reverse('submission-manage',args=['wrong',self.month.pk,item.pk])).status_code,404)

    def test_focused_post_allowlists_reject_unrelated_fields_without_writes(self):
        item=self.submission();actor=self.actor();self.client.force_login(actor)
        before=self.safety_state()
        for workflow,field,value in [('pages','title','Changed'),('pages','edition',self.edition.pk),
                ('pages','association_mode','only'),('book','submitted_pages',999),
                ('book','themes',['999']),('book','completed_on','2026-11-04'),('book','theme_response_999','Injected')]:
            with self.subTest(workflow=workflow,field=field):
                response=self.client.post(self.route_url('submission-fix-'+workflow,item),self.data(item,actor,workflow,**{field:value}))
                self.assertContains(response,'does not accept those fields')
                self.assertEqual(self.safety_state(),before)

    def test_pages_theme_preview_and_confirmation_preserve_identity(self):
        item=self.submission();actor=self.actor();self.client.force_login(actor)
        theme=MonthTheme.objects.create(month=self.month,name='25 percent',starts_on=self.month.starts_on,
            ends_on=self.month.ends_on,bonus_percentage=25)
        data=self.data(item,actor,'pages',submitted_pages=400,themes=[theme.pk])
        url=self.route_url('submission-fix-pages',item)
        response=self.client.post(url,data)
        self.assertContains(response,'Selected operation: Fix Pages / Themes')
        self.assertContains(response,'Matching and bonus impact')
        self.assertNotContains(response,'<pre>')
        item.refresh_from_db();self.assertEqual(item.approved_pages,350)
        data.update(preview_token=response.context['form']['preview_token'].value(),confirm_approved='on')
        self.assertEqual(self.client.post(url,data).status_code,302)
        item.refresh_from_db();self.assertEqual(item.approved_pages,400)
        self.assertIsNone(item.catalog_book_id)
        self.assertEqual((item.title,item.author),(self.book.title,self.book.author))
        self.assertEqual(item.theme_claims.get().award_bonus_pages,100)

    def test_link_only_preview_preserves_scoring_and_requires_valid_selection(self):
        item=self.submission();actor=self.actor();selected,_=self.selection();self.audio=False
        self.client.force_login(actor);url=self.route_url('submission-fix-book',item)
        missing=self.client.post(url,self.data(item,actor,'book',association_mode='only'))
        self.assertContains(missing,'requires a new Hardcover edition')
        unspecified=self.client.post(url,self.data(item,actor,'book',edition=selected.pk))
        self.assertContains(unspecified,'Choose Link Only or Update Book Information')
        data=self.data(item,actor,'book',edition=selected.pk,association_mode='only')
        before=self.safety_state()
        with patch('core.views._staff_tbr_search_token',return_value='synthetic'),patch(
                'core.submission_corrections.fetch_edition_metadata',side_effect=self.metadata):
            response=self.client.post(url,data)
            self.assertContains(response,'Selected operation: Link Only')
            self.assertContains(response,'Hardcover #202')
            token=response.context['form']['preview_token'].value()
            self.assertEqual(self.safety_state(),before)
            altered={**data,'edition':'','association_mode':'update','preview_token':token,'confirm_approved':'on','confirm_work':'on'}
            self.assertEqual(self.client.post(url,altered).status_code,200)
            self.assertEqual(self.safety_state(),before)
            data.update(preview_token=token,confirm_approved='on',confirm_work='on')
            self.assertEqual(self.client.post(url,data).status_code,302)
        item.refresh_from_db()
        original=next(r for r in before['BookSubmission'] if r['id']==item.pk)
        self.assertEqual((item.approved_pages,item.final_scored_pages),(original['approved_pages'],original['final_scored_pages']))
        self.assertEqual(item.catalog_edition_id,selected.pk)
        after=self.safety_state()
        for name in before:
            if name not in {'BookSubmission','AuditEvent'}:self.assertEqual(before[name],after[name],name)

    def test_update_book_information_requires_different_work_and_approved_confirmation(self):
        item=self.submission();actor=self.actor();selected,_=self.selection();self.audio=False
        self.client.force_login(actor);url=self.route_url('submission-fix-book',item)
        data=self.data(item,actor,'book',edition=selected.pk,association_mode='update')
        with patch('core.views._staff_tbr_search_token',return_value='synthetic'),patch(
                'core.submission_corrections.fetch_edition_metadata',side_effect=self.metadata):
            response=self.client.post(url,data)
            self.assertContains(response,'A different Hardcover work is selected')
            self.assertContains(response,'Update Book Information')
            data['preview_token']=response.context['form']['preview_token'].value()
            self.assertEqual(self.client.post(url,data).status_code,200)
            data['confirm_approved']='on'
            self.assertEqual(self.client.post(url,data).status_code,200)
            data['confirm_work']='on'
            self.assertEqual(self.client.post(url,data).status_code,302)
        item.refresh_from_db();self.assertEqual((item.title,item.approved_pages),('Selected book',673))

    def test_focused_noop_stale_and_tampered_preview_fail_without_correction(self):
        item=self.submission();actor=self.actor();self.client.force_login(actor)
        for workflow in ['book','pages']:
            response=self.client.post(self.route_url('submission-fix-'+workflow,item),self.data(item,actor,workflow))
            self.assertContains(response,'No changes to save')
        url=self.route_url('submission-fix-pages',item);data=self.data(item,actor,'pages',submitted_pages=400)
        response=self.client.post(url,data);token=response.context['form']['preview_token'].value()
        before=self.safety_state()
        tampered={**data,'preview_token':token+'tampered','confirm_approved':'on'}
        self.assertEqual(self.client.post(url,tampered).status_code,200)
        self.assertEqual(self.safety_state(),before)
        BookSubmission.objects.filter(pk=item.pk).update(approved_pages=351)
        changed=self.safety_state()
        self.assertEqual(self.client.post(url,{**data,'preview_token':token,'confirm_approved':'on'}).status_code,200)
        self.assertEqual(self.safety_state(),changed)

    def test_metadata_edit_requires_explicit_update_and_cannot_change_pages(self):
        item=self.submission();actor=self.actor();self.client.force_login(actor)
        url=self.route_url('submission-fix-book',item)
        response=self.client.post(url,self.data(item,actor,'book',title='Corrected title'))
        self.assertContains(response,'Choose Update Book Information')
        data=self.data(item,actor,'book',title='Corrected title',association_mode='update')
        with patch('core.submission_corrections.fetch_edition_metadata') as provider:
            response=self.client.post(url,data)
            self.assertContains(response,'Update Book Information')
            data.update(preview_token=response.context['form']['preview_token'].value(),confirm_approved='on')
            self.assertEqual(self.client.post(url,data).status_code,302)
            provider.assert_not_called()
        item.refresh_from_db()
        self.assertEqual((item.title,item.approved_pages,item.catalog_book_id),('Corrected title',350,None))

    def test_focused_permissions_keep_role_team_and_lifecycle_scope(self):
        item=self.submission()
        for role in ['owner','host','floater','platform','team_leader','moderator']:
            actor=self.actor(role);self.client.force_login(actor)
            expected=403 if role=='moderator' else 200
            for name in ['submission-manage','submission-fix-book','submission-fix-pages']:
                self.assertEqual(self.client.get(self.route_url(name,item)).status_code,expected)
            if role=='team_leader':
                TeamAssignment.objects.filter(team=self.team,participant=self.reader).update(ended_at=timezone.now())
                self.assertEqual(self.client.get(self.route_url('submission-fix-pages',item)).status_code,403)
        # A permitted actor still cannot correct completed Challenges.
        self.client.force_login(get_user_model().objects.get(username='edit-owner'))
        self.month.__class__.objects.filter(pk=self.month.pk).update(status='finalizing')
        self.assertEqual(self.client.get(self.route_url('submission-fix-pages',item)).status_code,200)
        self.month.__class__.objects.filter(pk=self.month.pk).update(status='completed')
        self.assertEqual(self.client.get(self.route_url('submission-fix-pages',item)).status_code,403)

    def test_history_is_staff_scoped_and_sanitized(self):
        item=self.submission();admin=self.actor('platform');host=self.actor()
        payload={'association_mode':'only','before':{'submission':{'title':'Old title','approved_pages':350},
            'reader_submissions':[{'title':'UNRELATED_SECRET'}]},'after':{'submission':{'title':'New title','approved_pages':350}},'token':'TOKEN_SECRET'}
        AuditEvent.objects.create(actor=host,group=self.month.group,action='submission.corrected',object_type='BookSubmission',object_id=str(item.pk),summary=json.dumps(payload))
        AuditEvent.objects.create(actor=host,group=self.month.group,action='submission.approved',object_type='BookSubmission',object_id=str(item.pk),summary='Approved; approved pages: 350 PRIVATE_NOTES')
        for object_type,object_id in [('OtherType',str(item.pk)),('BookSubmission','999999')]:
            AuditEvent.objects.create(group=self.month.group,action='submission.corrected',object_type=object_type,object_id=object_id,summary='WRONG_EVENT')
        group=ReadingGroup.objects.create(name='Foreign',slug='foreign-history')
        AuditEvent.objects.create(group=group,action='submission.corrected',object_type='BookSubmission',object_id=str(item.pk),summary='FOREIGN_EVENT')
        url=self.route_url('submission-history',item)
        self.client.force_login(host);self.assertEqual(self.client.get(url).status_code,200)
        self.client.force_login(admin)
        response=self.client.get(url)
        for text in ['Changed Title.','Approved','Submission edited']:
            self.assertContains(response,text)
        for secret in ['UNRELATED_SECRET','TOKEN_SECRET','PRIVATE_NOTES','WRONG_EVENT','FOREIGN_EVENT']:
            self.assertNotContains(response,secret)
        self.assertEqual(self.client.get(reverse('submission-history',args=['wrong',self.month.pk,item.pk])).status_code,404)
        self.assertEqual(self.client.post(url,{}).status_code,405)
        self.month.__class__.objects.filter(pk=self.month.pk).update(status='completed')
        self.assertEqual(self.client.get(url).status_code,200)
        self.assertNotContains(self.client.get(self.route_url('submission-manage',item)),'Fix Pages / Themes')
        self.assertEqual(self.client.get(self.route_url('submission-fix-pages',item)).status_code,403)

    def test_compact_editor_combined_correction_previews_and_keeps_unchanged_fields(self):
        item = self.submission()
        actor = self.actor()
        self.client.force_login(actor)
        url = self.route_url('submission-editor', item)
        response = self.client.get(url)
        for label in ('Book Details', 'Pages &amp; Completion', 'Themes'):
            self.assertContains(response, label)
        self.assertNotContains(response, 'submission-editor-section" open')
        theme = MonthTheme.objects.create(
            month=self.month, name='Combined editor theme', starts_on=self.month.starts_on,
            ends_on=self.month.ends_on, bonus_percentage=25,
        )
        data = {
            'version': correction_version(item, actor), 'title': 'Corrected title', 'author': item.author,
            'submitted_pages': 400, 'completed_on': item.completed_on.isoformat(), 'themes': [theme.pk],
        }
        response = self.client.post(url, data)
        self.assertContains(response, 'Review Changes')
        self.assertContains(response, 'name="confirm_approved"')
        self.assertNotContains(response, 'name="confirm_work"')
        original = (item.author, item.completed_on, item.catalog_edition_id, item.approved_pages)
        item.refresh_from_db()
        self.assertEqual(item.approved_pages, original[3])
        data.update(preview_token=response.context['form']['preview_token'].value(), confirm_approved='on')
        self.assertEqual(self.client.post(url, data).status_code, 302)
        item.refresh_from_db()
        self.assertEqual((item.author, item.completed_on, item.catalog_edition_id), original[:3])
        self.assertEqual((item.title, item.approved_pages), ('Corrected title', 400))
        self.assertEqual(item.theme_claims.get().award_bonus_pages, 100)
        self.assertTrue(AuditEvent.objects.filter(action='submission.corrected', object_id=str(item.pk)).exists())

    def test_compact_editor_opens_invalid_section_and_keeps_entered_title(self):
        item = self.submission()
        actor = self.actor()
        self.client.force_login(actor)
        url = self.route_url('submission-editor', item)
        response = self.client.post(url, {
            'version': correction_version(item, actor), 'title': 'Retained input', 'author': item.author,
            'submitted_pages': 'bad', 'completed_on': item.completed_on.isoformat(),
        })
        self.assertTrue(response.context['pages_open'])
        self.assertFalse(response.context['book_open'])
        self.assertContains(response, 'value="Retained input"')
        self.assertIn('submitted_pages', response.context['form'].errors)
        self.client.force_login(self.reader.user)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_activity_records_creation_review_decisions_and_preserves_original_times(self):
        from .scoring import apply_submission_review
        from .submission_corrections import correct_submission

        self.assertEqual(self.client.post(self.url, fixture.SubmissionHotfixTests.payload(self)).status_code, 302)
        item = BookSubmission.objects.get()
        actor = self.actor()
        submitted_at = item.submitted_at
        events = AuditEvent.objects.filter(object_type="BookSubmission", object_id=str(item.pk)).order_by("pk")
        self.assertEqual(list(events.values_list("action", flat=True)), ["submission.created"])
        item.status = "approved"
        item.approved_pages = 384
        item = apply_submission_review(item, [], actor, timezone.now())
        reviewed_at = item.reviewed_at
        item = apply_submission_review(item, [], actor, timezone.now())
        self.assertEqual(item.reviewed_at, reviewed_at)
        item = correct_submission(
            submission=item, actor=actor, changes={"title": "Edited title"},
            version=correction_version(item, actor),
        )
        self.assertEqual(item.submitted_at, submitted_at)
        self.assertEqual(item.reviewed_at, reviewed_at)
        item.review_notes = "Private review text"
        item = apply_submission_review(item, [], actor, timezone.now())
        item.status = "rejected"
        item.approved_pages = None
        item = apply_submission_review(item, [], actor, timezone.now())
        self.assertEqual(item.submitted_at, submitted_at)
        self.assertEqual(list(events.values_list("action", flat=True)), [
            "submission.created", "submission.approved", "submission.corrected",
            "submission.review_changed", "submission.rejected",
        ])
        self.assertNotIn("Private review text", events.get(action="submission.review_changed").summary)

    def test_activity_scope_and_pagination_exclude_other_team_and_group_records(self):
        item = self.submission()
        leader = self.actor("team_leader")
        host = self.actor()
        floater = self.actor("floater")
        owner = self.actor("owner")
        moderator = self.actor("moderator")
        admin = self.actor("platform")
        outsider = get_user_model().objects.create_user("activity-outsider")
        feed = reverse("submission-activity", args=[self.month.group.slug, self.month.pk])
        detail = self.route_url("submission-history", item)
        event = AuditEvent.objects.create(
            group=self.month.group, actor=host, action="submission.created",
            object_type="BookSubmission", object_id=str(item.pk), summary="PRIVATE SUMMARY",
        )
        AuditEvent.objects.filter(pk=event.pk).update(created_at=datetime(2026, 11, 9, 19, 34, tzinfo=utc.utc))
        self.month.group.timezone = "America/New_York"
        self.month.group.save(update_fields=["timezone"])
        foreign = ReadingGroup.objects.create(name="Foreign", slug="activity-foreign")
        AuditEvent.objects.bulk_create([AuditEvent(
            group=foreign, action="submission.created", object_type="BookSubmission", object_id=str(item.pk),
        ) for _ in range(30)])
        for user in (leader, host, floater, admin):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(feed)
                self.assertEqual(response.context["activity_page"].paginator.count, 1)
                self.assertContains(response, "November 9, 2026 · 2:34 PM")
                self.assertNotContains(response, "PRIVATE SUMMARY")
                self.assertEqual(self.client.get(detail).status_code, 200)
        for user, detail_status in ((moderator, 403), (outsider, 403), (owner, 200), (self.reader.user, 200)):
            self.client.force_login(user)
            response = self.client.get(feed)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(self.client.get(detail).status_code, detail_status)
        member = host.reading_memberships.get(group=self.month.group)
        MonthEnrollment.objects.create(month=self.month, participant=member)
        self.client.force_login(host)
        self.assertEqual(self.client.get(feed).status_code, 403)
        self.assertEqual(self.client.get(detail).status_code, 403)
        TeamAssignment.objects.create(month=self.month, participant=member, team=self.team)
        self.assertEqual(self.client.get(detail).status_code, 200)
        TeamAssignment.objects.filter(month=self.month, participant=self.reader).update(ended_at=timezone.now())
        for user in (host, leader):
            self.client.force_login(user)
            self.assertEqual(self.client.get(detail).status_code, 403)
            self.assertEqual(self.client.get(feed).context["activity_page"].paginator.count, 0)
        self.client.force_login(admin)
        self.assertEqual(self.client.get(reverse("platform-submission-activity")).context["activity_page"].paginator.count, 1)

    def test_reader_activity_hides_staff_credit_and_unrecorded_review_time(self):
        item = self.submission()
        BookSubmission.objects.filter(pk=item.pk).update(reviewed_at=timezone.now())
        AuditEvent.objects.create(group=self.month.group, action="submission.credit_assign",
            object_type="BookSubmission", object_id=str(item.pk), summary="PRIVATE CREDIT REASON")
        self.client.force_login(self.reader.user)
        response = self.client.get(self.route_url("submission-history", item))
        self.assertContains(response, "Submitted")
        self.assertNotContains(response, "Reviewed")
        self.assertNotContains(response, "Challenge credit assigned")
        self.assertNotContains(response, "PRIVATE CREDIT REASON")

    def test_activity_pages_order_matching_and_submission_events_together(self):
        item = self.submission()
        host = self.actor()
        match = item.personal_tbr_matches.get()
        for _ in range(25):
            AuditEvent.objects.create(group=self.month.group, actor=host, action="submission.created",
                object_type="BookSubmission", object_id=str(item.pk))
        AuditEvent.objects.create(group=self.month.group, actor=host, action="personal_tbr.match_confirmed",
            object_type="PersonalTBRMatch", object_id=str(match.pk), summary="PRIVATE MATCH EVIDENCE")
        self.client.force_login(host)
        url = reverse("submission-activity", args=[self.month.group.slug, self.month.pk])
        first = self.client.get(url)
        self.assertEqual(first.context["activity_page"].paginator.count, 26)
        self.assertEqual(first.context["cards"][0]["label"], "TBR match confirmed")
        self.assertNotContains(first, "PRIVATE MATCH EVIDENCE")
        self.assertEqual([card["label"] for card in self.client.get(url, {"page": 2}).context["cards"]], ["Submitted"])
        self.client.force_login(self.reader.user)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_activity_navigation_is_limited_and_manage_history_is_inline(self):
        item = self.submission()
        host = self.actor()
        slug = self.month.group.slug
        feed = reverse("submission-activity", args=[slug, self.month.pk])
        overview = self.month.get_absolute_url()
        settings = reverse("challenge-settings", args=[slug, self.month.pk])
        queue = reverse("review-queue", args=[slug, self.month.pk])
        self.client.force_login(host)
        self.assertContains(self.client.get(overview), feed)
        self.assertContains(self.client.get(queue), feed)
        self.assertNotContains(self.client.get(settings), feed)
        response = self.client.get(self.route_url("submission-manage", item))
        self.assertContains(response, "<summary>View History</summary>", html=True)
        self.assertContains(response, "Submitted")
        self.assertNotContains(response, feed)
        self.client.force_login(self.reader.user)
        response = self.client.get(overview)
        self.assertNotContains(response, feed)
        self.assertContains(response, self.route_url("submission-history", item))
