from types import SimpleNamespace
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import TestCase,RequestFactory,override_settings
from django.template.loader import render_to_string
from django.urls import reverse,resolve
from . import test_logical_navigation as fixture
from .context_processors import logical_navigation,logical_parent_label,NAVIGATION_PARENTS,NAVIGATION_EXCEPTIONS
from .models import ReadingGroup,ChallengeMonth,Team,AuditEvent


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class NavigationFoundationTests(TestCase):
    setUp=fixture.LogicalNavigationTests.setUp
    assert_logical_back=fixture.LogicalNavigationTests.assert_logical_back

    def nav(self,name,kwargs=None,user=None,query=None,**attributes):
        request=RequestFactory().get(reverse(name,kwargs=kwargs),query or {})
        request.user=user or self.owner_user;request.resolver_match=resolve(request.path)
        for key,value in attributes.items():setattr(request,key,value)
        result=logical_navigation(request)
        self.assertEqual(logical_parent_label(request),result['logical_parent_label'])
        return result
    def month_kwargs(self,**extra):return {'group_slug':self.group.slug,'month_pk':self.month.pk,**extra}
    def root(self):return get_user_model().objects.create_user('navigation-root',is_superuser=True)

    def test_account_pair_is_role_sensitive(self):
        for user,target,label in [(self.owner_user,'dashboard','Back to Home'),(self.root(),'config-dashboard','Back to Platform Administration')]:
            self.assertEqual(self.nav('account',user=user),{'logical_parent_url':reverse(target),'logical_parent_label':label})

    def test_team_delete_label_matches_edit_destination(self):
        ChallengeMonth.objects.filter(pk=self.month.pk).update(status='draft');self.month.refresh_from_db()
        team=Team.objects.create(month=self.month,name='Unused Team')
        result=self.nav('team-delete',self.month_kwargs(pk=team.pk))
        self.assertEqual(result['logical_parent_url'],reverse('team-edit',kwargs=self.month_kwargs(pk=team.pk)))
        self.assertEqual(result['logical_parent_label'],'Back to Edit Team')
        response=self.client.get(reverse('team-delete',kwargs=self.month_kwargs(pk=team.pk)))
        self.assertContains(response,'Back to Edit Team');self.assertEqual(response.content.decode().count('class="back-button contextual-back"'),1)

    def test_staffing_has_single_challenge_return_and_keeps_cancel(self):
        for name in ['challenge-host-list','challenge-floater-list']:
            response=self.client.get(reverse(name,kwargs=self.month_kwargs()))
            self.assertContains(response,'Back to Challenge');self.assertNotContains(response,'Back to Month')
            self.assertEqual(response.content.decode().count('class="back-button contextual-back"'),1)

    def test_new_settings_and_history_mapping_pairs(self):
        for name,target,label in [('challenge-branding','challenge-settings','Back to Challenge Settings'),('challenge-botm-settings','challenge-settings','Back to Challenge Settings'),('immersive-settings','challenge-settings','Back to Challenge Settings'),('botm-list','month-detail','Back to Challenge'),('botm-match-list','botm-list','Back to Book of the Month'),('personal-tbr-match-list','review-queue','Back to Review Queue')]:
            result=self.nav(name,self.month_kwargs())
            kwargs={'group_slug':self.group.slug,'pk':self.month.pk} if target=='month-detail' else self.month_kwargs()
            self.assertEqual(result,{'logical_parent_url':reverse(target,kwargs=kwargs),'logical_parent_label':label})

    def test_unauthorized_settings_and_cross_group_parents_suppressed(self):
        result=self.nav('challenge-branding',self.month_kwargs(),user=self.reader_user)
        self.assertIsNone(result['logical_parent_url']);self.assertEqual(result['logical_parent_label'],'')
        other=ReadingGroup.objects.create(name='Other',slug='other-navigation')
        result=self.nav('challenge-branding',{'group_slug':other.slug,'month_pk':self.month.pk})
        self.assertIsNone(result['logical_parent_url'])
        outsider=get_user_model().objects.create_user('navigation-outsider')
        self.assertIsNone(self.nav('group-discord-notifications',{'group_slug':self.group.slug},user=outsider)['logical_parent_url'])

    def test_arbitrary_next_and_referer_are_not_navigation_inputs(self):
        result=self.nav('account',query={'next':'https://evil.example/','return_url':'//evil.example/'})
        self.assertEqual(result['logical_parent_url'],reverse('dashboard'))

    def test_routes_are_intentionally_classified_once(self):
        from .urls import urlpatterns
        names={route.name for route in urlpatterns if route.name}
        self.assertEqual(len(names),233)
        self.assertFalse(set(NAVIGATION_PARENTS)&set(NAVIGATION_EXCEPTIONS))
        self.assertEqual(names,set(NAVIGATION_PARENTS)|set(NAVIGATION_EXCEPTIONS))
        for name,kwargs in [('dashboard',None),('global-search',None),('health',None),('config-login',None),('submission-catalog',self.month_kwargs())]:
            self.assertIsNone(self.nav(name,kwargs)['logical_parent_url'])
        self.assertIsNone(logical_navigation(SimpleNamespace(resolver_match=None))['logical_parent_url'])

    def test_shared_partial_escapes_values_and_requires_a_complete_pair(self):
        html=render_to_string('core/includes/page_return.html',{'destination':'/account/?a=1&b=2','label':'Back <unsafe>'})
        self.assertIn('Back &lt;unsafe&gt;',html);self.assertIn('/account/?a=1&amp;b=2',html);self.assertIn('aria-hidden="true"',html)
        self.assertNotIn('onclick',html)
        self.assertEqual(render_to_string('core/includes/page_return.html',{'destination':'/account/'}).strip(),'')

    def test_recovery_trusted_detail_then_domain_fallback(self):
        user=self.root();kwargs={'pk':self.group.pk}
        detail=reverse('recovery-group-detail',kwargs=kwargs)
        result=self.nav('recovery-group-status',kwargs,user=user,_recovery_navigation_parent=detail)
        self.assertEqual(result['logical_parent_url'],detail)
        self.assertEqual(result['logical_parent_label'],'Back to Group Recovery')
        for bad in ['https://evil.example/','//evil.example/',reverse('group-detail',kwargs={'group_slug':self.group.slug}),reverse('recovery-group-detail',kwargs={'pk':987654})]:
            result=self.nav('recovery-group-status',kwargs,user=user,_recovery_navigation_parent=bad)
            self.assertEqual(result['logical_parent_url'],reverse('recovery-group-list'))
        self.assertIsNone(self.nav('recovery-group-status',kwargs,_recovery_navigation_parent=detail)['logical_parent_url'])

    def test_recovery_rendering_uses_detail_and_keeps_cancel(self):
        user=self.root();self.client.force_login(user)
        url=reverse('recovery-group-status',args=[self.group.pk]);detail=reverse('recovery-group-detail',args=[self.group.pk])
        count=AuditEvent.objects.count();response=self.client.get(url)
        self.assertEqual(response.status_code,200);self.assertEqual(response.context['logical_parent_url'],detail)
        self.assertContains(response,f'href="{detail}">Cancel</a>');self.assertEqual(AuditEvent.objects.count(),count)


