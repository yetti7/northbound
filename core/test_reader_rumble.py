import json
import tempfile
from pathlib import Path
from datetime import date
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied,ValidationError
from django.core.files.base import ContentFile
from django.db import connection
from django.test import TestCase,override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from . import test_team_planning_foundation as fixture
from .models import (Membership,MonthEnrollment,TeamAssignment,Team,ChallengeStaffAssignment,
    ModifierProvenance,BookSubmission,AuditEvent,ReadingGroup,ChallengeMonth)
from .reader_rumble import build_reader_rumble,can_generate_reader_rumble


class ReaderRumbleTests(TestCase):
    setUpFixture=fixture.TeamPlanningFoundationTests.setUp
    member=fixture.TeamPlanningFoundationTests.member
    book=fixture.TeamPlanningFoundationTests.book
    historical_book=fixture.TeamPlanningFoundationTests.historical_book

    def setUp(self):
        self.setUpFixture()
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        override=override_settings(MEDIA_ROOT=self.directory.name);override.enable();self.addCleanup(override.disable)
        self.url=reverse('reader-rumble',args=[self.group.slug,self.month.pk,self.team.pk])
        self.other_team=Team.objects.create(month=self.month,name='Other Team')

    def data(self,actor=None,**options):
        return build_reader_rumble(actor=actor or self.host_user,team=self.team,**options)

    def more_reader(self,name,pages=0):
        user=get_user_model().objects.create_user(name)
        member=Membership.objects.create(group=self.group,user=user)
        MonthEnrollment.objects.create(month=self.month,participant=member)
        TeamAssignment.objects.create(month=self.month,participant=member,team=self.team)
        if pages:self.book(member,name,pages,0)
        return member

    def test_default_base_and_bonus_mode_changes_ranking_without_role_priority(self):
        base=self.data();bonus=self.data(bonuses=True)
        self.assertEqual(base['metric'],'Base Pages');self.assertEqual(base['top'],20)
        self.assertEqual([r['value'] for r in base['rows']],[200,100,50])
        self.assertEqual([r['value'] for r in bonus['rows']],[200,120,110])
        self.assertEqual(bonus['metric'],'Total Challenge Points')
        ModifierProvenance.objects.create(month=self.month,participant=self.reader_b,source_type='game_reward',source_reference='rank-switch',source_label='Fixture',amount=200,effective_date=date(2026,9,15))
        self.assertEqual(self.data(bonuses=True)['rows'][0]['username'],self.reader_b_user.username)
        self.assertEqual(self.data()['rows'][0]['username'],self.leader_a_user.username)

    def test_limits_rank_all_readers_first_and_include_zero_values_without_placeholders(self):
        self.more_reader('rank-last-created-highest',1000)
        for i in range(25):self.more_reader(f'zero-{i:02d}')
        for limit in (5,10,15,20,25):
            data=self.data(top=limit,include_zero=True)
            self.assertEqual(len(data['rows']),limit)
            self.assertEqual(data['rows'][0]['username'],'rank-last-created-highest')
            self.assertEqual([r['rank'] for r in data['rows']],list(range(1,limit+1)))
            if limit>5:self.assertEqual(data['rows'][-1]['value'],0)
        for bad in [0,6,26,'20',True]:
            with self.assertRaises(ValidationError):self.data(top=bad)

    def test_fewer_readers_and_case_insensitive_stable_ties(self):
        self.more_reader('alpha');self.more_reader('Alpha')
        for limit in (5,10,15,20,25):self.assertEqual(len(self.data(top=limit,include_zero=True)['rows']),min(limit,6))
        zeroes=[r['username'] for r in self.data(include_zero=True)['rows'] if r['value']==0]
        self.assertEqual(zeroes,['alpha','Alpha','hierarchy-leader-z'])
        empty=build_reader_rumble(actor=self.host_user,team=self.other_team)
        self.assertEqual(empty['rows'],[])

    def test_all_modifier_categories_included_and_ineligible_credit_excluded(self):
        base=self.data(bonuses=True)['rows'][0]['value']
        for index,kind in enumerate(ModifierProvenance.SourceType.values):
            ModifierProvenance.objects.create(month=self.month,participant=self.leader_a,source_type=kind,
                source_reference=f'all:{index}',source_label='Fixture',amount=3,effective_date=date(2026,9,12))
        ModifierProvenance.objects.create(month=self.month,participant=self.leader_a,source_type='legacy_modifier',source_reference='inactive',source_label='Fixture',amount=777,is_active=False,effective_date=date(2026,9,12))
        pending=self.book(self.leader_a,'not approved',50,999);pending.status='pending';pending.save(update_fields=['status'])
        removed=self.book(self.leader_a,'removed',50,999);BookSubmission.objects.filter(pk=removed.pk).update(is_removed=True)
        self.assertEqual(self.data(bonuses=True)['rows'][0]['value'],base+3*len(ModifierProvenance.SourceType.values))
        self.assertEqual(self.data()['rows'][0]['value'],200)

    def test_transfer_departure_and_inactive_enrollment_excluded(self):
        assignment=TeamAssignment.objects.get(month=self.month,participant=self.reader)
        assignment.ended_at=timezone.now();assignment.save(update_fields=['ended_at'])
        TeamAssignment.objects.create(month=self.month,participant=self.reader,team=self.other_team)
        self.assertNotIn(self.reader_user.username,[r['username'] for r in self.data()['rows']])
        transferred=build_reader_rumble(actor=self.host_user,team=self.other_team,bonuses=True)
        self.assertEqual(transferred['rows'][0]['value'],120)
        MonthEnrollment.objects.filter(month=self.month,participant=self.reader_b).update(is_active=False,credited_team=self.team)
        self.assertNotIn(self.reader_b_user.username,[r['username'] for r in self.data()['rows']])
        from .score_aggregation import challenge_team_totals
        self.assertEqual(challenge_team_totals(month=self.month,team_ids=[self.team.pk])[self.team.pk],310)

    def test_staff_scope_and_individual_scores_required_even_with_public_standings(self):
        for actor in [self.platform_owner,self.owner_user,self.host_user,self.floater_user,self.leader_a_user]:
            self.assertTrue(can_generate_reader_rumble(actor,self.team))
        self.assertFalse(can_generate_reader_rumble(self.reader_user,self.team))
        self.assertFalse(can_generate_reader_rumble(self.leader_a_user,self.other_team))
        self.month.reader_scores_visibility='hosts';self.month.save(update_fields=['reader_scores_visibility'])
        self.assertFalse(can_generate_reader_rumble(self.leader_a_user,self.team))
        self.assertFalse(can_generate_reader_rumble(self.floater_user,self.team))
        with self.assertRaises(PermissionDenied):self.data(actor=self.leader_a_user)
        self.client.force_login(self.leader_a_user)
        self.assertEqual(self.client.get(self.url).status_code,403)
        self.assertNotContains(self.client.get(self.team_url),'Reader Rumble')
        # Public score visibility does not give participating Hosts cross-team staff export scope.
        self.month.reader_scores_visibility='everybody';self.month.save(update_fields=['reader_scores_visibility'])
        MonthEnrollment.objects.create(month=self.month,participant=self.host)
        TeamAssignment.objects.create(month=self.month,participant=self.host,team=self.team)
        self.assertFalse(can_generate_reader_rumble(self.host_user,self.other_team))

    def test_endpoint_group_isolation_privacy_no_writes_and_private_cache(self):
        self.client.force_login(self.host_user)
        before=(list(BookSubmission.objects.values()),list(ModifierProvenance.objects.values()),list(TeamAssignment.objects.values()),AuditEvent.objects.count())
        response=self.client.get(self.url);self.assertEqual(response.status_code,200)
        payload=response.context['rumble'];self.assertEqual(payload['timezone'],self.group.timezone)
        self.assertEqual(set(payload['rows'][0]),{'rank','username','value'})
        body=response.content.decode()
        for secret in ['private-reader','private-leader','hardcover','Discord Message','approved_pages']:
            self.assertNotIn(secret,body)
        self.assertContains(response,'Download PNG');self.assertNotContains(response,'name="bonuses" checked')
        self.assertIn('no-store',response['Cache-Control'])
        self.assertContains(response,'Back to Team')
        self.assertEqual(self.client.get(self.url,{'top':99}).status_code,400)
        self.assertEqual(self.client.post(self.url,{}).status_code,405)
        wrong=reverse('reader-rumble',args=['wrong-group',self.month.pk,self.team.pk])
        self.assertEqual(self.client.get(wrong).status_code,404)
        self.assertEqual((list(BookSubmission.objects.values()),list(ModifierProvenance.objects.values()),list(TeamAssignment.objects.values()),AuditEvent.objects.count()),before)
        self.client.logout();self.assertEqual(self.client.get(self.url).status_code,302)

    def test_branding_per_asset_inheritance_missing_art_and_bounded_queries(self):
        self.assertEqual((self.data()['logo'],self.data()['banner']),('',''))
        for obj,name in [(self.group,'group'),(self.month,'challenge'),(self.team,'team')]:
            obj.logo.save(name+'.png',ContentFile(b'fixture'),save=True)
            obj.banner.save(name+'-banner.png',ContentFile(b'fixture'),save=True)
            self.group.branding_mode='banner';self.group.save(update_fields=['branding_mode'])
            data=self.data();self.assertEqual(data['logo'],obj.logo.url);self.assertEqual(data['banner'],obj.banner.url)
        self.group.branding_mode='logo';self.group.save(update_fields=['branding_mode'])
        self.assertEqual(self.data()['banner'],'')
        Path(self.directory.name,self.team.logo.name).unlink()
        self.assertEqual(self.data()['logo'],self.month.logo.url)
        with CaptureQueriesContext(connection) as queries:self.data(actor=self.platform_owner)
        count=len(queries)
        for i in range(26):self.more_reader('query-'+str(i))
        with CaptureQueriesContext(connection) as queries:self.data(actor=self.platform_owner)
        self.assertLessEqual(len(queries),count+1)

    def test_foreign_group_inactive_actor_and_overlapping_host_owner_scope(self):
        user=get_user_model().objects.create_user('foreign-owner')
        foreign=ReadingGroup.objects.create(name='Foreign',slug='foreign-rumble')
        Membership.objects.create(group=foreign,user=user,role='owner')
        for actor in [user,get_user_model().objects.create_user('inactive-rumble',is_active=False,is_superuser=True)]:
            with self.assertRaises(PermissionDenied):self.data(actor=actor)
        self.host.role='owner';self.host.save(update_fields=['role'])
        MonthEnrollment.objects.create(month=self.month,participant=self.host)
        TeamAssignment.objects.create(month=self.month,participant=self.host,team=self.team)
        with self.assertRaises(PermissionDenied):build_reader_rumble(actor=self.host_user,team=self.other_team)
        self.owner.role='moderator';self.owner.save(update_fields=['role'])
        self.assertTrue(can_generate_reader_rumble(self.owner_user,self.team))

    def test_full_challenge_totals_ignore_team_page_date_and_search_parameters(self):
        self.client.force_login(self.host_user)
        response=self.client.get(self.url,{'top':'25','from':'2026-09-29','to':'2026-09-30','search':'nobody'})
        self.assertEqual([r['value'] for r in response.context['rumble']['rows']],[200,100,50])
        self.assertEqual(response.context['rumble']['metric'],'Base Pages')

    def test_zero_checkbox_defaults_and_metric_specific_bonus_only_reader(self):
        self.client.force_login(self.host_user)
        response=self.client.get(self.url)
        self.assertFalse(response.context['form'].cleaned_data['bonuses'])
        self.assertFalse(response.context['form'].cleaned_data['include_zero'])
        self.assertContains(response,'Include Zero-Score Readers')
        self.assertLess(response.content.index(b'Include Bonuses'),response.content.index(b'Include Zero-Score Readers'))
        self.assertNotIn(self.leader_z_user.username,[r['username'] for r in response.context['rumble']['rows']])
        ModifierProvenance.objects.create(month=self.month,participant=self.leader_z,source_type='game_reward',source_reference='bonus-only-zero-test',source_label='Fixture',amount=100,effective_date=date(2026,9,15))
        self.assertNotIn(self.leader_z_user.username,[r['username'] for r in self.data()['rows']])
        bonus=self.data(bonuses=True)['rows']
        self.assertEqual(next(r['value'] for r in bonus if r['username']==self.leader_z_user.username),100)
        self.assertEqual([r['value'] for r in bonus],[200,120,110,100])
        included=self.data(include_zero=True)['rows']
        self.assertEqual(included[-1],{'rank':4,'username':self.leader_z_user.username,'value':0})

    def test_zero_filter_precedes_limit_and_all_top_sizes_preserve_ranking(self):
        # Twenty-two eligible Readers, seventeen nonzero Base totals.
        for i in range(18):self.more_reader(f'filter-reader-{i:02d}',10 if i<14 else 0)
        self.assertEqual(len(self.data(top=20)['rows']),17)
        self.assertEqual(len(self.data(top=20,include_zero=True)['rows']),20)
        for top in (5,10,15,20,25):
            filtered=self.data(top=top)['rows'];included=self.data(top=top,include_zero=True)['rows']
            self.assertEqual(len(filtered),min(top,17));self.assertEqual(len(included),min(top,22))
            self.assertEqual(filtered,included[:len(filtered)])
            self.assertTrue(all(row['value']!=0 for row in filtered))
            self.assertEqual([row['rank'] for row in filtered],list(range(1,len(filtered)+1)))
        self.assertEqual(self.data(top=25,include_zero=True)['rows'][-1]['value'],0)
        with self.assertRaises(ValidationError):self.data(include_zero='yes')

    def test_empty_result_has_no_png_and_accessible_rows_match_payload(self):
        self.client.force_login(self.host_user)
        for include in [False,True]:
            response=self.client.get(self.url,{'top':20,**({'include_zero':'on'} if include else {})})
            rows=response.context['rumble']['rows']
            import re,html
            accessible=[(html.unescape(name),int(value)) for name,value in re.findall(r'<li><span>(.*?)</span><strong>(\d+)</strong></li>',response.content.decode())]
            self.assertEqual(accessible,[(row['username'],row['value']) for row in rows])
        BookSubmission.objects.filter(month=self.month).update(status='pending')
        response=self.client.get(self.url)
        self.assertEqual(response.status_code,200);self.assertEqual(response.context['rumble']['rows'],[])
        self.assertContains(response,'No Readers match these settings.')
        self.assertNotContains(response,'Download PNG');self.assertNotContains(response,'id="reader-rumble-data"')
        self.assertContains(response,'data-rumble-controls')
        included=self.client.get(self.url,{'top':20,'include_zero':'on'})
        self.assertEqual(len(included.context['rumble']['rows']),4)
        self.assertTrue(all(row['value']==0 for row in included.context['rumble']['rows']))
        self.assertContains(included,'Download PNG')
