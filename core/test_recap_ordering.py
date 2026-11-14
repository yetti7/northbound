from datetime import date
from django.test import TestCase
from django.urls import reverse
from . import test_recaps as fixture
from .models import ModifierProvenance
from .recaps import build_recap,filter_choices
from .recap_forms import RecapFilterForm
from .recap_slides import build_wrapped_slides
from .recap_exports import export_data


class RecapOrderingTests(TestCase):
    setUp=fixture.RecapTests.setUp
    make_book=fixture.RecapTests.make_book
    status=fixture.RecapTests.status

    def section(self,**options):
        return build_recap(viewer=self.admin,perspective='team',target_id=self.team.pk,
            challenge_id=self.month.pk,**options)['sections'][0]

    def test_contributions_use_total_descending_and_period_not_enrollment_order(self):
        self.make_book(self.leader,50)
        ModifierProvenance.objects.create(month=self.month,participant=self.members[self.leader.pk],source_type='game_reward',source_reference='sort-order',source_label='Fixture',amount=100,effective_date=date(2026,11,20))
        rows=self.section()['contributions']
        self.assertEqual([r['reader'] for r in rows],[self.leader.username,self.reader.username,self.host.username])
        self.assertEqual([r['points'] for r in rows],[150,100,0])
        early=self.section(period='custom',start=date(2026,11,3),end=date(2026,11,3))['contributions']
        self.assertEqual([r['points'] for r in early],[100,50,0])
        self.assertEqual(early[0]['reader'],self.reader.username)
        self.assertNotIn(self.other.username,str(rows))

    def test_case_insensitive_ties_and_shared_dashboard_slideshow_order(self):
        self.host.username='zulu';self.host.save(update_fields=['username'])
        self.leader.username='Alpha';self.leader.save(update_fields=['username'])
        self.make_book(self.leader,100);self.make_book(self.host,100)
        self.client.force_login(self.admin)
        params={'perspective':'team','team':self.team.pk,'challenge':self.month.pk}
        dashboard=self.client.get(self.url,params)
        wrapped=self.client.get(reverse('recap-wrapped'),params)
        expected=['Alpha',self.reader.username,'zulu']
        self.assertEqual([r['reader'] for r in dashboard.context['recap']['sections'][0]['contributions']],expected)
        slides=wrapped.context['slides'];rows=[r for s in slides if s['kind']=='contributions' for r in s['rows']]
        self.assertEqual([r['reader'] for r in rows],expected)
        exports=wrapped.context['export_data']
        self.assertNotIn('Alpha',str(exports));self.assertNotIn('zulu',str(exports))
        self.assertNotContains(wrapped,'data-export-identity="team" checked')

    def test_reader_dropdown_alphabetical_not_score_sorted_with_my_reading_special(self):
        self.host.username='zulu';self.host.save(update_fields=['username'])
        self.leader.username='Alpha';self.leader.save(update_fields=['username'])
        choices=filter_choices(self.admin,challenge_id=self.month.pk)['readers']
        labels=[label for pk,label in choices if pk!=self.admin.pk]
        self.assertEqual(labels,sorted(labels,key=str.casefold))
        form=RecapFilterForm({'perspective':'reader','challenge':str(self.month.pk)},viewer=self.admin)
        self.assertEqual(form.fields['reader'].choices[0],('','My Reading'))
        self.assertEqual([label for pk,label in form.fields['reader'].choices[1:]],['Reader · Alpha','Reader · forbidden-reader','Reader · recap-reader','Reader · zulu'])
        self.assertNotIn(self.outsider.pk,[pk for pk,label in choices])
        form=RecapFilterForm({'perspective':'reader'},viewer=self.reader)
        self.assertNotIn('reader',form.fields)

    def test_restricted_contributions_remain_absent(self):
        result=build_recap(viewer=self.reader,perspective='team',target_id=self.team.pk,challenge_id=self.month.pk)
        self.assertTrue(all(not section['contributions'] for section in result['sections']))
