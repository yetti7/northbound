from types import SimpleNamespace
from django import forms
from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import TestCase, RequestFactory, override_settings
from django.urls import reverse,resolve
from .test_navigation_foundation import NavigationFoundationTests
from .test_logical_navigation import LogicalNavigationTests
from .context_processors import logical_navigation
from .models import Membership,AuditEvent,RecoveryOperation,BookSubmission

@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class SettingsFormNavigationTests(TestCase):
    setUp=LogicalNavigationTests.setUp
    nav=NavigationFoundationTests.nav
    month_kwargs=NavigationFoundationTests.month_kwargs
    def test_group_subsections_use_authorized_settings_hub(self):
        for name in ['group-branding','group-discord-notifications','group-access-code','group-hardcover-connection']:
            result=self.nav(name,{'group_slug':self.group.slug})
            self.assertEqual(result['logical_parent_url'],reverse('group-edit',args=[self.group.slug]))
            self.assertEqual(result['logical_parent_label'],'Back to Group Settings')
            result=self.nav(name,{'group_slug':self.group.slug},user=self.reader_user)
            self.assertEqual(result['logical_parent_url'],reverse('group-detail',args=[self.group.slug]))
        outsider=get_user_model().objects.create_user('nav3-outsider')
        self.assertIsNone(self.nav('group-branding',{'group_slug':self.group.slug},user=outsider)['logical_parent_url'])
        self.assertEqual(self.client.get(reverse('group-edit',args=[self.group.slug])).context['logical_parent_label'],'Back to Group')
    def test_challenge_subsections_keep_settings_parent_and_boundaries(self):
        for name,extra in [('challenge-branding',{}),('challenge-schedule-settings',{}),('challenge-prompt-settings',{'kind':'bingo'})]:
            result=self.nav(name,self.month_kwargs(**extra))
            self.assertEqual(result['logical_parent_url'],reverse('challenge-settings',kwargs=self.month_kwargs()))
            self.assertEqual(result['logical_parent_label'],'Back to Challenge Settings')
        self.assertIsNone(self.nav('challenge-branding',{'group_slug':'foreign','month_pk':self.month.pk})['logical_parent_url'])
    def test_generic_form_cancel_is_link_and_errors_preserve_destination(self):
        url=reverse('team-create',kwargs=self.month_kwargs())
        parent=reverse('team-list',kwargs=self.month_kwargs())
        for response in [self.client.get(url),self.client.post(url,{})]:
            self.assertEqual(response.status_code,200)
            self.assertContains(response,'type="submit"')
            self.assertContains(response,'<a class="button secondary" href="'+parent+'">Cancel</a>',html=True)
            self.assertContains(response,'class="back-button contextual-back"',count=1)
            self.assertNotContains(response,'name="cancel"')
        form=forms.Form()
        html=render_to_string('core/form_page.html',{'form':form})
        self.assertNotIn('>Cancel</a>',html)
    def test_branding_has_one_shared_return_and_no_local_duplicate(self):
        page=self.client.get(reverse('group-branding',args=[self.group.slug]))
        self.assertEqual(page.status_code,200)
        self.assertContains(page,'class="back-button contextual-back"',count=1)
        self.assertNotContains(page,'group-branding-back')
        self.assertContains(page,'Back to Group Settings')
    def test_create_and_platform_settings_forms_offer_authorized_cancel(self):
        root=get_user_model().objects.create_superuser('navigation-settings-root','settings@example.test','test-password')
        self.client.force_login(root)
        for route,kwargs,parent in [('group-create',None,reverse('dashboard')),('month-create',{'group_slug':self.group.slug},reverse('month-list',args=[self.group.slug])),('platform-general-settings',None,reverse('config-dashboard'))]:
            response=self.client.get(reverse(route,kwargs=kwargs))
            self.assertEqual(response.status_code,200)
            self.assertContains(response,'<a class="button secondary" href="'+parent+'">Cancel</a>',html=True)

    def test_manage_timestamp_mobile_rule_is_scoped_and_preserves_format(self):
        from pathlib import Path
        from django.conf import settings
        css=(Path(settings.BASE_DIR)/'static/css/theme.css').read_text()
        self.assertIn('@media(max-width:600px){.submission-manage .submission-timeline .details{grid-template-columns:minmax(0,1fr);gap:4px}',css)
        template=(Path(settings.BASE_DIR)/'templates/core/includes/submission_timeline.html').read_text()
        self.assertIn('F j, Y · g:i A',template)
        self.assertNotIn('white-space:nowrap',css[css.index('/* Manage Submission timeline:'):])

    def test_existing_save_redirect_unchanged(self):
        page=self.client.post(reverse('team-create',kwargs=self.month_kwargs()),{'name':'Navigation created','color':'#123456'})
        self.assertRedirects(page,reverse('team-list',kwargs=self.month_kwargs()),fetch_redirect_response=False)

