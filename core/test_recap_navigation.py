from django.test import TestCase
from django.urls import reverse
from django.core.exceptions import PermissionDenied
from . import test_recaps as fixture
from .models import Membership,ChallengeMonth,MonthEnrollment,Team,TeamAssignment,ChallengeStaffAssignment
from .recaps import build_recap,can_view_final_wrapped


class RecapNavigationTests(TestCase):
    setUp=fixture.RecapTests.setUp
    make_book=fixture.RecapTests.make_book
    status=fixture.RecapTests.status

    def page(self,user,**data):
        self.client.force_login(user)
        return self.client.get(reverse('recap-wrapped'),{'group':self.group.pk,'challenge':self.month.pk,'period':'all',**data})

    def test_nonadmins_cannot_select_readers_in_form_or_service(self):
        staff=Membership.objects.create(group=self.group,user=self.outsider,role='moderator')
        ChallengeStaffAssignment.objects.create(month=self.month,membership=staff,role='floater')
        for user in [self.reader,self.host,self.leader,self.outsider]:
            self.client.force_login(user)
            page=self.client.get(self.url,{'group':self.group.pk,'challenge':self.month.pk})
            self.assertNotContains(page,'id="id_reader"')
            self.assertNotContains(page,'<option value="'+str(self.other.pk)+'"')
            forged=self.page(user,reader=self.other.pk)
            self.assertFalse(forged.context['slides'])
            with self.assertRaises(PermissionDenied):build_recap(viewer=user,target_id=self.other.pk)
        admin=self.page(self.admin,reader=self.reader.pk)
        self.assertTrue(admin.context['slides'])
        self.assertContains(admin,self.reader.username+' — My Reading')

    def test_primary_order_group_isolation_and_stale_reader_ignored_other_perspectives(self):
        self.client.force_login(self.admin)
        page=self.client.get(self.url,{'group':self.group.pk,'perspective':'team'})
        html=page.content.decode()
        self.assertLess(html.index('id="id_group"'),html.index('id="id_challenge"'))
        self.assertLess(html.index('id="id_challenge"'),html.index('id="id_perspective"'))
        self.assertNotContains(page,'id="id_reader"')
        self.assertNotIn('Secret foreign Challenge',[label for _,label in page.context['form'].fields['challenge'].choices])
        for perspective in ['team','challenge']:
            clean=self.page(self.admin,perspective=perspective)
            stale=self.page(self.admin,perspective=perspective,reader=self.outsider.pk)
            self.assertEqual(clean.context['slides'],stale.context['slides'])
            self.assertNotIn('reader=',stale.context['dashboard_url'])
        foreign=ChallengeMonth.objects.get(group=self.foreign)
        wrong=self.page(self.admin,challenge=foreign.pk)
        self.assertFalse(wrong.context['slides'])
        self.assertNotContains(wrong,'Secret foreign book')

    def test_completed_all_teams_and_roles_without_private_book_disclosure(self):
        team3=Team.objects.create(month=self.month,name='Third team')
        unassigned=Membership.objects.create(group=self.group,user=self.outsider,role='member')
        moderator=Membership.objects.get(pk=self.members[self.leader.pk].pk)
        for status in ['completed','archived']:
            self.status(status)
            for user in [self.reader,self.host,self.leader,self.outsider,self.admin]:
                result=build_recap(viewer=user,perspective='team',challenge_id=self.month.pk,include_book_highlights=True)
                self.assertEqual({s['subject'] for s in result['sections']},{self.team.name,self.team_b.name,team3.name})
                for section in result['sections']:
                    self.assertTrue(section['cards'])
                    if user==self.reader and section['subject']==self.team_b.name:
                        self.assertEqual(section['books'],[])
            self.assertTrue(can_view_final_wrapped(self.outsider,self.month))
            self.assertTrue(self.page(self.outsider,perspective='challenge').context['slides'])
        # Moderator and Floater get the same completed competition access, not more book access.
        moderator.role='moderator';moderator.save()
        ChallengeStaffAssignment.objects.filter(membership=moderator).update(ended_at=__import__('django.utils.timezone',fromlist=['now']).now())
        MonthEnrollment.objects.filter(month=self.month,participant=moderator).update(is_active=False)
        TeamAssignment.objects.filter(month=self.month,participant=moderator).update(ended_at=__import__('django.utils.timezone',fromlist=['now']).now())
        ChallengeStaffAssignment.objects.create(month=self.month,membership=moderator,role='floater')
        result=build_recap(viewer=self.leader,perspective='team',challenge_id=self.month.pk)
        self.assertEqual(len(result['sections']),3)

    def test_earlier_stages_do_not_grant_completed_access(self):
        for status in ['draft','upcoming','active','finalizing']:
            self.status(status)
            self.assertFalse(can_view_final_wrapped(self.reader,self.month))
            result=self.page(self.reader,perspective='challenge')
            self.assertFalse(result.context['slides'])
            with self.assertRaises(PermissionDenied):
                build_recap(viewer=self.leader,perspective='team',target_id=self.team_b.pk,challenge_id=self.month.pk)
            if status=='active':self.assertTrue(self.page(self.admin,perspective='challenge').context['slides'])

    def test_card_button_only_completed_or_archived_correct_full_period_url(self):
        self.client.force_login(self.reader)
        url=reverse('month-list',args=[self.group.slug])
        for status in ['upcoming','active','finalizing','completed','archived']:
            self.status(status)
            response=self.client.get(url,{'archive':'1'} if status=='archived' else {})
            expected=status in ['completed','archived']
            self.assertEqual('View Wrapped' in response.content.decode(),expected)
            if expected:
                href=f'{reverse("recap-wrapped")}?group={self.group.pk}&amp;challenge={self.month.pk}&amp;perspective=challenge&amp;period=all'
                self.assertContains(response,href)
                self.assertTrue(self.page(self.reader,perspective='challenge').context['slides'])
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(url).status_code,403)
        denied=self.page(self.outsider,perspective='challenge')
        self.assertFalse(denied.context['slides'])
        self.assertNotContains(denied,'978321')

    def test_history_cross_challenge_periods_and_no_store(self):
        member=self.members[self.reader.pk];member.is_active=False;member.save()
        result=self.page(self.reader)
        self.assertTrue(result.context['slides'])
        self.assertNotContains(result,self.group.name)
        self.assertIn('no-store',result['Cache-Control'])
        self.assertIn('private',result['Cache-Control'])
        self.client.force_login(self.admin)
        for period in ['all','month','quarter','year','custom']:
            response=self.client.get(self.url,{'group':self.group.pk,'perspective':'challenge','period':period,'anchor':'2026-11-15','start':'2026-11-01','end':'2026-11-30'})
            self.assertTrue(response.context['form'].is_valid())
            self.assertContains(response,'View Wrapped')

    def test_transferred_withdrawn_attribution_remains_authoritative(self):
        self.status('completed')
        enrollment=MonthEnrollment.objects.get(month=self.month,participant=self.members[self.reader.pk])
        enrollment.is_active=False;enrollment.credited_team=self.team;enrollment.save()
        TeamAssignment.objects.filter(month=self.month,participant=enrollment.participant).update(ended_at=__import__('django.utils.timezone',fromlist=['now']).now())
        result=build_recap(viewer=self.reader,perspective='team',challenge_id=self.month.pk)
        a=next(s for s in result['sections'] if s['subject']==self.team.name)
        self.assertEqual(next(c['value'] for c in a['cards'] if c['key']=='team_points'),100)
        self.assertTrue(any(c['key']=='entries' and c['value']==1 for c in a['cards']))
