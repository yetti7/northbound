from unittest.mock import patch
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from . import test_team_branding as fixture
from .test_branding import upload
from .models import Team, ChallengeMonth
from .branding import community_branding, team_branding_many


class TeamBrandingLayoutTests(TestCase):
    setUp = fixture.TeamBrandingTests.setUp
    setUpFixture = fixture.TeamBrandingTests.setUpFixture
    staff = fixture.TeamBrandingTests.staff
    status = fixture.TeamBrandingTests.status
    make_book = fixture.TeamBrandingTests.make_book
    save = fixture.TeamBrandingTests.save
    branding_url = fixture.TeamBrandingTests.branding_url

    def page(self, team=None, user=None, listing=False, **query):
        self.client.force_login(user or self.reader)
        return self.client.get(reverse('team-list' if listing else 'team-detail',
            args=[self.group.slug,self.month.pk]+([] if listing else [(team or self.team).pk])),query)

    def parent_art(self):
        self.group.logo.save('group.png',upload(),save=True)
        self.group.banner.save('group-banner.png',upload(),save=True)
        self.group.branding_mode='banner';self.group.save(update_fields=['branding_mode'])
        self.month.logo.save('challenge.png',upload(),save=True)
        self.month.banner.save('challenge-banner.png',upload(),save=True)

    def test_own_art_partial_inheritance_and_group_mode_on_both_pages(self):
        self.parent_art();self.save(logo=upload());self.team.refresh_from_db()
        for page in [self.page(),self.page(listing=True)]:
            self.assertContains(page,self.team.logo.url);self.assertContains(page,self.month.banner.url)
        self.save(banner=upload());self.team.refresh_from_db()
        page=self.page();self.assertContains(page,self.team.banner.url);self.assertNotContains(page,self.month.banner.url)
        self.save(data={'logo-clear':'on'});page=self.page()
        self.assertContains(page,self.month.logo.url);self.assertContains(page,self.team.banner.url)
        self.group.branding_mode='logo';self.group.save(update_fields=['branding_mode'])
        for page in [self.page(),self.page(listing=True)]:
            self.assertNotContains(page,self.team.banner.url);self.assertNotContains(page,self.month.banner.url)

    def test_group_fallback_missing_media_and_clean_text_header(self):
        self.group.logo.save('group.png',upload(),save=True)
        self.team.logo='missing-file.png';self.team.save(update_fields=['logo'])
        for page in [self.page(),self.page(listing=True)]:
            self.assertContains(page,self.group.logo.url);self.assertNotContains(page,'missing-file.png')
        self.group.logo='';self.group.save(update_fields=['logo'])
        page=self.page();self.assertContains(page,'<h1>Team A</h1>',html=True)
        self.assertNotContains(page,'class="challenge-identity-logo"');self.assertNotContains(page,'class="challenge-identity-banner"')
        self.assertNotContains(self.page(listing=True),'team-card-identity')

    def test_cards_have_independent_art_and_keep_existing_links_order_and_counts(self):
        self.save(logo=upload());self.save(team=self.team_b,logo=upload());self.team.refresh_from_db();self.team_b.refresh_from_db()
        page=self.page(listing=True);html=page.content.decode()
        self.assertContains(page,self.team.logo.url,count=1);self.assertContains(page,self.team_b.logo.url,count=1)
        self.assertEqual([t.name for t in page.context['teams']],sorted([self.team.name,self.team_b.name]))
        self.assertContains(page,'active Reader');self.assertContains(page,'Team Leader')
        for team in [self.team,self.team_b]:self.assertContains(page,reverse('team-detail',args=[self.group.slug,self.month.pk,team.pk]))
        other=ChallengeMonth.objects.create(group=self.group,name='Different Challenge',status='active',starts_on=self.month.starts_on,ends_on=self.month.ends_on)
        twin=Team.objects.create(month=other,name=self.team.name)
        self.assertFalse(twin.logo);self.assertFalse(twin.banner)

    def test_settings_links_scopes_locked_labels_and_existing_actions(self):
        for actor in [self.admin,self.owner,self.mod,self.host,self.team_leader]:
            self.assertContains(self.page(user=actor),'Team Branding Settings')
        for actor in [self.reader,self.floater]:
            self.assertNotContains(self.page(user=actor),self.branding_url())
        self.assertNotContains(self.page(team=self.team_b,user=self.team_leader),self.branding_url(self.team_b))
        self.assertEqual(self.page(user=self.outsider).status_code,403)
        self.assertContains(self.page(user=self.admin),'>Edit Team</a>')
        self.assertNotContains(self.page(user=self.team_leader),'>Edit Team</a>')
        for stage in ['finalizing','completed','archived']:
            self.status(stage);page=self.page(user=self.admin)
            self.assertContains(page,'View Team Branding');self.assertNotContains(page,'>Edit Team</a>')
            self.client.force_login(self.admin)
            self.assertEqual(self.client.post(self.branding_url(),{'logo':upload()}).status_code,403)

    def test_score_roster_and_navigation_boundaries_unchanged(self):
        self.save(logo=upload())
        self.book.title='Permitted own-team book';self.book.save(update_fields=['title'])
        self.secret.title='Restricted other-team book';self.secret.save(update_fields=['title'])
        for page in [self.page(),self.page(listing=True)]:self.assertNotContains(page,'978321')
        page=self.page();self.assertContains(page,'Team total hidden');self.assertContains(page,'Permitted own-team book');self.assertNotContains(page,'Restricted other-team book')
        self.assertContains(page,'Back to Teams');self.assertContains(page,'Current Roster')
        self.assertContains(self.page(user=self.admin),'Team Total')
        self.assertContains(self.page(listing=True,user=self.admin),'verified pages')
        self.assertContains(self.page(listing=True),'Page totals hidden.')

    def test_long_names_archived_team_and_parent_changes_preserve_art(self):
        self.parent_art();self.save(logo=upload());self.team.refresh_from_db();own=self.team.logo.url
        self.team.name='The Fellowship of Unforgettably Long Northern Reading Adventures';self.team.is_archived=True
        self.team.save(update_fields=['name','is_archived'])
        self.month.logo.save('replacement.png',upload(),save=True)
        page=self.page();self.assertContains(page,own);self.assertContains(page,self.team.name)
        self.assertContains(self.page(listing=True,archive='1'),own)
        self.group.banner.save('replacement-banner.png',upload(),save=True)
        self.month.banner='';self.month.save(update_fields=['banner'])
        self.assertContains(self.page(),self.group.banner.url)

    def test_batch_resolution_does_not_add_queries_per_team(self):
        others=[Team.objects.create(month=self.month,name='Extra '+str(n)) for n in range(5)]
        teams=[self.team,*others]
        for team in teams:team.month=self.month
        with CaptureQueriesContext(connection) as small:team_branding_many(self.reader,teams[:1])
        with CaptureQueriesContext(connection) as many, patch('core.branding.community_branding',wraps=community_branding) as resolver:
            result=team_branding_many(self.reader,teams)
        self.assertEqual(len(small),len(many));self.assertEqual(resolver.call_count,1)
        self.assertEqual(set(result),{t.pk for t in teams})

    def test_existing_transfer_service_does_not_move_team_artwork(self):
        from .participation import assign_participant_to_team
        self.save(logo=upload(),banner=upload());self.save(team=self.team_b,logo=upload())
        before=list(Team.objects.filter(month=self.month).order_by('pk').values('id','name','logo','banner'))
        assignment,_,_=assign_participant_to_team(month=self.month,participant=self.members[self.reader.pk],team=self.team_b,actor=self.admin)
        self.assertEqual(assignment.team_id,self.team_b.pk)
        self.assertEqual(before,list(Team.objects.filter(month=self.month).order_by('pk').values('id','name','logo','banner')))

    def test_card_color_indicator_is_inside_name_and_border_is_preserved(self):
        page=self.page(listing=True)
        self.assertContains(page,'class="team-card-name"')
        self.assertContains(page,'<h2 class="team-card-name"><span class="team-swatch" style="background:'+self.team.color+'" aria-hidden="true"></span><span>'+self.team.name+'</span></h2>',html=True)
        self.assertContains(page,'style="--team-color:'+self.team.color+'"')
