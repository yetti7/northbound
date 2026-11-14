from datetime import date
from django.test import TestCase
from django.urls import reverse
from . import test_recaps as fixture
from .models import CatalogBook, CatalogEdition, BookSubmission, MonthEnrollment, TeamAssignment, ThemeClaim, MonthTheme
from .recaps import build_recap
from .recap_slides import team_bars, build_wrapped_slides


class RecapPresentationTests(TestCase):
    setUp=fixture.RecapTests.setUp
    make_book=fixture.RecapTests.make_book
    status=fixture.RecapTests.status

    def work(self, identity, cover=True, provider='hardcover'):
        return CatalogBook.objects.create(provider=provider,provider_book_id=identity,title=identity,author='Writer',cover_url='https://covers.example.test/'+identity+'.png' if cover else '')

    def add(self, work, *, title=None, reader=None, **changes):
        book=self.make_book(reader or self.reader,10)
        book.title=title or (work.title if work else 'Same unknown title')
        book.catalog_book=work
        for key,value in changes.items():setattr(book,key,value)
        book.save()
        return book

    def books(self, viewer=None, **filters):
        return build_recap(viewer=viewer or self.reader,challenge_id=self.month.pk,include_book_highlights=True,**filters)['sections'][0]['books']

    def test_selector_has_exactly_one_personal_option_and_accepts_existing_deep_link(self):
        self.client.force_login(self.admin)
        for value in ['',str(self.admin.pk)]:
            response=self.client.get(self.url,{'reader':value})
            choices=response.context['form'].fields['reader'].choices
            self.assertEqual([label for _,label in choices].count('My Reading'),1)
            self.assertEqual(len([v for v,_ in choices if str(v) in ('',str(self.admin.pk))]),1)
            self.assertNotIn('My reading',[label for _,label in choices])
        response=self.client.get(self.url,{'reader':self.reader.pk})
        self.assertIn('Reader · '+self.reader.username,[label for _,label in response.context['form'].fields['reader'].choices])
        self.client.force_login(self.reader)
        private=self.client.get(self.url,{'challenge':self.month.pk})
        self.assertNotContains(private,'unrelated-reader')
        self.assertNotContains(private,'forbidden-reader')

    def test_headings_summary_logo_and_methodology_all_perspectives(self):
        self.client.force_login(self.admin)
        for perspective,extra,heading in [('reader',{'reader':self.reader.pk},self.reader.username+' — My Reading'),('team',{'team':self.team.pk},'Reading Group — Team Wrapped'),('challenge',{},'Reading Group — Challenge Wrapped')]:
            response=self.client.get(reverse('recap-wrapped'),{'perspective':perspective,'challenge':self.month.pk,**extra})
            self.assertEqual(response.context['recap']['heading'],heading)
            self.assertContains(response,'data-recap-summary')
            self.assertContains(response,'northbound-header-dark.png')
            self.assertContains(response,'width="700" height="233"')
            self.assertEqual(response.context['slides'][-1]['kind'],'summary')
            self.assertNotContains(response,'Every chapter counts')
            self.assertNotContains(response,'Your chosen chapter')
            self.assertNotContains(response,'not an exhaustive list')
            self.assertContains(response,heading)
        self.client.force_login(self.reader)
        response=self.client.get(self.url,{'challenge':self.month.pk})
        self.assertEqual(response.context['recap']['heading'],self.reader.username+' — My Reading')
        self.assertContains(response,'How are these numbers calculated?')

    def test_covers_before_frequency_and_trusted_identity_grouping(self):
        self.book.delete()
        covered=self.work('covered');popular=self.work('popular');plain=self.work('plain',False)
        first=self.add(covered)
        for index in range(5):
            theme=MonthTheme.objects.create(month=self.month,name=f'Theme {index}',starts_on=self.month.starts_on,ends_on=self.month.ends_on)
            ThemeClaim.objects.create(submission=first,theme=theme,status='approved',approved_bonus_pages=1)
        for _ in range(3):self.add(popular)
        for _ in range(5):self.add(plain)
        books=self.books()
        self.assertEqual([b['title'] for b in books],['popular','covered','plain'])
        # One qualified submission counts once, regardless of its awards/claims.
        self.assertEqual(len(books),3)
        for index in range(3):self.add(covered,status='pending')
        self.add(covered,is_removed=True)
        self.assertEqual(self.books(),books)

    def test_unknown_and_conflicting_identity_do_not_merge_and_ties_are_stable(self):
        self.book.delete()
        first=self.add(None);second=self.add(None)
        work=self.work('linked');other=self.work('different')
        edition=CatalogEdition.objects.create(book=other,provider='hardcover',provider_edition_id='conflict',page_count=100)
        self.add(work,catalog_edition=edition)
        self.add(work,catalog_edition=edition)
        result=self.books()
        self.assertEqual(len(result),4)
        self.assertEqual([b['title'] for b in result[:2]],['Same unknown title']*2)
        self.assertFalse(any(b['cover_url'] for b in result))
        self.assertEqual(result,self.books())

    def test_private_reader_frequency_does_not_use_other_readers_popularity(self):
        self.book.delete()
        a=self.work('earlier');b=self.work('later')
        self.add(a);self.add(b)
        for _ in range(8):self.add(b,reader=self.other)
        self.assertEqual([r['title'] for r in self.books()],['earlier','later'])
        team=self.books(viewer=self.admin,perspective='team',target_id=self.team.pk)
        self.assertEqual([r['title'] for r in team],['earlier','later'])
        challenge=self.books(viewer=self.admin,perspective='challenge')
        self.assertEqual([r['title'] for r in challenge][:2],['later','earlier'])
        # Participating Host cannot use other-team frequency or covers.
        scoped=self.books(viewer=self.host,perspective='team',target_id=self.team.pk)
        self.assertEqual([r['title'] for r in scoped],['earlier','later'])

    def test_period_frequency_is_bounded_and_cross_challenge_sections_stay_separate(self):
        self.book.delete()
        a=self.work('earlier');b=self.work('later');self.add(a);self.add(b)
        for _ in range(3):self.add(b,completed_on=date(2026,11,20))
        bounded=self.books(period='custom',start=date(2026,11,1),end=date(2026,11,5))
        self.assertEqual([r['title'] for r in bounded],['earlier','later'])
        self.assertEqual(self.books()[0]['title'],'later')
        from .models import ChallengeMonth
        month=ChallengeMonth.objects.create(group=self.group,name='Other month',status='completed',starts_on=date(2026,11,1),ends_on=date(2026,11,30))
        MonthEnrollment.objects.create(month=month,participant=self.members[self.reader.pk])
        book=self.add(a);BookSubmission.objects.filter(pk=book.pk).update(month=month)
        recap=build_recap(viewer=self.reader,include_book_highlights=True)
        self.assertEqual(len(recap['sections']),2)
        self.assertEqual(len(next(s for s in recap['sections'] if s['challenge']=='Other month')['books']),1)

    def test_local_and_provider_covers_have_equal_priority_and_invalid_covers_do_not(self):
        self.book.delete()
        local=self.work('local');local.cover_url='http://northbound.example.test/media/upload.png';local.save()
        external=self.work('external');bad=self.work('bad');bad.cover_url='https://user:secret@covers.example.test/x';bad.save()
        self.add(local);self.add(external)
        for _ in range(6):self.add(bad)
        self.assertEqual([b['title'] for b in self.books()],['local','external','bad'])
        self.assertFalse(self.books()[-1]['cover_url'])

    def test_summary_uses_exact_section_books_and_restricted_data_is_absent(self):
        self.client.force_login(self.reader)
        work=self.work('secret-cover');self.secret.catalog_book=work;self.secret.title='PRIVATE NEVER SERIALIZE';self.secret.save()
        response=self.client.get(reverse('recap-wrapped'),{'perspective':'reader','challenge':self.month.pk})
        summary=response.context['slides'][-1]
        self.assertEqual(summary['sections'][0]['books'],response.context['recap']['sections'][0]['books'])
        self.assertNotContains(response,'PRIVATE NEVER SERIALIZE');self.assertNotContains(response,'secret-cover.png')
        restricted=self.client.get(reverse('recap-wrapped'),{'perspective':'challenge','challenge':self.month.pk})
        self.assertNotContains(restricted,'data-recap-summary')
        self.assertNotContains(restricted,'978321')

    def test_team_chart_scaling_and_text_equivalent(self):
        bars=team_bars([{'name':'A','points':200},{'name':'B','points':50},{'name':'C','points':0}])
        self.assertEqual([r['bar_width'] for r in bars],[100,25,0])
        self.assertEqual(team_bars([{'name':'Z','points':0}])[0]['bar_width'],0)
        self.client.force_login(self.admin)
        response=self.client.get(reverse('recap-wrapped'),{'perspective':'challenge','challenge':self.month.pk})
        self.assertContains(response,'aria-label="Team points comparison"')
        self.assertContains(response,'978321 points')
        self.client.force_login(self.reader)
        restricted=self.client.get(reverse('recap-wrapped'),{'perspective':'team','team':self.team_b.pk,'challenge':self.month.pk})
        self.assertNotContains(restricted,'978321 points')