@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],NORTHBOUND_HARDCOVER_WRITEBACK_ENABLED=False)
class RecoveryNavigationCleanupTests(TestCase):
    def setUp(self):
        from .test_recovery_fixtures import create_protected_recovery_graph
        self.root=get_user_model().objects.create_superuser('nav3-root','nav3@example.test','test-password')
        create_protected_recovery_graph(self.root)
        self.client.force_login(self.root)
    def nav(self,name,pk,user=None):
        q=RequestFactory().get(reverse(name,kwargs={'pk':pk}),{'next':'https://evil.example/'})
        q.user=user or self.root;q.resolver_match=resolve(q.path)
        return logical_navigation(q)
    def test_specialized_forms_prefer_actual_object_details_without_writes(self):
        from .models import MonthTheme,ThemeClaim,BotmBook,BotmMatch,PersonalTBRBook,PersonalTBR,PersonalTBRMatch,GameRewardApplication
        cases=[('recovery-theme-correct',MonthTheme.objects.first(),'recovery-theme-detail',lambda o:o.pk),
            ('recovery-theme-claim',ThemeClaim.objects.first(),'recovery-submission-detail',lambda o:o.submission_id),
            ('recovery-botm-book-correct',BotmBook.objects.first(),'recovery-botm-book-detail',lambda o:o.pk),
            ('recovery-botm-match',BotmMatch.objects.first(),'recovery-botm-book-detail',lambda o:o.botm_book_id),
            ('recovery-tbr-entry',PersonalTBRBook.objects.first(),'recovery-tbr-detail',lambda o:o.personal_tbr_id),
            ('recovery-tbr-rebuild',PersonalTBR.objects.first(),'recovery-tbr-detail',lambda o:o.pk),
            ('recovery-tbr-match',PersonalTBRMatch.objects.first(),'recovery-tbr-detail',lambda o:o.personal_tbr_book.personal_tbr_id),
            ('recovery-game-recreate',GameRewardApplication.objects.first(),'recovery-game-detail',lambda o:o.game_id)]
        before=(AuditEvent.objects.count(),RecoveryOperation.objects.count(),list(BookSubmission.objects.values()))
        for name,obj,target,identity in cases:
            self.assertIsNotNone(obj)
            expected=reverse(target,kwargs={'pk':identity(obj)})
            self.assertEqual(self.nav(name,obj.pk)['logical_parent_url'],expected)
        self.assertEqual(before,(AuditEvent.objects.count(),RecoveryOperation.objects.count(),list(BookSubmission.objects.values())))
    def test_missing_object_falls_back_and_nonroot_sees_no_recovery_parent(self):
        self.assertEqual(self.nav('recovery-tbr-entry',999999)['logical_parent_url'],reverse('recovery-tbr-list'))
        reader=get_user_model().objects.get(username='purge-reader')
        self.assertIsNone(self.nav('recovery-tbr-entry',999999,user=reader)['logical_parent_url'])
        self.assertEqual(self.client.get(reverse('recovery-theme-correct',args=[999999])).status_code,404)
        self.client.force_login(reader)
        self.assertEqual(self.client.get(reverse('recovery-theme-correct',args=[999999])).status_code,403)