from . import test_recaps as recap_fixture

class ChronicleParentTests(TestCase):
    setUp=recap_fixture.RecapTests.setUp
    make_book=recap_fixture.RecapTests.make_book

    def test_selected_challenge_group_and_home_are_canonical(self):
        self.client.force_login(self.reader)
        for filters,target,label in [({'group':self.group.pk,'challenge':self.month.pk},self.month.get_absolute_url(),'Back to Challenge'),({'group':self.group.pk},reverse('group-detail',args=[self.group.slug]),'Back to Group'),({},reverse('dashboard'),'Back to Home')]:
            response=self.client.get(self.url,filters)
            self.assertEqual(response.context['logical_parent_url'],target)
            self.assertEqual(response.context['logical_parent_label'],label)
            self.assertEqual(response.content.decode().count('class="back-button contextual-back"'),1)

    def test_forged_group_or_historical_membership_cannot_create_private_parent(self):
        self.client.force_login(self.reader)
        response=self.client.get(self.url,{'group':self.foreign.pk,'next':'https://evil.example/'})
        self.assertEqual(response.context['logical_parent_url'],reverse('dashboard'))
        member=self.members[self.reader.pk];member.is_active=False;member.save()
        response=self.client.get(self.url,{'challenge':self.month.pk})
        self.assertEqual(response.context['logical_parent_url'],reverse('dashboard'))
        self.assertNotContains(response,f'class="back-button contextual-back" href="{self.month.get_absolute_url()}"')

    def test_wrapped_retains_one_existing_filtered_chronicle_return(self):
        self.client.force_login(self.reader)
        response=self.client.get(reverse('recap-wrapped'),{'group':self.group.pk,'challenge':self.month.pk,'period':'month','anchor':'2026-11-05'})
        self.assertIsNone(response.context['logical_parent_url'])
        self.assertContains(response,'Back to Chronicle')
        self.assertIn('challenge='+str(self.month.pk),response.context['dashboard_url'])
        self.assertIn('period=month',response.context['dashboard_url'])
        self.assertContains(response,'class="back-button contextual-back"',count=1)
