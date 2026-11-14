import os
from pathlib import Path
from django.test import TestCase
from django.urls import reverse
from . import test_branding as fixture
from .branding import update_branding


class BrandingUIConsistencyTests(TestCase):
    setUp = fixture.BrandingTests.setUp
    setUpFixture = fixture.BrandingTests.setUpFixture
    make_book = fixture.BrandingTests.make_book
    status = fixture.BrandingTests.status

    def page(self, name, *args):
        self.client.force_login(self.admin)
        return self.client.get(reverse(name,args=args))

    def test_challenge_and_team_cards_inheritance_and_removal(self):
        update_branding(actor=self.admin,target=self.group,data={'branding_mode':'logo'},files={'logo':fixture.upload('group-logo.png'),'banner':fixture.upload('group-banner.png')})
        for name,args in [('challenge-branding',[self.group.slug,self.month.pk]),('team-branding',[self.group.slug,self.month.pk,self.team.pk])]:
            response=self.page(name,*args)
            self.assertContains(response,'group-branding-edit-grid')
            self.assertContains(response,'class="card group-branding-image"',count=2)
            self.assertContains(response,'Inherited from Group')
            self.assertNotContains(response,'name="logo-clear"')
            self.assertNotContains(response,'name="banner-clear"')
            self.assertNotContains(response,'Stored artwork preview')
            self.assertContains(response,'name="logo"');self.assertContains(response,'name="banner"')
        update_branding(actor=self.admin,target=self.month,data={},files={'logo':fixture.upload('challenge.png')})
        response=self.page('team-branding',self.group.slug,self.month.pk,self.team.pk)
        self.assertContains(response,'Inherited from Challenge');self.assertContains(response,'Inherited from Group')
        update_branding(actor=self.admin,target=self.team,data={},files={'logo':fixture.upload('team.png')})
        response=self.page('team-branding',self.group.slug,self.month.pk,self.team.pk)
        self.assertContains(response,'Stored Team artwork');self.assertContains(response,'name="logo-clear"')
        self.group.refresh_from_db();self.month.refresh_from_db()
        before=(self.group.logo.name,self.group.banner.name,self.month.logo.name)
        response=self.client.post(reverse('team-branding',args=[self.group.slug,self.month.pk,self.team.pk]),{'logo-clear':'on'})
        self.assertEqual(response.status_code,302);self.team.refresh_from_db();self.assertFalse(self.team.logo)
        self.group.refresh_from_db();self.month.refresh_from_db()
        self.assertEqual((self.group.logo.name,self.group.banner.name,self.month.logo.name),before)

    def test_team_edit_navigation_and_existing_controls(self):
        response=self.page('team-edit',self.group.slug,self.month.pk,self.team.pk)
        self.assertContains(response,'Team Branding Settings')
        self.assertContains(response,reverse('team-branding',args=[self.group.slug,self.month.pk,self.team.pk]))
        for label in ['Save Team','Archive Team','name="name"','name="color"']:self.assertContains(response,label)
        self.assertNotContains(response,'type="file"')

    def test_locked_team_cards_read_only_and_no_edit_navigation(self):
        self.status('completed')
        response=self.page('team-branding',self.group.slug,self.month.pk,self.team.pk)
        self.assertContains(response,'Read-only');self.assertContains(response,'group-branding-edit-grid')
        self.assertNotContains(response,'type="file"');self.assertNotContains(response,'Save Branding')

    def test_group_reference_unchanged(self):
        response=self.page('group-branding',self.group.slug)
        self.assertContains(response,'data-group-branding-form');self.assertContains(response,'Header preview')
        self.assertContains(response,'group-branding-edit-grid')

    def test_submission_heading_badge_and_link(self):
        response=self.page('month-detail',self.group.slug,self.month.pk)
        self.assertContains(response,'<th>Submission</th>',html=True)
        self.assertContains(response,'<span class="pill">BOOK</span>',html=True)
        self.assertContains(response,self.book.title)
        self.assertContains(response,reverse('submission-review',args=[self.group.slug,self.month.pk,self.book.pk]))
        self.assertNotContains(response,'<th>Book</th>',html=True)

    def test_optional_responsive_fixtures(self):
        directory=os.environ.get('NB_BRANDING_UI_FIXTURES')
        if not directory:return
        import shutil
        root=Path(directory);root.mkdir(parents=True,exist_ok=True)
        update_branding(actor=self.admin,target=self.group,data={'branding_mode':'banner'},files={'logo':fixture.upload('group.png'),'banner':fixture.upload('banner.png')})
        for name,args,label in [('group-branding',[self.group.slug],'group'),('challenge-branding',[self.group.slug,self.month.pk],'challenge'),('team-branding',[self.group.slug,self.month.pk,self.team.pk],'team')]:
            response=self.page(name,*args);self.assertEqual(response.status_code,200)
            (root/(label+'.html')).write_bytes(response.content)
        from django.conf import settings
        shutil.copytree(settings.MEDIA_ROOT,root/'media',dirs_exist_ok=True)
