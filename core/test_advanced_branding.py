"""Advanced branding stays opt-in, independently resolved, authorized and audited."""
from unittest.mock import patch
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from . import test_branding as fixture
from .branding import (BrandingForm, ChallengeBrandingForm, TeamBrandingForm,
    update_branding, community_branding, team_branding, team_branding_many)
from .models import AuditEvent, ReadingGroup, ChallengeMonth, Team


class AdvancedBrandingTests(TestCase):
    setUp = fixture.BrandingTests.setUp
    setUpFixture = fixture.BrandingTests.setUpFixture
    make_book = fixture.BrandingTests.make_book
    save = fixture.BrandingTests.save

    def parents(self):
        self.save(logo=fixture.upload(), banner=fixture.upload());self.group.refresh_from_db()
        self.save(target=self.month, logo=fixture.upload(), banner=fixture.upload());self.month.refresh_from_db()
        self.group.branding_mode='banner';self.group.save(update_fields=['branding_mode'])
        self.month.branding_mode='banner';self.month.save(update_fields=['branding_mode'])
        self.team.branding_mode='banner';self.team.save(update_fields=['branding_mode'])
        self.month.group=self.group;self.team.month=self.month

    def page(self, route, *args):
        self.client.force_login(self.admin)
        return self.client.get(reverse(route, args=[self.group.slug, *args]))

    def change(self,target,**data):
        data={'gradient_coverage':target.gradient_coverage,'gradient_darkness':target.gradient_darkness,**data}
        if isinstance(target,ReadingGroup):data['branding_mode']=target.branding_mode
        return update_branding(actor=self.admin,target=target,data=data,files={})

    def test_new_and_existing_records_have_legacy_defaults(self):
        self.parents()
        for target in (self.group,self.month,self.team):
            self.assertFalse(target.gradient_enabled)
            self.assertEqual((target.gradient_coverage,target.gradient_darkness),(75,85))
        self.assertFalse(self.team.hide_inherited_logo)
        for route,args in [('group-detail',()),('month-detail',(self.month.pk,)),('team-detail',(self.month.pk,self.team.pk)),('team-list',(self.month.pk,))]:
            response=self.page(route,*args)
            self.assertNotContains(response,'branding-custom-gradient');self.assertNotContains(response,'is-artwork-showcase')

    def test_logo_suppression_custom_override_missing_artwork_and_export_opt_out(self):
        self.parents()
        self.assertEqual(team_branding(self.reader,self.team)['effective_logo'],self.month.logo.url)
        self.team.hide_inherited_logo=True;self.team.branding_legacy_visibility=True;self.team.save(update_fields=['hide_inherited_logo','branding_legacy_visibility'])
        self.assertEqual(team_branding(self.reader,self.team)['effective_logo'],'')
        self.assertEqual(team_branding_many(self.reader,[self.team])[self.team.pk]['effective_logo'],'')
        self.assertEqual(team_branding(self.reader,self.team,presentation=False)['effective_logo'],self.month.logo.url)
        self.assertEqual(team_branding_many(self.reader,[self.team],presentation=False)[self.team.pk]['effective_logo'],self.month.logo.url)
        self.team.logo.save('own.png',fixture.upload(),save=True)
        self.assertEqual(team_branding(self.reader,self.team)['effective_logo'],self.team.logo.url)
        self.team.logo='missing';self.team.banner='missing'
        self.assertEqual(team_branding(self.reader,self.team)['effective_logo'],'')
        self.team.hide_inherited_logo=False
        self.assertEqual(team_branding(self.reader,self.team)['effective_logo'],self.month.logo.url)
        self.month.logo='missing';self.group.logo='missing'
        self.assertEqual(team_branding(self.reader,self.team)['effective_logo'],'')

    def test_independent_appearance_with_inherited_banner_and_previews(self):
        self.parents()
        settings=[(self.group,10,20),(self.month,40,50),(self.team,70,80)]
        for target,coverage,darkness in settings:
            form,saved=self.change(target,gradient_enabled=True,gradient_coverage=coverage,gradient_darkness=darkness)
            self.assertTrue(saved,form.errors);target.refresh_from_db()
        self.month.group=self.group;self.team.month=self.month
        art=team_branding(self.admin,self.team)
        self.assertEqual(art['effective_banner'],self.month.banner.url)
        self.assertEqual(art['appearance']['style'],'--branding-coverage:70%;--branding-darkness:0.80')
        routes=[('group-branding',(),10,20),('challenge-branding',(self.month.pk,),40,50),('team-branding',(self.month.pk,self.team.pk),70,80)]
        for route,args,coverage,darkness in routes:
            response=self.page(route,*args)
            self.assertContains(response,f'--branding-coverage:{coverage}%;--branding-darkness:{darkness/100:.2f}')
            self.assertContains(response,'Header preview');self.assertContains(response,'Banner Appearance')
        self.assertEqual(community_branding(self.reader,self.group,self.month)['group_appearance']['style'],'--branding-coverage:10%;--branding-darkness:0.20')

    def test_slider_boundaries_validation_and_disabled_values_ignored(self):
        for target,formclass in [(self.group,BrandingForm),(self.month,ChallengeBrandingForm),(self.team,TeamBrandingForm)]:
            for value in (0,100):
                data={'branding_mode':'logo','gradient_enabled':'on','gradient_coverage':value,'gradient_darkness':value}
                form=formclass(data,instance=target);self.assertTrue(form.is_valid(),form.errors)
            for field in ('gradient_coverage','gradient_darkness'):
                for value in (-1,101,'invalid'):
                    data={'branding_mode':'logo','gradient_coverage':50,'gradient_darkness':50,field:value}
                    form=formclass(data,instance=target);self.assertFalse(form.is_valid());self.assertIn(field,form.errors)
                obj=type(target)(**{field:101})
                with self.assertRaises(ValidationError):obj._meta.get_field(field).clean(101,obj)
        self.parents();self.team.gradient_coverage=0;self.team.gradient_darkness=0
        self.assertEqual(team_branding(self.reader,self.team)['appearance'],{})

    def test_showcase_requires_custom_available_banner_suppression_and_no_custom_logo(self):
        self.parents();self.team.hide_inherited_logo=True;self.team.branding_legacy_visibility=True
        self.assertFalse(team_branding(self.reader,self.team)['showcase'])
        self.team.banner.save('own-banner.png',fixture.upload(),save=True)
        self.assertTrue(team_branding(self.reader,self.team)['showcase'])
        self.team.gradient_enabled=True;self.team.gradient_coverage=30;self.team.gradient_darkness=65
        self.team.save(update_fields=['hide_inherited_logo','branding_legacy_visibility','gradient_enabled','gradient_coverage','gradient_darkness'])
        cards=self.page('team-list',self.month.pk)
        self.assertContains(cards,'has-banner is-artwork-showcase')
        self.assertNotContains(cards,'--branding-coverage:30%')
        detail=self.page('team-detail',self.month.pk,self.team.pk)
        self.assertContains(detail,'branding-custom-gradient is-artwork-showcase')
        self.assertContains(detail,'--branding-coverage:30%;--branding-darkness:0.65')
        self.assertContains(self.page('team-branding',self.month.pk,self.team.pk),'branding-custom-gradient is-artwork-showcase')
        self.team.logo.save('own.png',fixture.upload(),save=True)
        self.assertFalse(team_branding(self.reader,self.team)['showcase'])
        self.team.logo='';self.team.hide_inherited_logo=False
        self.assertFalse(team_branding(self.reader,self.team)['showcase'])
        self.team.hide_inherited_logo=True;self.team.banner='missing'
        self.assertFalse(team_branding(self.reader,self.team)['showcase'])
        self.team.banner='';self.group.branding_mode='logo'
        self.assertEqual(team_branding(self.reader,self.team)['effective_banner'],self.month.banner.url)

    def test_restore_defaults_only_appearance_and_audit_and_permissions(self):
        self.parents();self.team.hide_inherited_logo=True;self.team.branding_legacy_visibility=True;self.team.branding_legacy_visibility=True;self.team.save(update_fields=['hide_inherited_logo','branding_legacy_visibility'])
        paths=(self.group.logo.name,self.month.logo.name,self.team.logo.name,self.team.banner.name)
        first=AuditEvent.objects.order_by('pk').first();historical=first.summary
        form,saved=self.change(self.team,hide_inherited_logo='on',gradient_enabled='on',gradient_coverage=20,gradient_darkness=30)
        self.assertTrue(saved,form.errors)
        event=AuditEvent.objects.latest('pk')
        self.assertIn('gradient_enabled',event.summary);self.assertIn('branding_mode',event.summary)
        self.assertIn('"before"',event.summary);self.assertIn('"after"',event.summary)
        form,saved=self.change(self.team,hide_inherited_logo='on',gradient_coverage=75,gradient_darkness=85)
        self.assertTrue(saved,form.errors);self.team.refresh_from_db()
        self.assertFalse(self.team.gradient_enabled);self.assertTrue(self.team.hide_inherited_logo)
        self.assertEqual((self.team.gradient_coverage,self.team.gradient_darkness),(75,85))
        self.assertEqual(paths,(self.group.logo.name,self.month.logo.name,self.team.logo.name,self.team.banner.name))
        first.refresh_from_db();self.assertEqual(first.summary,historical)
        before=AuditEvent.objects.count()
        for target in (self.group,self.month,self.team):
            with self.assertRaises(PermissionDenied):
                update_branding(actor=self.reader,target=target,data={'gradient_enabled':'on','hide_inherited_logo':'on'},files={})
        self.assertEqual(AuditEvent.objects.count(),before)
