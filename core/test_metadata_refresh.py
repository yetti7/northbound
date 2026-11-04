import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from . import test_submission_hotfix as fixture
from .integrations.hardcover import fetch_edition_metadata, HardcoverConnectionError
from .metadata_refresh import refresh_selection, refresh_version, can_refresh, cover_only, load_selection
from .models import (
    AuditEvent, PersonalTBRBook, BotmBook, BookSubmission, CatalogBook, CatalogEdition,
    Membership, MonthEnrollment, ChallengeStaffAssignment, PersonalTBRMatch, Team,
)
from .test_display_metadata import evidence_state
from .personal_tbr_matching import synchronize_submission
from .botm_matching import synchronize_submission as synchronize_botm
from .scoring import apply_submission_review


class MetadataRefreshTests(TransactionTestCase):
    setUp = fixture.SubmissionHotfixTests.setUp

    def prepare(self):
        # Real provider identities are numeric; replace only fixture catalog keys.
        CatalogBook.objects.filter(pk=self.book.pk).update(provider_book_id='101')
        CatalogEdition.objects.filter(pk=self.edition.pk).update(provider_edition_id='202')
        self.book.refresh_from_db(); self.edition.refresh_from_db()
        selection = self.tbr.books.get(position=1)
        PersonalTBRBook.objects.filter(pk=selection.pk).update(page_count_snapshot=384, display_page_count=384)
        return load_selection(PersonalTBRBook, selection.pk)

    def payload(self, **changes):
        value = {'id':202, 'book_id':101, 'canonical_id':None,
                 'book':{'id':101, 'canonical_id':None}, 'title':'Updated title',
                 'cached_contributors':[{'author':{'name':'Updated author'}}],
                 'pages':400, 'image':{'url':'https://example.com/new.jpg'}}
        value.update(changes)
        return {'editions_by_pk':value}

    def run_refresh(self, selection, actor=None, payload=None, callback=None, version=None):
        actor = actor or self.reader.user
        version = version or refresh_version(selection, actor)
        def fetch(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            if callback: callback()
            return payload if payload is not None else self.payload()
        with patch('core.integrations.hardcover.execute_graphql', side_effect=fetch) as provider:
            result = refresh_selection(selection=selection, actor=actor, token='credential', version=version)
        provider.assert_called_once()
        self.assertEqual(provider.call_args.args[2], {'id':202})
        return result

    def admin(self, role, group=None):
        user = get_user_model().objects.create_user('refresh-'+role, is_superuser=role=='platform')
        if role != 'platform':
            member = Membership.objects.create(group=group or self.month.group, user=user,
                display_name=role, role=role if role in ('owner','moderator') else 'member')
            if role in ('host','floater','team_leader'):
                kwargs = dict(month=self.month, membership=member, role=role)
                if role == 'team_leader':
                    from .models import TeamAssignment
                    kwargs['team'] = Team.objects.create(month=self.month, name='Leader team')
                    MonthEnrollment.objects.create(month=self.month,participant=member)
                    TeamAssignment.objects.create(month=self.month,team=kwargs['team'],participant=member)
                ChallengeStaffAssignment.objects.create(**kwargs)
        return user

    def submission(self, *, participant=None, status='pending', catalog=True, title=None):
        return BookSubmission.objects.create(month=self.month, participant=participant or self.reader,
            title=title or self.book.title, author=self.book.author, book_format='ebook',
            completed_on=self.month.starts_on, submitted_pages=384,
            approved_pages=384 if status=='approved' else None, status=status,
            catalog_book=self.book if catalog else None, catalog_edition=self.edition if catalog else None)

    def historical(self):
        # Display columns and the new audit are the only permitted differences.
        result = evidence_state()
        result.pop('core.AuditEvent')
        return result

    def test_full_refresh_exact_identity_cache_bypass_and_isolation(self):
        selection = self.prepare()
        before = self.historical()
        with CaptureQueriesContext(connection) as queries:
            result = self.run_refresh(selection)
        self.assertFalse(result['cover_only'])
        current = PersonalTBRBook.objects.get(pk=selection.pk)
        self.assertEqual((current.display_title,current.display_author,current.display_page_count),('Updated title','Updated author',400))
        self.assertEqual(self.historical(), before)
        self.assertEqual(AuditEvent.objects.filter(action='book.display_metadata_refreshed').count(),1)
        writes=[query['sql'] for query in queries if query['sql'].lstrip().upper().startswith(('UPDATE','INSERT','DELETE'))]
        self.assertTrue(all('core_personaltbrbook' in sql or 'core_auditevent' in sql for sql in writes))

    def test_bad_identities_and_redirects_never_write(self):
        selection = self.prepare()
        for change in ({'id':203}, {'book_id':999}, {'book':{'id':999,'canonical_id':None}},
                       {'canonical_id':203}, {'book':{'id':101,'canonical_id':999}},
                       {'id':True}, {'id':'0202'}, {'book':None}):
            before=self.historical()
            with self.assertRaises(HardcoverConnectionError):
                self.run_refresh(selection,payload=self.payload(**change))
            self.assertEqual(self.historical(),before)
        self.assertFalse(AuditEvent.objects.filter(action='book.display_metadata_refreshed').exists())

    def test_missing_invalid_fields_and_audio_pages_preserve_good_values(self):
        selection=self.prepare()
        for pages in (None,0,-1,True,'123',1.5,2147483648):
            self.run_refresh(selection,payload=self.payload(title=None,cached_contributors=[{'author':'bad'},123],
                image={'url':'javascript:bad'},pages=pages))
            current=PersonalTBRBook.objects.get(pk=selection.pk)
            self.assertEqual(current.display_title,selection.display_title)
            self.assertEqual(current.display_page_count,384)
            self.assertEqual(current.display_cover_url,selection.display_cover_url)
            selection=load_selection(PersonalTBRBook,selection.pk)
        # No alternative-edition lookup is made, even for an audio edition.
        self.assertEqual(AuditEvent.objects.filter(action='book.display_metadata_refreshed').count(),7)

    def test_pending_without_matches_and_ambiguous_candidates_are_cover_only(self):
        selection=self.prepare()
        self.submission()
        self.assertFalse(PersonalTBRMatch.objects.exists())
        before=self.historical()
        result=self.run_refresh(selection)
        self.assertTrue(result['cover_only'])
        current=PersonalTBRBook.objects.get(pk=selection.pk)
        self.assertEqual(current.display_title,selection.display_title)
        self.assertEqual(current.display_page_count,384)
        self.assertEqual(current.display_cover_url,'https://example.com/new.jpg')
        self.assertEqual(self.historical(),before)

    def test_ambiguous_manual_pending_restricts_all_botm_candidates(self):
        self.prepare()
        work=CatalogBook.objects.create(provider='hardcover',provider_book_id='102',title=self.book.title,author=self.book.author)
        edition=CatalogEdition.objects.create(provider='hardcover',provider_edition_id='203',book=work,page_count=200)
        books=[BotmBook.objects.create(month=self.month,position=position,catalog_book=catalog,catalog_edition=selected,
            title_snapshot=self.book.title,author_snapshot=self.book.author,page_count_snapshot=200)
            for position,(catalog,selected) in enumerate(((self.book,self.edition),(work,edition)),1)]
        self.submission(catalog=False)
        for book in books:self.assertTrue(cover_only(book))

    def test_completed_and_approval_during_fetch_allow_only_artwork(self):
        selection=self.prepare()
        pending=self.submission()
        def approve():
            pending.status='approved';pending.approved_pages=384
            apply_submission_review(pending,[],self.reader.user,timezone.now())
        result=self.run_refresh(selection,callback=approve)
        self.assertTrue(result['cover_only'])
        current=load_selection(PersonalTBRBook,selection.pk)
        self.assertEqual(current.display_page_count,384)
        before=self.historical()
        self.run_refresh(current,payload=self.payload(image={'url':'https://example.com/again.jpg'}))
        self.assertEqual(self.historical(),before)

    def test_botm_restrictions_include_other_readers(self):
        selection=self.prepare()
        self.month.botm_enabled=True;self.month.save()
        botm=BotmBook.objects.create(month=self.month,position=1,catalog_book=self.book,catalog_edition=self.edition,
            title_snapshot=self.book.title,author_snapshot=self.book.author,page_count_snapshot=384)
        user=get_user_model().objects.create_user('other-reader')
        other=Membership.objects.create(group=self.month.group,user=user,display_name='Other')
        MonthEnrollment.objects.create(month=self.month,participant=other)
        pending=self.submission(participant=other)
        self.assertFalse(cover_only(selection))
        self.assertTrue(cover_only(load_selection(BotmBook,botm.pk)))
        actor=self.admin('host')
        self.assertTrue(self.run_refresh(botm,actor=actor)['cover_only'])
        pending.status='approved';pending.approved_pages=384;pending.save();synchronize_botm(pending)
        current=load_selection(BotmBook,botm.pk)
        self.assertTrue(cover_only(current))
        before=self.historical()
        self.assertTrue(self.run_refresh(current,actor=actor)['cover_only'])
        self.assertEqual(self.historical(),before)

    def test_permissions_manual_entries_lifecycle_and_server_enforcement(self):
        selection=self.prepare()
        for role in ('owner','host','platform'):
            self.assertTrue(can_refresh(self.admin(role),selection))
        for role in ('moderator','floater','team_leader','reader'):
            actor=self.admin(role)
            self.assertFalse(can_refresh(actor,selection))
            self.client.force_login(actor)
            response=self.client.post(reverse('tbr-metadata-refresh',args=['hotfix',self.month.pk,selection.pk]))
            self.assertEqual(response.status_code,403)
        for status in ('finalizing','completed','archived'):
            self.month.__class__.objects.filter(pk=self.month.pk).update(status=status)
            current=load_selection(PersonalTBRBook,selection.pk)
            self.assertFalse(can_refresh(self.reader.user,current))
        self.month.__class__.objects.filter(pk=self.month.pk).update(status='active')
        manual=self.tbr.books.get(position=3)
        with self.assertRaises(ValidationError):refresh_version(manual,self.reader.user)
        self.reader.user.is_active=False
        self.assertFalse(can_refresh(self.reader.user,selection))

    def test_stale_competing_refresh_and_atomic_audit_failure(self):
        selection=self.prepare()
        version=refresh_version(selection,self.reader.user)
        def competing():
            self.run_refresh(selection,payload=self.payload(title='Winner'))
        with self.assertRaisesMessage(ValidationError,'changed during refresh'):
            self.run_refresh(selection,version=version,callback=competing)
        current=load_selection(PersonalTBRBook,selection.pk)
        self.assertEqual(current.display_title,'Winner')
        with self.assertRaises(ValidationError):self.run_refresh(current,version=version)
        before=self.historical();stamp=current.display_refreshed_at
        with patch('core.metadata_refresh.AuditEvent.objects.create',side_effect=RuntimeError('Audit failed')):
            with self.assertRaises(RuntimeError):self.run_refresh(current,payload=self.payload(title='Must roll back'))
        self.assertEqual(self.historical(),before)
        current.refresh_from_db()
        self.assertEqual(current.display_title,'Winner')
        self.assertEqual(current.display_refreshed_at,stamp)

    def test_ui_post_success_hide_manual_and_preserve_controls(self):
        selection=self.prepare()
        self.client.force_login(self.reader.user)
        page=self.client.get(reverse('personal-tbr-detail',args=['hotfix',self.month.pk]))
        self.assertContains(page,'Refresh Metadata',count=1)
        self.assertContains(page,'data-book-cover')
        url=reverse('tbr-metadata-refresh',args=['hotfix',self.month.pk,selection.pk])
        self.assertEqual(self.client.get(url).status_code,405)
        with patch('core.views.get_reader_hardcover_token',return_value='credential'),patch('core.integrations.hardcover.execute_graphql',return_value=self.payload()):
            response=self.client.post(url,{'version':refresh_version(selection,self.reader.user)},follow=True)
        self.assertContains(response,'Display metadata refreshed.')
        self.assertContains(response,'Updated title')
        self.assertEqual(self.client.post(reverse('tbr-metadata-refresh',args=['missing-group',self.month.pk,selection.pk])).status_code,404)

    def test_concurrent_refreshes_only_one_update_and_audit(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections, OperationalError
        selection=self.prepare(); actor=self.reader.user
        version=refresh_version(selection,actor)
        barrier=Barrier(2)
        def provider(*args):
            self.assertFalse(connection.in_atomic_block)
            barrier.wait(timeout=10)
            return self.payload()
        def attempt(_):
            close_old_connections()
            try:
                refresh_selection(selection=selection,actor=actor,token='credential',version=version)
                return 'saved'
            except (ValidationError,OperationalError):return 'rejected'
            finally:close_old_connections()
        with patch('core.integrations.hardcover.execute_graphql',side_effect=provider):
            with ThreadPoolExecutor(max_workers=2) as pool:
                result=list(pool.map(attempt,range(2)))
        self.assertEqual(sorted(result),['rejected','saved'])
        self.assertEqual(AuditEvent.objects.filter(action='book.display_metadata_refreshed').count(),1)

    def test_identity_and_authority_rechecked_after_fetch(self):
        selection=self.prepare()
        # A changed selection must never be silently rebound to the fetched edition.
        def change_identity():
            CatalogEdition.objects.filter(pk=self.edition.pk).update(provider_edition_id='203')
        with self.assertRaises(ValidationError):self.run_refresh(selection,callback=change_identity)
        CatalogEdition.objects.filter(pk=self.edition.pk).update(provider_edition_id='202')
        def revoke():
            Membership.objects.filter(pk=self.reader.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):self.run_refresh(selection,callback=revoke)
        self.assertFalse(AuditEvent.objects.filter(action='book.display_metadata_refreshed').exists())

    def test_manual_post_does_not_resolve_credentials_and_forms_hide_after_completion(self):
        selection=self.prepare()
        self.client.force_login(self.reader.user)
        manual=self.tbr.books.get(position=3)
        with patch('core.views.get_reader_hardcover_token') as credentials:
            response=self.client.post(reverse('tbr-metadata-refresh',args=['hotfix',self.month.pk,manual.pk]))
        self.assertEqual(response.status_code,302)
        credentials.assert_not_called()
        approved=self.submission(status='approved');synchronize_submission(approved)
        response=self.client.get(reverse('personal-tbr-detail',args=['hotfix',self.month.pk]))
        self.assertContains(response,'Cover artwork only while completed or awaiting review.')
        self.assertContains(response,'book-completion-overlay')
        # An outsider Group Owner has no authority in this Group.
        from .models import ReadingGroup
        foreign=self.admin('owner',group=ReadingGroup.objects.create(name='Foreign',slug='foreign'))
        self.assertFalse(can_refresh(foreign,load_selection(PersonalTBRBook,selection.pk)))

    def test_refresh_preserves_completed_awards_themes_standings_and_checkpoints(self):
        from .models import MonthTheme, ThemeClaim, ProgressCheckpoint, TeamAssignment
        from .scoring import refresh_submission_score
        from .score_aggregation import challenge_score_totals
        from .progress_checkpoints import process_due_progress_checkpoints
        selection=self.prepare()
        self.month.botm_enabled=True;self.month.botm_completion_bonus_pages=100;self.month.save()
        submissions=[]
        for book in self.tbr.books.order_by('position'):
            item=self.submission(status='approved',catalog=book.catalog_book_id is not None,title=book.title_snapshot)
            synchronize_submission(item);submissions.append(item)
        BotmBook.objects.create(month=self.month,position=1,catalog_book=self.book,catalog_edition=self.edition,
            title_snapshot=self.book.title,author_snapshot=self.book.author,page_count_snapshot=384,bonus_pages=50)
        synchronize_botm(submissions[0])
        theme=MonthTheme.objects.create(month=self.month,name='Theme',starts_on=self.month.starts_on,
            ends_on=self.month.ends_on,bonus_rule='legacy_fixed',bonus_pages=20)
        ThemeClaim.objects.create(submission=submissions[0],theme=theme,status='approved',approved_bonus_pages=20)
        refresh_submission_score(submissions[0])
        team=Team.objects.create(month=self.month,name='Historical team')
        TeamAssignment.objects.create(month=self.month,team=team,participant=self.reader)
        ProgressCheckpoint.objects.create(month=self.month,scheduled_at=timezone.now(),
            target_basis='fixed',fixed_target_pages=100,progress_basis='total')
        process_due_progress_checkpoints(now=timezone.now())
        from .models import PersonalTBRCompletionAward, BotmCompletionAward
        self.assertTrue(PersonalTBRCompletionAward.objects.exists())
        self.assertTrue(BotmCompletionAward.objects.exists())
        before=self.historical();totals=challenge_score_totals(month=self.month);team_total=team.approved_pages
        self.assertTrue(self.run_refresh(selection)['cover_only'])
        self.assertEqual(self.historical(),before)
        self.assertEqual(challenge_score_totals(month=self.month),totals)
        self.assertEqual(team.approved_pages,team_total)

    def test_repeated_valid_refresh_is_harmless_and_audited(self):
        selection=self.prepare()
        first=self.run_refresh(selection)
        self.assertTrue(first['changed_fields'])
        current=load_selection(PersonalTBRBook,selection.pk)
        before=self.historical()
        second=self.run_refresh(current)
        self.assertEqual(second['changed_fields'],[])
        self.assertEqual(self.historical(),before)
        self.assertEqual(AuditEvent.objects.filter(action='book.display_metadata_refreshed').count(),2)

    def test_removed_selection_during_fetch_returns_stale_error(self):
        selection=self.prepare()
        # Simulate an existing authorized configuration operation removing a row.
        # QuerySet deletion is used only against this isolated fixture.
        def remove():
            PersonalTBRBook.objects.filter(pk=selection.pk).delete()
        with self.assertRaisesMessage(ValidationError,'removed'):
            self.run_refresh(selection,callback=remove)
        self.assertFalse(AuditEvent.objects.filter(action='book.display_metadata_refreshed').exists())
