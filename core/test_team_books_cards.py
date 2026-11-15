"""Team Books presentation preserves existing inclusion, scope and saved data."""
from datetime import date
from django.test import TestCase
from django.template.loader import render_to_string
from django.urls import reverse
from . import test_team_detail_reporting as fixture
from .models import BookSubmission, CatalogBook, ModifierProvenance, TeamAssignment
from .templatetags.team_books import safe_reference_url, recorded_pages

class TeamBooksCardsTests(TestCase):
    setUp = fixture.TeamDetailReportingTests.setUp
    member = fixture.TeamDetailReportingTests.member
    submission = fixture.TeamDetailReportingTests.submission

    def page(self):
        self.client.force_login(self.host_user)
        return self.client.get(self.url,{'view':'books'})

    def gallery(self,response):
        return response.content.decode().split('<section id="team-books">',1)[1].split('</section>',1)[0]

    def test_existing_order_pending_approved_and_filters_preserved(self):
        pending=BookSubmission.objects.create(month=self.month,participant=self.alpha,title='Newest Pending',author='Writer',book_format='ebook',completed_on=date(2026,8,10),submitted_pages=1)
        rejected=self.submission(self.alpha,'Rejected hidden',date(2026,8,10),100)
        BookSubmission.objects.filter(pk=rejected.pk).update(status='rejected')
        removed=self.submission(self.alpha,'Removed hidden',date(2026,8,10),100)
        BookSubmission.objects.filter(pk=removed.pk).update(is_removed=True)
        self.submission(self.ordinary,'Other Team hidden',date(2026,8,10),100)
        before=list(BookSubmission.objects.order_by('pk').values())
        page=self.page();html=self.gallery(page)
        expected=list(BookSubmission.objects.filter(pk__in=[self.alpha_submission.pk,pending.pk]+list(BookSubmission.objects.filter(title__in=['Alpha Late','Beta Middle']).values_list('pk',flat=True))).order_by('-submitted_at','-pk').values_list('pk',flat=True))
        self.assertEqual([row['pk'] for row in page.context['team_books']['items']],expected)
        self.assertIn('Newest Pending',html);self.assertIn('Alpha Early',html);self.assertIn('1 page',html)
        for text in ('Rejected hidden','Removed hidden','Other Team hidden'):self.assertNotIn(text,html)
        self.assertEqual(before,list(BookSubmission.objects.order_by('pk').values()))
        pending.refresh_from_db();self.assertEqual(pending.status,'pending')

    def test_only_requested_information_and_saved_base_pages_are_displayed(self):
        BookSubmission.objects.filter(pk=self.alpha_submission.pk).update(approved_pages=352,bonus_pages=999,final_scored_pages=1351,notes='PRIVATE READER NOTES',review_notes='PRIVATE STAFF NOTES')
        before=list(ModifierProvenance.objects.values())
        html=self.gallery(self.page())
        for text in ('Alpha Early','Author','352 pages',self.alpha_user.username):self.assertIn(text,html)
        for text in ('<time','class="pill"','>Approved<','>Pending<','999','1351','PRIVATE READER NOTES','PRIVATE STAFF NOTES'):self.assertNotIn(text,html)
        self.assertEqual(before,list(ModifierProvenance.objects.values()))

    def test_reference_link_is_conditional_and_uses_existing_external_conventions(self):
        BookSubmission.objects.filter(pk=self.alpha_submission.pk).update(reference_url='https://books.example.com/title?a=1&b=2')
        html=self.gallery(self.page())
        self.assertIn('href="https://books.example.com/title?a=1&amp;b=2" target="_blank" rel="noopener noreferrer">Alpha Early</a>',html)
        self.assertNotIn('>Alpha Late</a>',html)
        self.assertNotIn(reverse('team-book-detail',args=[self.group.slug,self.month.pk,self.team.pk,self.alpha_submission.pk]),html)
        for bad in ('javascript:alert(1)','data:text/html,unsafe','//untrusted.example/book','not a URL'):
            BookSubmission.objects.filter(pk=self.alpha_submission.pk).update(reference_url=bad)
            html=self.gallery(self.page());self.assertIn('<h3>Alpha Early</h3>',html);self.assertNotIn('href="'+bad,html)

    def test_cover_and_existing_fallback_are_preserved(self):
        catalog=CatalogBook.objects.create(provider='hardcover',provider_book_id='team-gallery',title='Catalog identity',author='Catalog author',cover_url='https://images.example.com/cover.jpg')
        BookSubmission.objects.filter(pk=self.alpha_submission.pk).update(catalog_book=catalog)
        html=self.gallery(self.page())
        self.assertIn('src="https://images.example.com/cover.jpg"',html)
        self.assertIn('alt="Cover of Alpha Early"',html)
        self.assertIn('data-book-cover-placeholder',html)
        self.assertIn('Alpha Early',html);self.assertNotIn('Catalog identity',html)

    def test_page_count_fallbacks_and_missing_author(self):
        values={'title':'No pages','author':'','status':'pending','submitted_pages':None,'approved_pages':None,'participant__user__username':'Reader'}
        html=render_to_string('core/includes/team_book_card.html',{'book':values,'month':self.month,'compact':True})
        self.assertIn('Pages unavailable',html);self.assertIn('Author unavailable',html)
        self.assertEqual(recorded_pages({'status':'pending','approved_pages':100,'submitted_pages':25}),25)
        self.assertEqual(recorded_pages({'status':'approved','approved_pages':None,'submitted_pages':50}),50)
        self.assertEqual(recorded_pages({'status':'approved','approved_pages':0,'submitted_pages':50}),0)

    def test_invalid_reference_schemes_fail_closed(self):
        for value in (None,'','javascript:alert(1)','data:text/html,book','ftp://books.example.com/a','//books.example.com/a','https://books.example.com/\nunsafe'):
            self.assertEqual(safe_reference_url(value),'')
        self.assertEqual(safe_reference_url(' https://books.example.com/a '),'https://books.example.com/a')

    def test_other_team_reader_still_cannot_view_books(self):
        self.client.force_login(self.ordinary_user)
        response=self.client.get(self.url,{'view':'books'})
        self.assertNotContains(response,'id="team-books"');self.assertNotContains(response,'Alpha Early')
        self.assertEqual(self.client.get(reverse('team-book-detail',args=[self.group.slug,self.month.pk,self.team.pk,self.alpha_submission.pk])).status_code,403)

    def test_legacy_team_book_detail_keeps_existing_presentation(self):
        self.client.force_login(self.host_user)
        response=self.client.get(reverse('team-book-detail',args=[self.group.slug,self.month.pk,self.team.pk,self.alpha_submission.pk]))
        self.assertContains(response,'<time');self.assertContains(response,'Approved')
        self.assertNotContains(response,'css/team_books.css')
