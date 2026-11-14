from copy import deepcopy
import json
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from . import test_team_branding as fixture
from .test_branding import upload
from .models import ChallengeMonth, Membership, MonthEnrollment, TeamAssignment, Team, AuditEvent
from .branding import update_branding


class WrappedBrandingTests(TestCase):
    setUpFixture=fixture.TeamBrandingTests.setUpFixture
    staff=fixture.TeamBrandingTests.staff
    status=fixture.TeamBrandingTests.status
    make_book=fixture.TeamBrandingTests.make_book
    save=fixture.TeamBrandingTests.save
    branding_url=fixture.TeamBrandingTests.branding_url

    def setUp(self):
        fixture.TeamBrandingTests.setUp(self)
        for target in [self.group,self.month,self.team,self.team_b]:
            update_branding(actor=self.admin,target=target,data={'branding_mode':'banner'},files={'logo':upload(),'banner':upload()})
        self.group.refresh_from_db();self.month.refresh_from_db();self.team.refresh_from_db();self.team_b.refresh_from_db()

    def wrap(self,viewer=None,**params):
        self.client.force_login(viewer or self.reader)
        query={'group':self.group.pk,'challenge':self.month.pk,'perspective':'reader',**params}
        return self.client.get(reverse('recap-wrapped'),query)

    def options(self,page):return page.context['export_data']['branding_options']

    def test_defaults_consent_and_no_eager_community_art(self):
        page=self.wrap();self.assertEqual(page.status_code,200)
        self.assertNotIn('branding',page.context['export_data'])
        self.assertEqual(page.context['export_data']['branding_selection']['branding_source'],'challenge')
        self.assertContains(page,'Include Community Branding');self.assertNotContains(page,'type="checkbox" checked')
        opening=page.content.decode().split('class="wrapped-slide wrapped-slide-opening"')[1].split('</article>')[0]
        self.assertNotIn('/media/',opening)
        self.assertContains(page,'data-export-identity="reader"');self.assertNotContains(page,'data-export-identity="team"')
        for key in ['group','challenge','team']:
            params={'branding_source':key}
            if key=='team':params['branding_team']=self.team.pk
            selected=self.wrap(**params);self.assertEqual(selected.status_code,200)
            self.assertNotContains(selected,'type="checkbox" checked')

    def test_role_matrix_uses_identity_authority_not_standings(self):
        self.status('completed')
        for viewer in [self.admin,self.owner,self.mod,self.host,self.floater]:
            page=self.wrap(viewer,perspective='challenge')
            self.assertEqual({t['id'] for t in self.options(page)['teams']},{str(self.team.pk),str(self.team_b.pk)})
        for viewer in [self.reader,self.team_leader]:
            page=self.wrap(viewer,perspective='challenge')
            self.assertEqual({t['id'] for t in self.options(page)['teams']},{str(self.team.pk)})
            # Completed results can expose other Team totals, not their artwork choices.
            self.assertNotIn(self.team_b.logo.url,json.dumps(self.options(page)))

    def test_forged_ids_sources_groups_and_unauthorized_choices_rejected(self):
        self.status('completed')
        foreign_team=Team.objects.create(month=self.foreign.challenge_months.first(),name='Private foreign Team')
        foreign_team.logo.save('foreign.png',upload(),save=True)
        self.assertNotIn(foreign_team.logo.url,json.dumps(self.options(self.wrap(self.admin,perspective='challenge'))))
        for params in [{'branding_source':'team','branding_team':foreign_team.pk},{'branding_source':'team','branding_team':self.team_b.pk},
                       {'branding_source':'team','branding_team':999999},
                       {'branding_source':'team'}, {'branding_source':'evil'},
                       {'branding_source':'group','branding_team':self.team.pk}]:
            page=self.wrap(**params);self.assertEqual(page.status_code,400,params)
            self.assertNotContains(page,'wrapped-export-data',status_code=400)
            self.assertNotContains(page,self.team_b.logo.url,status_code=400)
        denied=self.wrap(self.outsider,branding_source='team',branding_team=self.team.pk)
        self.assertEqual(denied.status_code,400)
        foreign=self.wrap(group=self.foreign.pk,branding_source='group')
        self.assertEqual(foreign.status_code,400)

    def test_transfers_current_assignment_only_and_inactive_enrollment(self):
        from .participation import assign_participant_to_team
        self.assertEqual([t['id'] for t in self.options(self.wrap())['teams']],[str(self.team.pk)])
        assign_participant_to_team(month=self.month,participant=self.members[self.reader.pk],team=self.team_b,actor=self.admin)
        page=self.wrap();options=self.options(page)
        self.assertEqual([t['id'] for t in options['teams']],[str(self.team_b.pk)])
        self.assertNotIn(self.team.logo.url,json.dumps(options))
        self.assertTrue(TeamAssignment.objects.filter(participant=self.members[self.reader.pk],team=self.team,ended_at__isnull=False).exists())
        MonthEnrollment.objects.filter(participant=self.members[self.reader.pk],month=self.month).update(is_active=False)
        self.assertFalse(self.options(self.wrap())['teams'])

    def test_single_team_default_does_not_select_inaccessible_or_other_team(self):
        self.status('completed')
        page=self.wrap(self.admin,perspective='team',team=self.team.pk)
        self.assertEqual(page.context['export_data']['branding_selection'],{'branding_source':'team','branding_team':str(self.team.pk)})
        page=self.wrap(self.reader,perspective='team',team=self.team_b.pk)
        self.assertEqual(page.context['export_data']['branding_selection']['branding_source'],'challenge')
        alternate=self.wrap(self.admin,perspective='team',team=self.team.pk,branding_source='team',branding_team=self.team_b.pk)
        self.assertEqual(alternate.context['recap']['sections'][0]['subject'],self.team.name)

    def test_branding_changes_no_report_statistics_book_highlights_or_audit(self):
        baseline=self.wrap(self.admin,perspective='challenge');before=deepcopy(baseline.context['export_data'])
        audits=AuditEvent.objects.count()
        for source,team in [('group',''),('challenge',''),('team',str(self.team.pk)),('team',str(self.team_b.pk))]:
            page=self.wrap(self.admin,perspective='challenge',branding_source=source,branding_team=team)
            self.assertEqual(page.context['export_data']['sections'],before['sections'])
            self.assertEqual(page.context['export_data']['slides'],before['slides'])
        self.assertEqual(AuditEvent.objects.count(),audits)

    def test_partial_inheritance_and_logo_only_never_offer_banner(self):
        self.team.logo='';self.team.save(update_fields=['logo'])
        option=next(t for t in self.options(self.wrap())['teams'] if t['id']==str(self.team.pk))
        self.assertEqual(option['logo'],self.month.logo.url);self.assertEqual(option['banner'],self.team.banner.url)
        self.month.logo='';self.month.save(update_fields=['logo'])
        option=self.options(self.wrap())['teams'][0];self.assertEqual(option['logo'],self.group.logo.url)
        self.group.branding_mode='logo';self.group.save(update_fields=['branding_mode'])
        options=self.options(self.wrap())
        self.assertTrue(all(not t['banner'] for t in options['teams']))
        self.assertTrue(all(not t['banner'] for t in options['art'].values()))

    def test_multiple_challenges_same_group_and_mixed_groups(self):
        other=ChallengeMonth.objects.create(group=self.group,name='Second Challenge',status='active',starts_on=self.month.starts_on,ends_on=self.month.ends_on)
        MonthEnrollment.objects.create(month=other,participant=self.members[self.reader.pk])
        page=self.wrap(challenge='')
        self.assertEqual(page.context['export_data']['branding_selection']['branding_source'],'group')
        self.assertEqual(self.options(page)['sources'],[('northbound','Northbound Default'),('group','Group Branding')])
        page=self.wrap(self.admin,group='',challenge='',perspective='challenge')
        self.assertEqual(self.options(page)['sources'],[('northbound','Northbound Default')])
        self.assertEqual(self.options(page)['teams'],[])

    def test_historical_self_access_never_restores_identity(self):
        self.status('completed');Membership.objects.filter(pk=self.members[self.reader.pk].pk).update(is_active=False)
        page=self.wrap()
        self.assertEqual(self.options(page),{'sources':[('northbound','Northbound Default')],'art':{},'teams':[]})
        self.assertNotIn(self.group.name,json.dumps(self.options(page)))
        self.assertNotIn('/media/',json.dumps(page.context['export_data']))

    def test_completed_archived_and_revoked_team_leader(self):
        for stage in ['completed','archived']:
            self.status(stage)
            page=self.wrap(self.team_leader,perspective='team',team=self.team.pk,branding_source='team',branding_team=self.team.pk)
            self.assertEqual(page.status_code,200)
        from .models import ChallengeStaffAssignment
        ChallengeStaffAssignment.objects.filter(membership__user=self.team_leader).update(ended_at=timezone.now())
        TeamAssignment.objects.filter(participant__user=self.team_leader).update(ended_at=timezone.now())
        self.assertFalse(self.options(self.wrap(self.team_leader))['teams'])

    def test_missing_artwork_is_not_serialized_and_preserves_selected_identity(self):
        for target in [self.group,self.month,self.team]:
            target.logo='absent-logo.png';target.banner='absent-banner.png';target.save(update_fields=['logo','banner'])
        options=self.options(self.wrap(branding_source='team',branding_team=self.team.pk))
        art=next(t for t in options['teams'] if t['id']==str(self.team.pk))
        self.assertEqual((art['logo'],art['banner']),('',''))
        self.assertEqual(art['name'],self.team.name)
        self.assertNotIn('absent-',json.dumps(options))
