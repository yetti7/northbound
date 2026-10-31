from unittest.mock import patch

from django.core import signing
from django.test import TestCase
from django.urls import reverse

from .forms import PersonalTbrRegistrationBookForm, PersonalTbrRegistrationBookFormSet, StaffChallengeTbrBookForm
from .integrations.hardcover import _normalize_search_hit, lookup_edition
from .models import CatalogBook, CatalogEdition, PersonalTBR
from .personal_tbr import replace_draft_personal_tbr
from . import test_staff_challenge_tbr as staff_fixture
from .views import _prepare_personal_tbr_edition


class TbrCoverPreviewTests(TestCase):
    setUp = staff_fixture.StaffChallengeTbrTests.setUp
    books = staff_fixture.StaffChallengeTbrTests.books
    save = staff_fixture.StaffChallengeTbrTests.save
    payload = staff_fixture.StaffChallengeTbrTests.payload

    def catalog(self):
        book = CatalogBook.objects.create(provider="hardcover", provider_book_id="123", title="Book", author="Author", cover_url="https://example.com/cache.jpg")
        selected = CatalogEdition.objects.create(book=book, provider="hardcover", provider_edition_id="456", page_count=200)
        scoring = CatalogEdition.objects.create(book=book, provider="hardcover", provider_edition_id="789", page_count=300)
        return book, selected, scoring

    def test_search_result_includes_cover_or_empty_url(self):
        self.assertEqual(_normalize_search_hit({"document":{"image":{"url":"https://example.com/search.jpg"}}})["cover_url"], "https://example.com/search.jpg")
        self.assertEqual(_normalize_search_hit({"document":{}})["cover_url"], "")
        with patch("core.views.get_reader_hardcover_token", return_value="staff"), patch("core.views.search_books", return_value=([{"title":"Found", "cover_url":"https://example.com/search.jpg"}],False)):
            response = self.client.post(self.catalog_url,{"action":"search", "query":"Found"})
        self.assertEqual(response.json()["results"][0]["cover_url"], "https://example.com/search.jpg")

    def test_selected_edition_art_is_signed_before_scoring_lookup_and_survives_cache_change(self):
        catalog, selected, scoring = self.catalog()
        def metadata(token, query, variables):
            self.assertEqual(variables, {"id":456})
            return {"editions_by_pk":{"id":456,"book_id":123,"title":"Selected audio", "audio_seconds":1000,"edition_format":"Audiobook", "image":{"url":"https://example.com/selected.jpg"}, "cached_contributors":["Author"], "book":{"id":123,"slug":"book"}}}
        def resolve(token, result):
            CatalogBook.objects.filter(pk=catalog.pk).update(cover_url="https://example.com/scoring.jpg")
            return {"edition_id":"789","pages":300}, "hardcover_audio"
        with patch("core.integrations.hardcover.execute_graphql", side_effect=metadata) as graphql, patch("core.views.resolve_scoring_edition",side_effect=resolve):
            # Selected edition is already cached with the wrong work-level art.
            result = _prepare_personal_tbr_edition("actor", {"edition_id":"456"})["result"]
        graphql.assert_called_once()
        self.assertEqual(result["cover_url"], "https://example.com/selected.jpg")
        signed = signing.loads(result["catalog_selection"],salt="northbound.personal-tbr-selection")
        self.assertEqual(signed["cover_url"],result["cover_url"])
        form = PersonalTbrRegistrationBookForm({"catalog_selection":result["catalog_selection"], "cover_url_snapshot":"https://example.com/untrusted.jpg"})
        self.assertTrue(form.is_valid(),form.errors)
        self.assertEqual(form.cleaned_data["cover_url_snapshot"],"https://example.com/selected.jpg")
        self.assertEqual(form.cleaned_data["page_count_snapshot"],300)
        formset = PersonalTbrRegistrationBookFormSet({"form-TOTAL_FORMS":"1","form-INITIAL_FORMS":"0","form-0-catalog_selection":result["catalog_selection"]})
        self.assertTrue(formset.is_valid(),formset.errors)
        saved = replace_draft_personal_tbr(enrollment=self.enrollment,books=formset.book_values())
        self.assertEqual(saved.books.get().cover_url_snapshot,"https://example.com/selected.jpg")

    def test_signed_empty_cover_and_legacy_signatures(self):
        catalog, selected, _ = self.catalog()
        for extra,expected in [({},catalog.cover_url),({"cover_url":""},"")]:
            token=signing.dumps({"selected":selected.pk,"scoring":selected.pk,**extra},salt="northbound.personal-tbr-selection")
            form=PersonalTbrRegistrationBookForm({"catalog_selection":token,"cover_url_snapshot":"https://example.com/client.jpg"})
            self.assertTrue(form.is_valid(),form.errors)
            self.assertEqual(form.cleaned_data["cover_url_snapshot"],expected)
        form=PersonalTbrRegistrationBookForm({"catalog_selection":token+'tampered'})
        self.assertFalse(form.is_valid())

    def test_manual_url_validation_never_fetches_images(self):
        with patch("core.integrations.hardcover.urlopen") as network:
            for value in ("", "https://example.com/cover.jpg", "http://example.com/cover.png"):
                form=PersonalTbrRegistrationBookForm({"title_snapshot":"Manual","author_snapshot":"Writer","cover_url_snapshot":value})
                self.assertTrue(form.is_valid(),form.errors)
                self.assertEqual(form.cleaned_data["cover_url_snapshot"],value)
            for value in ("javascript:alert(1)","data:image/png;base64,xx","ftp://example.com/cover.jpg","/cover.jpg","not a URL","https://example.com/"+'x'*1000):
                form=PersonalTbrRegistrationBookForm({"title_snapshot":"Manual","author_snapshot":"Writer","cover_url_snapshot":value})
                self.assertFalse(form.is_valid(),value)
                self.assertIn('cover_url_snapshot',form.errors)
            network.assert_not_called()

    def test_manual_cover_persists_for_staff_and_existing_preview_allows_manual_changes(self):
        data=self.payload([{**self.books(1)[0],"cover_url_snapshot":"https://example.com/manual.jpg"}])
        self.assertEqual(self.client.post(self.url,data).status_code,302)
        tbr=PersonalTBR.objects.get(enrollment=self.enrollment)
        self.assertEqual(tbr.books.get().cover_url_snapshot,"https://example.com/manual.jpg")
        response=self.client.get(self.url)
        self.assertContains(response,'src="https://example.com/manual.jpg"')
        self.assertContains(response,'Cover URL (Optional)')
        data=self.payload([{**self.books(1)[0],"cover_url_snapshot":"https://example.com/replacement.jpg","existing_selection":response.context['tbr_formset'].initial[0]['existing_selection']}])
        self.assertEqual(self.client.post(self.url,data).status_code,302)
        self.assertEqual(tbr.books.get().cover_url_snapshot,"https://example.com/replacement.jpg")

    def test_reader_manual_cover_round_trip_and_placeholder(self):
        self.month.registration_is_open=True;self.month.save()
        self.enrollment.is_active=False;self.enrollment.save()
        self.client.force_login(self.reader_user)
        url=reverse('challenge-register',args=[self.group.slug,self.month.pk])
        response=self.client.get(url)
        self.assertContains(response,'data-tbr-cover-placeholder')
        self.assertContains(response,'botm-cover-placeholder')
        data={'tbr-books-TOTAL_FORMS':'1','tbr-books-INITIAL_FORMS':'0','tbr-books-0-title_snapshot':'Reader book','tbr-books-0-author_snapshot':'Writer','tbr-books-0-cover_url_snapshot':'https://example.com/reader.jpg'}
        self.assertEqual(self.client.post(url,data).status_code,200)
        # Return from the review step to the same shared editor.
        self.assertContains(self.client.get(url),'src="https://example.com/reader.jpg"')
        response=self.client.post(reverse('challenge-register-confirm',args=[self.group.slug,self.month.pk]))
        self.assertEqual(response.status_code,302)
        self.assertEqual(PersonalTBR.objects.get(enrollment=self.enrollment).books.get().cover_url_snapshot,'https://example.com/reader.jpg')

    def test_existing_catalog_snapshot_is_preserved_and_client_override_ignored(self):
        catalog, selected, _=self.catalog()
        tbr=self.save([{'position':1,'title_snapshot':'Book','author_snapshot':'Author','catalog_book':catalog,'catalog_edition':selected,'cover_url_snapshot':'https://example.com/frozen.jpg'}])
        book=tbr.books.get()
        initial=self.client.get(self.url).context['tbr_formset'].initial[0]
        for cover in (None,'https://example.com/untrusted.jpg'):
            data={'existing_selection':initial['existing_selection'],'title_snapshot':'Book','author_snapshot':'Author'}
            if cover is not None:data['cover_url_snapshot']=cover
            form=StaffChallengeTbrBookForm(data,enrollment_id=self.enrollment.pk)
            self.assertTrue(form.is_valid(),form.errors)
            self.assertEqual(form.cleaned_data['cover_url_snapshot'],book.cover_url_snapshot)
            self.assertEqual(form.cleaned_data['catalog_book'],catalog)
        response=self.client.get(self.url)
        self.assertContains(response,'src="https://example.com/frozen.jpg"')
        self.assertContains(response,'data-catalog-cover-locked="true"')

    def test_manual_entries_without_covers_remain_supported(self):
        self.assertEqual(self.client.post(self.url,self.payload(self.books(1))).status_code,302)
        self.assertEqual(PersonalTBR.objects.get(enrollment=self.enrollment).books.get().cover_url_snapshot,'')
        self.assertContains(self.client.get(self.url),'data-tbr-cover-placeholder')

    def test_other_catalog_callers_keep_existing_cache_behavior(self):
        catalog, selected, _ = self.catalog()
        with patch("core.integrations.hardcover.execute_graphql") as graphql:
            result, cached = lookup_edition("actor", selected.provider_edition_id)
        graphql.assert_not_called()
        self.assertTrue(cached)
        self.assertEqual(result['cover_url'],catalog.cover_url)

    def test_staff_selected_catalog_cover_uses_signature_not_manual_url(self):
        catalog, selected, _ = self.catalog()
        signature=signing.dumps({'selected':selected.pk,'scoring':selected.pk,'cover_url':'https://example.com/selected.jpg'},salt='northbound.personal-tbr-selection')
        data=self.payload([{'catalog_selection':signature,'cover_url_snapshot':'https://example.com/client.jpg'}])
        self.assertEqual(self.client.post(self.url,data).status_code,302)
        saved=PersonalTBR.objects.get(enrollment=self.enrollment).books.get()
        self.assertEqual(saved.cover_url_snapshot,'https://example.com/selected.jpg')
        self.assertEqual(saved.catalog_edition,selected)
