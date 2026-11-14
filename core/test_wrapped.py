import json
from urllib.parse import urlsplit,parse_qs
from django.test import TestCase
from django.urls import reverse
from . import test_recaps as fixture
from .models import BookSubmission,CatalogBook,ChallengeMonth,TeamAssignment,MonthEnrollment,AuditEvent,ModifierProvenance
from .recaps import build_recap
from .recap_slides import build_wrapped_slides


class WrappedTests(TestCase):
    setUp=fixture.RecapTests.setUp
    make_book=fixture.RecapTests.make_book
    status=fixture.RecapTests.status

    def wrap(self,viewer,**params):
        self.client.force_login(viewer)
        return self.client.get(reverse('recap-wrapped'),{'challenge':self.month.pk,**params})

    def attach_covers(self):
        for book,title,path in [(self.book,'My approved cover book','own'),(self.secret,'Restricted tome ZX','secret')]:
            work=CatalogBook.objects.create(provider='hardcover',provider_book_id='wrapped-'+path,title=title,author='Author',cover_url='https://covers.example.test/'+path+'.png')
            book.title=title;book.catalog_book=work;book.save(update_fields=['title','catalog_book'])

    def test_dashboard_action_retains_validated_filters_only(self):
        self.client.force_login(self.reader)
        params={'perspective':'reader','challenge':self.month.pk,'group':self.group.pk,'period':'quarter','anchor':'2026-11-15','unrelated':'DO_NOT_COPY'}
        dashboard=self.client.get(self.url,params)
        self.assertContains(dashboard,'View Wrapped')
        query=parse_qs(urlsplit(dashboard.context['wrapped_url']).query)
        for key in ['perspective','challenge','group','period','anchor']:self.assertEqual(query[key],[str(params[key])])
        self.assertNotIn('unrelated',query)
        wrapped=self.client.get(dashboard.context['wrapped_url'])
        self.assertEqual(wrapped.status_code,200)
        self.assertEqual(parse_qs(urlsplit(wrapped.context['dashboard_url']).query),query)

    def test_all_perspectives_reuse_authorized_cards(self):
        for perspective,viewer,extra in [('reader',self.reader,{}),('team',self.admin,{'team':self.team.pk}),('challenge',self.admin,{})]:
            page=self.wrap(viewer,perspective=perspective,**extra)
            self.assertEqual(page.status_code,200)
            self.assertContains(page,'Progress Snapshot')
            expected=build_recap(viewer=viewer,perspective=perspective,target_id=extra.get('team'),challenge_id=self.month.pk)
            actual=page.context['recap']
            for section in actual['sections']:section.pop('books',None)
            expected.pop('generated_at');actual.pop('generated_at')
            self.assertEqual(actual,expected)
            self.assertEqual(page.context['slides'][0]['kind'],'opening')

    def test_final_modes_are_current_results_and_other_stages_have_no_slides(self):
        for status in ['completed','archived']:
            self.status(status)
            page=self.wrap(self.reader)
            self.assertContains(page,'Final Wrapped')
            self.assertTrue(page.context['slides'])
        for status in ['draft','upcoming','finalizing']:
            self.status(status)
            page=self.wrap(self.reader)
            self.assertContains(page,'No Wrapped slides available')
            self.assertFalse(page.context['slides'])

    def test_active_aggregates_and_unauthorized_book_metadata_absent(self):
        self.attach_covers()
        page=self.wrap(self.reader,perspective='challenge')
        self.assertFalse(page.context['slides'])
        for hidden in ['978321','Restricted tome ZX','secret.png','My approved cover book','own.png']:
            self.assertNotContains(page,hidden)
        self.assertEqual(page.context['recap']['sections'], [])

    def test_competitive_results_never_imply_book_title_access(self):
        self.attach_covers();self.status('completed')
        page=self.wrap(self.reader,perspective='challenge')
        self.assertContains(page,'978321') # Completed competitive results are public within the Group.
        self.assertNotContains(page,'Restricted tome ZX');self.assertNotContains(page,'secret.png')
        self.assertContains(page,'My approved cover book')
        self.assertContains(page,'data-cover-src="https://covers.example.test/own.png"')
        self.assertNotContains(page,' src="https://covers.example.test/own.png"')

    def test_host_owner_and_team_leader_covers_remain_team_scoped(self):
        self.attach_covers()
        self.month.reader_scores_visibility='hosts_leaders';self.month.team_standings_visibility='hosts_leaders'
        self.month.save(update_fields=['reader_scores_visibility','team_standings_visibility'])
        for viewer in [self.host,self.leader]:
            page=self.wrap(viewer,perspective='team',team=self.team.pk)
            self.assertContains(page,'My approved cover book')
            self.assertNotContains(page,'Restricted tome ZX');self.assertNotContains(page,'secret.png')
            denied=self.wrap(viewer,perspective='team',team=self.team_b.pk)
            self.assertFalse(denied.context['slides'])
            self.assertNotContains(denied,'Restricted tome ZX')

    def test_public_team_totals_do_not_expose_other_teams_books(self):
        self.attach_covers();self.month.team_standings_visibility='everybody';self.month.save(update_fields=['team_standings_visibility'])
        page=self.wrap(self.reader,perspective='team',team=self.team_b.pk)
        self.assertContains(page,'978321')
        self.assertNotContains(page,'Restricted tome ZX');self.assertNotContains(page,'secret.png')
        self.assertFalse(any(slide['kind']=='books' for slide in page.context['slides']))

    def test_independent_reader_scores_and_standings_slide_permissions(self):
        self.month.reader_scores_visibility='everybody';self.month.save(update_fields=['reader_scores_visibility'])
        page=self.wrap(self.reader,perspective='team',team=self.team_b.pk)
        self.assertTrue(any(slide['kind']=='contributions' for slide in page.context['slides']))
        self.assertFalse(any(slide['kind']=='points' for slide in page.context['slides']))
        self.assertNotContains(page,'Team Challenge points')

    def test_historical_self_access_retains_only_own_books_and_generic_context(self):
        self.attach_covers();self.status('completed')
        member=self.members[self.reader.pk];member.is_active=False;member.save(update_fields=['is_active'])
        page=self.wrap(self.reader)
        self.assertContains(page,'My approved cover book')
        self.assertContains(page,'Personal history')
        for hidden in ['Reading Group','November recap','Forbidden violet team','Restricted tome ZX','secret.png']:
            self.assertNotContains(page,hidden)

    def test_missing_unsafe_and_conflicting_cover_urls_use_placeholders(self):
        page=self.wrap(self.reader)
        self.assertContains(page,'data-book-cover-placeholder')
        self.assertNotContains(page,'data-cover-src=')
        work=CatalogBook.objects.create(provider='hardcover',provider_book_id='unsafe-cover',title='Book',author='Author',cover_url='https://user:password@covers.example.test/secret.png')
        self.book.catalog_book=work;self.book.save(update_fields=['catalog_book'])
        page=self.wrap(self.reader)
        self.assertNotContains(page,'password@');self.assertNotContains(page,'secret.png')
        work.cover_url='javascript:alert(1)';CatalogBook.objects.filter(pk=work.pk).update(cover_url=work.cover_url)
        self.assertNotContains(self.wrap(self.reader),'javascript:')

    def test_covers_are_capped_approved_nonvoid_and_date_filtered_without_deduplication(self):
        for index in range(9):self.make_book(self.reader,10)
        pending=BookSubmission.objects.create(month=self.month,participant=self.members[self.reader.pk],title='Pending forbidden cover',author='Author',book_format='ebook',completed_on=self.month.starts_on,submitted_pages=10,status='pending')
        self.book.is_removed=True;self.book.save(update_fields=['is_removed'])
        result=build_recap(viewer=self.reader,challenge_id=self.month.pk,include_book_highlights=True)
        self.assertEqual(len(result['sections'][0]['books']),6)
        page=self.wrap(self.reader)
        self.assertNotContains(page,'Pending forbidden cover')
        empty=self.wrap(self.reader,period='custom',start='2026-11-20',end='2026-11-20')
        self.assertFalse(any(slide['kind']=='books' for slide in empty.context['slides']))

    def test_group_isolation_and_direct_url_authentication(self):
        foreign=ChallengeMonth.objects.get(group=self.foreign)
        page=self.wrap(self.reader,group=self.foreign.pk,challenge=foreign.pk)
        self.assertFalse(page.context['slides'])
        self.assertNotContains(page,'Secret foreign Group');self.assertNotContains(page,'Secret foreign book')
        self.client.logout()
        self.assertEqual(self.client.get(reverse('recap-wrapped')).status_code,302)
        self.client.force_login(self.reader)
        self.assertEqual(self.client.post(reverse('recap-wrapped')).status_code,405)

    def test_presentation_is_readonly_private_and_supports_accessible_navigation(self):
        self.client.force_login(self.reader)
        models=[BookSubmission,ModifierProvenance,AuditEvent,MonthEnrollment,TeamAssignment]
        before={m.__name__:list(m.objects.values()) for m in models}
        page=self.client.get(reverse('recap-wrapped'),{'challenge':self.month.pk})
        for model in models:self.assertEqual(before[model.__name__],list(model.objects.values()))
        for marker in ['aria-roledescription="carousel"','aria-live="polite"','aria-label="Wrapped slide progress"','Back to Chronicle','<noscript>','data-wrapped-prev','data-wrapped-next']:
            self.assertContains(page,marker)
        self.assertIn('private',page['Cache-Control']);self.assertIn('no-store',page['Cache-Control'])

    def test_slide_adapter_only_presents_provided_data_and_chunks_long_rows(self):
        recap={'heading':'Team Wrapped','perspective':'team','period':'All recorded results','summary':[],
            'notes':['Safe note'],'sections':[{'subject':'Team','challenge':'Challenge','group':'Group','mode':'Final Wrapped','timezone':'UTC','from_date':'','to_date':'','cards':[],
                'teams':[{'name':'Permitted team','points':10}]*13,'contributions':[]}]}
        slides=build_wrapped_slides(recap)
        rows=[slide for slide in slides if slide['kind']=='teams']
        self.assertEqual([len(slide['rows']) for slide in rows],[6,6,1])
        self.assertFalse(any(slide['kind']=='books' for slide in slides))
        self.assertEqual(build_wrapped_slides(None),[])
