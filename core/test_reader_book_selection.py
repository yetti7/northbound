from datetime import date
from django.core import signing
from django.test import TestCase
from django.urls import reverse
from . import test_submission_hotfix as fixture
from .models import BookSubmission, BotmBook, BotmMatch, PersonalTBRMatch, PersonalTBRBook, CatalogEdition, CatalogBook
from .matching_intent import selection_token


class ReaderBookSelectionTests(TestCase):
    payload = fixture.SubmissionHotfixTests.payload
    token = fixture.SubmissionHotfixTests.token

    def setUp(self):
        fixture.SubmissionHotfixTests.setUp(self)
        self.month.botm_enabled=True;self.month.save()
        self.botm=BotmBook.objects.create(month=self.month,position=1,title_snapshot=self.book.title,
            author_snapshot=self.book.author,catalog_book=self.book,catalog_edition=self.edition,page_count_snapshot=384,bonus_pages=250)
        self.tbr_book=self.tbr.books.first()
        self.catalog_url=reverse('submission-catalog',args=[self.month.group.slug,self.month.pk])

    def intent(self,botm=True,tbr=False):
        data={}
        for domain,book,enabled in [('botm',self.botm,botm),('tbr',self.tbr_book,tbr)]:
            if enabled:data.update({f'selected_{domain}':book.pk,f'{domain}_token':selection_token(book,self.reader.user)})
        return data

    def test_picker_uses_actual_confirmed_reader_books_and_shared_cover(self):
        response=self.client.get(self.url)
        self.assertContains(response,'Your Challenge Books')
        self.assertContains(response,'My Personal TBR')
        self.assertContains(response,'data-book-cover')
        self.assertContains(response,'aria-pressed="false"')
        self.assertEqual(len(response.context['challenge_book_groups'][1]['books']),9)
        self.tbr.books.exclude(pk=self.tbr_book.pk).delete()
        response=self.client.get(self.url)
        self.assertEqual(len(response.context['challenge_book_groups'][1]['books']),1)
        self.assertNotContains(response,'verified pages')
        type(self.tbr).objects.filter(pk=self.tbr.pk).update(confirmed_at=None)
        self.assertNotContains(self.client.get(self.url),'My Personal TBR')

    def test_manual_botm_tbr_both_and_no_selection(self):
        for botm,tbr in [(True,False),(False,True),(True,True),(False,False)]:
            with self.subTest(botm=botm,tbr=tbr):
                response=self.client.post(self.url,self.payload(**self.intent(botm,tbr)))
                self.assertEqual(response.status_code,302)
                s=BookSubmission.objects.latest('pk')
                self.assertEqual(s.selected_botm_book_id,self.botm.pk if botm else None)
                self.assertEqual(s.selected_tbr_book_id,self.tbr_book.pk if tbr else None)
                self.assertEqual(s.status,'pending')
                self.assertFalse(BotmMatch.objects.filter(submission=s,is_qualifying=True).exists())
                self.assertFalse(PersonalTBRMatch.objects.filter(submission=s,is_qualifying=True).exists())

    def test_hardcover_alternate_edition_keeps_both_intents_and_scores(self):
        audio=CatalogEdition.objects.create(book=self.book,provider='hardcover',provider_edition_id='audio',format_name='Audiobook',audio_seconds=50000)
        token=signing.dumps({'selected':audio.pk,'scoring':self.edition.pk,'method':'hardcover_audio'},salt='northbound.catalog-selection')
        response=self.client.post(self.url,self.payload(catalog_selection=token,**self.intent(True,True)))
        self.assertEqual(response.status_code,302)
        s=BookSubmission.objects.get();self.assertEqual(s.catalog_edition_id,audio.pk)
        self.assertEqual(s.approved_pages,384);self.assertEqual(s.selected_botm_book_id,self.botm.pk)
        self.assertEqual(s.selected_tbr_book_id,self.tbr_book.pk)
        self.assertTrue(BotmMatch.objects.get(submission=s).is_qualifying)
        self.assertTrue(PersonalTBRMatch.objects.get(submission=s).is_qualifying)

    def test_form_errors_preserve_valid_selections_and_do_not_write(self):
        data=self.payload(completed_on='2026-12-01',**self.intent(True,True))
        response=self.client.post(self.url,data)
        self.assertEqual(response.status_code,200)
        self.assertEqual(str(response.context['intent_form']['selected_botm'].value()),str(self.botm.pk))
        self.assertEqual(str(response.context['intent_form']['selected_tbr'].value()),str(self.tbr_book.pk))
        self.assertFalse(BookSubmission.objects.exists())

    def test_cross_reader_cross_challenge_retired_and_expired_are_rejected(self):
        from unittest.mock import patch
        from .models import ChallengeMonth, Membership, MonthEnrollment, PersonalTBR
        from django.contrib.auth import get_user_model
        other_month=ChallengeMonth.objects.create(group=self.month.group,name='Other',status='active',starts_on=date(2026,11,1),ends_on=date(2026,11,30))
        foreign_botm=BotmBook.objects.create(month=other_month,position=1,title_snapshot='Foreign',author_snapshot='Author',page_count_snapshot=100)
        other=Membership.objects.create(group=self.month.group,user=get_user_model().objects.create_user('other'),display_name='Other')
        foreign_tbr=PersonalTBR.objects.create(enrollment=MonthEnrollment.objects.create(month=self.month,participant=other))
        foreign_book=PersonalTBRBook.objects.create(personal_tbr=foreign_tbr,position=1,title_snapshot='Private other book',author_snapshot='Author')
        foreign_tbr.confirmed_at=self.tbr.confirmed_at;foreign_tbr.save()
        for domain,book in [('botm',foreign_botm),('tbr',foreign_book)]:
            data={f'selected_{domain}':book.pk,f'{domain}_token':selection_token(book,self.reader.user)}
            response=self.client.post(self.url,self.payload(**data))
            self.assertEqual(response.status_code,200);self.assertTrue(response.context['intent_form'].errors)
        self.assertNotContains(self.client.get(self.url),'Private other book')
        data=self.intent();self.botm.is_retired=True;self.botm.save()
        self.assertTrue(self.client.post(self.url,self.payload(**data)).context['intent_form'].errors)
        self.botm.is_retired=False;self.botm.save()
        with patch('django.core.signing.time.time',return_value=1):expired=self.intent()
        self.assertTrue(self.client.post(self.url,self.payload(**expired)).context['intent_form'].errors)
        stale=self.intent();self.botm.title_snapshot='Changed selection';self.botm.save()
        self.assertTrue(self.client.post(self.url,self.payload(**stale)).context['intent_form'].errors)
        self.assertFalse(BookSubmission.objects.exists())

    def test_search_and_edition_requests_never_change_intent_and_preview_is_read_only(self):
        data=self.intent(True,True)
        response=self.client.post(self.catalog_url,dict(action='intent-preview',title=self.book.title,author=self.book.author,**data))
        self.assertEqual(response.status_code,200)
        self.assertEqual(len(response.json()['feedback']),2)
        other=CatalogBook.objects.create(provider='hardcover',provider_book_id='different',title='Different',author='Other')
        edition=CatalogEdition.objects.create(book=other,provider='hardcover',provider_edition_id='different',page_count=100)
        token=signing.dumps({'selected':edition.pk,'scoring':edition.pk,'method':'hardcover'},salt='northbound.catalog-selection')
        response=self.client.post(self.catalog_url,dict(action='intent-preview',catalog_selection=token,**data))
        self.assertContains(response,'appears different')
        self.assertFalse(BookSubmission.objects.exists());self.assertFalse(BotmMatch.objects.exists());self.assertFalse(PersonalTBRMatch.objects.exists())
        response=self.client.post(self.url,self.payload(catalog_selection=self.token(),**data))
        self.assertEqual(response.status_code,302)
        self.assertEqual(BookSubmission.objects.get().selected_tbr_book_id,self.tbr_book.pk)
