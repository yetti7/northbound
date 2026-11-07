from django.test import TestCase
from django.urls import reverse
from . import test_matching_phase2a as fixture
from .models import (BookSubmission, BotmMatch, PersonalTBRMatch, CatalogEdition, CatalogBook,
                     AuditEvent, ModifierProvenance, HardcoverSyncOutbox, Membership, TeamAssignment)
from .botm_matching import synchronize_submission as sync_botm
from .personal_tbr_matching import synchronize_submission as sync_tbr


class UnifiedSubmissionReviewTests(TestCase):
    setUp=fixture.MatchingPhase2ATests.setUp
    actor=fixture.MatchingPhase2ATests.actor
    submission=fixture.MatchingPhase2ATests.submission
    botm=fixture.MatchingPhase2ATests.botm

    def review_url(self,s):
        return reverse('submission-review',args=[self.month.group.slug,self.month.pk,s.pk])

    def payload(self,s,**changes):
        response=self.client.get(self.review_url(s));self.assertEqual(response.status_code,200)
        result={'status':'approved','approved_pages':s.submitted_pages,'verification_url':'','review_notes':'Reviewed',
                'matching_version':response.context['matching_form']['matching_version'].value()}
        result.update(changes);return result

    def login(self,role='floater'):
        self.reviewer=self.actor(role);self.client.force_login(self.reviewer)

    def test_ordinary_approval_no_matches_and_repeat_is_rejected(self):
        self.login();s=self.submission(status='pending',title='An unrelated book',author='Unrelated author')
        data=self.payload(s);self.assertEqual(self.client.post(self.review_url(s),data).status_code,302)
        before=BookSubmission.objects.filter(pk=s.pk).values().get()
        response=self.client.post(self.review_url(s),data)
        self.assertContains(response,'Nothing was saved')
        self.assertEqual(BookSubmission.objects.filter(pk=s.pk).values().get(),before)
        self.assertEqual(AuditEvent.objects.filter(action='submission.approved').count(),1)

    def test_automatic_both_domains_alternate_formats_and_translation(self):
        self.login();target=self.botm(title=self.book.title,author=self.book.author,catalog_book=self.book)
        for label,title in [('Paperback',self.book.title),('Audiobook',self.book.title),('German ebook','Nicht verliebt')]:
            with self.subTest(format=label):
                s=self.submission(status='pending',title=title,author='Translated contributor',catalog=self.book,pages=384)
                s.catalog_edition=CatalogEdition.objects.create(book=self.book,provider='hardcover',provider_edition_id=label,format_name=label,page_count=384)
                s.save()
                response=self.client.get(self.review_url(s))
                self.assertContains(response,'Verified match',count=2)
                self.assertNotContains(response,'name="confirm_botm"')
                self.assertEqual(self.client.post(self.review_url(s),self.payload(s)).status_code,302)
                self.assertTrue(BotmMatch.objects.get(submission=s).is_qualifying)
                self.assertTrue(PersonalTBRMatch.objects.get(submission=s).is_qualifying)
                s.refresh_from_db();self.assertEqual(s.approved_pages,384)
                s.is_removed=True;s.save();sync_botm(s);sync_tbr(s)

    def test_each_suggestion_and_both_confirmed_during_floater_approval(self):
        self.login();self.botm()
        tbr=self.tbr.books.first()
        # Use the preexisting canonical TBR with a conservative subtitle variant.
        for domains,title,author in [(['botm'],'Dreadful Girls Halloween 99 Extended Edition','Rosalia Kings'),
                                     (['tbr'],self.book.title+' Extended Edition',self.book.author)]:
            with self.subTest(domains=domains):
                s=self.submission(status='pending',title=title,author=author)
                sync_botm(s);sync_tbr(s)
                data=self.payload(s,**{'confirm_'+d:'on' for d in domains})
                self.assertEqual(self.client.post(self.review_url(s),data).status_code,302)
                model=BotmMatch if domains==['botm'] else PersonalTBRMatch
                match=model.objects.get(submission=s)
                self.assertTrue(match.is_qualifying);self.assertEqual(match.reviewer_id,self.reviewer.pk)
                self.assertTrue(match.decision_evidence)
                s.refresh_from_db();self.assertEqual(s.approved_pages,158)
                sync_botm(s);sync_tbr(s)
                match.refresh_from_db();self.assertEqual(match.method,'manual_review')

    def test_both_suggestions_and_approve_book_only(self):
        self.login();self.botm(title=self.book.title,author=self.book.author)
        s=self.submission(status='pending',title=self.book.title+' Extended Edition',author=self.book.author)
        # GET stays read-only, including when legacy submissions lack candidate rows.
        before=AuditEvent.objects.count();response=self.client.get(self.review_url(s))
        self.assertContains(response,'name="confirm_botm"');self.assertContains(response,'name="confirm_tbr"')
        self.assertFalse(BotmMatch.objects.exists());self.assertFalse(PersonalTBRMatch.objects.exists())
        self.assertEqual(AuditEvent.objects.count(),before)
        data=self.payload(s)
        self.assertEqual(self.client.post(self.review_url(s),data).status_code,302)
        self.assertFalse(BotmMatch.objects.get(submission=s).is_qualifying)
        self.assertFalse(PersonalTBRMatch.objects.get(submission=s).is_qualifying)
        s.refresh_from_db()
        data=self.payload(s,confirm_botm='on',confirm_tbr='on')
        self.assertEqual(self.client.post(self.review_url(s),data).status_code,302)
        self.assertTrue(BotmMatch.objects.get(submission=s).is_qualifying)
        self.assertTrue(PersonalTBRMatch.objects.get(submission=s).is_qualifying)
        self.assertFalse(HardcoverSyncOutbox.objects.exists())

    def test_stale_changed_withdrawn_and_expired_roll_back(self):
        from unittest.mock import patch
        self.login();self.botm();s=self.submission(status='pending')
        sync_botm(s);data=self.payload(s,confirm_botm='on')
        BookSubmission.objects.filter(pk=s.pk).update(title='Changed by Reader')
        self.assertContains(self.client.post(self.review_url(s),data),'Nothing was saved')
        s.refresh_from_db();self.assertEqual(s.status,'pending')
        self.assertFalse(ModifierProvenance.objects.filter(submission=s).exists())
        data=self.payload(s);BookSubmission.objects.filter(pk=s.pk).update(is_removed=True)
        self.assertContains(self.client.post(self.review_url(s),data),'Nothing was saved')
        BookSubmission.objects.filter(pk=s.pk).update(is_removed=False)
        with patch('django.core.signing.time.time',return_value=1):data=self.payload(s)
        self.assertContains(self.client.post(self.review_url(s),data),'Nothing was saved')

    def test_conflict_and_duplicate_block_confirmation_but_not_book_approval(self):
        self.login();self.botm(catalog_book=self.book)
        wrong=CatalogBook.objects.create(provider='hardcover',provider_book_id='wrong',title='Dreadful Girls',author='Rosalia Kings')
        s=self.submission(status='pending',catalog=wrong)
        response=self.client.get(self.review_url(s));self.assertContains(response,'Conflict')
        self.assertNotContains(response,'name="confirm_botm"')
        response=self.client.post(self.review_url(s),self.payload(s,confirm_botm='on'))
        self.assertContains(response,'cannot be confirmed here')
        self.assertEqual(self.client.post(self.review_url(s),self.payload(s)).status_code,302)
        self.assertFalse(BotmMatch.objects.get(submission=s).is_qualifying)
        # A qualification already held by another submission cannot be stolen.
        first=self.submission(title=self.book.title,author=self.book.author,catalog=self.book);sync_tbr(first)
        second=self.submission(status='pending',title=self.book.title,author=self.book.author,catalog=self.book)
        response=self.client.get(self.review_url(second));self.assertContains(response,'Conflict')
        self.assertEqual(self.client.post(self.review_url(second),self.payload(second)).status_code,302)
        self.assertTrue(PersonalTBRMatch.objects.get(submission=first).is_qualifying)
        self.assertFalse(PersonalTBRMatch.objects.get(submission=second).is_qualifying)

    def test_translation_intent_without_verified_work_can_be_confirmed(self):
        from .matching_intent import capture_intent,selection_token
        self.login();target=self.botm(title=self.book.title,author=self.book.author)
        s=self.submission(status='pending',title='Nicht verliebt',author='Translator',save=False)
        capture_intent(s,actor=self.reader.user,botm_id=target.pk,tokens={'botm':selection_token(target,self.reader.user)})
        s.save()
        response=self.client.get(self.review_url(s));self.assertContains(response,'Suggested match')
        self.assertEqual(self.client.post(self.review_url(s),self.payload(s,confirm_botm='on')).status_code,302)
        self.assertTrue(BotmMatch.objects.get(submission=s).is_qualifying)

    def test_team_scope_cross_group_and_no_advanced_authority(self):
        self.login('team_leader');self.botm();s=self.submission(status='pending')
        self.assertEqual(self.client.post(self.review_url(s),self.payload(s,confirm_botm='on')).status_code,302)
        TeamAssignment.objects.filter(participant=self.reader).delete()
        self.assertEqual(self.client.get(self.review_url(s)).status_code,403)
        self.client.force_login(self.reader.user)
        self.assertEqual(self.client.get(self.review_url(s)).status_code,403)
        from django.contrib.auth import get_user_model
        outsider=get_user_model().objects.create_user('outsider');self.client.force_login(outsider)
        self.assertEqual(self.client.get(self.review_url(s)).status_code,403)

    def test_confirmation_failure_rolls_back_book_candidates_awards_and_audits(self):
        from unittest.mock import patch
        from django.core.exceptions import ValidationError
        self.login();self.botm();s=self.submission(status='pending')
        data=self.payload(s,confirm_botm='on')
        before=BookSubmission.objects.filter(pk=s.pk).values().get()
        with patch('core.matching_resolution.confirm_suggestion',side_effect=ValidationError('Internal failure')):
            response=self.client.post(self.review_url(s),data)
        self.assertContains(response,'Nothing was saved')
        self.assertNotContains(response,'Internal failure')
        self.assertEqual(BookSubmission.objects.filter(pk=s.pk).values().get(),before)
        self.assertFalse(BotmMatch.objects.exists())
        self.assertFalse(ModifierProvenance.objects.exists())
        self.assertFalse(AuditEvent.objects.filter(action='submission.approved').exists())

    def test_queue_compact_indicators_and_invalid_choices_are_preserved(self):
        self.login();self.botm();s=self.submission(status='pending')
        response=self.client.get(reverse('review-queue',args=[self.month.group.slug,self.month.pk]))
        self.assertContains(response,'needs confirmation')
        data=self.payload(s,confirm_botm='on',approved_pages='invalid')
        response=self.client.post(self.review_url(s),data)
        self.assertTrue(response.context['matching_form']['confirm_botm'].value())
        self.assertEqual(BookSubmission.objects.get(pk=s.pk).status,'pending')
