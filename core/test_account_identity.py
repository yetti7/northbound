"""Focused v1.1.3 regression coverage; no production fixtures or external delivery."""
from django.contrib.auth import get_user_model
from django.contrib.admin.sites import site
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, RequestFactory
from django.urls import reverse
from django.utils import timezone
from .forms import (RegularAuthenticationForm, RootAuthenticationForm, PublicRegistrationForm,
                    FirstRunSetupForm, PlatformOwnerAcceptanceForm, MemberCreateForm,
                    AccountProfileForm, PlatformAccountIdentityForm)
from .identity import public_name, validate_identifiers, resolve_identifier
from .models import UserProfile
from .admin import NorthboundAdminAuthenticationForm, AdminAccountCreationForm
from .middleware import RequireDisplayNameConfirmationMiddleware
from django.http import HttpResponse

User = get_user_model()
PASSWORD = 'Identity-test-passphrase-732!'


class AccountIdentityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('Reader.One', 'reader@example.invalid', PASSWORD)
        self.profile = UserProfile.objects.create(user=self.user, discord_username='Public Reader')

    def login_form(self, identifier, password=PASSWORD, cls=RegularAuthenticationForm):
        return cls(data={'username': identifier, 'password': password})

    def registration(self, **changes):
        data = {'username':'new-reader','email':'new@example.invalid','display_name':'Public Reader',
                'password1':PASSWORD,'password2':PASSWORD,'selected_avatar':''}
        data.update(changes)
        return PublicRegistrationForm(data)

    def test_username_email_case_insensitive_existing_password(self):
        original = self.user.password
        for identifier in ('Reader.One','READER.ONE','reader@example.invalid','READER@EXAMPLE.INVALID'):
            form = self.login_form(identifier)
            self.assertTrue(form.is_valid(), form.errors)
            self.assertEqual(form.get_user().pk, self.user.pk)
            self.assertEqual(form.get_user().backend, 'django.contrib.auth.backends.ModelBackend')
        self.user.refresh_from_db()
        self.assertEqual(self.user.password, original)

    def test_generic_failures_and_inactive(self):
        errors = []
        for identifier,password in [('unknown',PASSWORD),('reader.one','wrong')]:
            form=self.login_form(identifier,password)
            self.assertFalse(form.is_valid()); errors.append(form.non_field_errors()[0])
        self.user.is_active=False;self.user.save(update_fields=['is_active'])
        form=self.login_form('reader.one');self.assertFalse(form.is_valid())
        self.assertEqual(errors[0],errors[1]);self.assertEqual(errors[0],form.non_field_errors()[0])

    def test_unicode_normalization_and_fail_closed_ambiguity(self):
        user=User.objects.create_user('Straße','unicode@example.invalid',PASSWORD)
        self.assertEqual(resolve_identifier('STRASSE'),user.pk)
        self.assertFalse(self.registration(username='STRASSE').is_valid())
        # A legacy/noninteractive writer can bypass Python casefold checks. Never choose arbitrarily.
        other=User.objects.create_user('strasse','other@example.invalid',PASSWORD)
        self.assertIsNone(resolve_identifier('STRASSE'))
        self.assertFalse(self.login_form('STRASSE').is_valid())
        self.assertNotEqual(user.pk,other.pk)

    def test_login_boundaries(self):
        owner=User.objects.create_superuser('owner','owner@example.invalid',PASSWORD)
        self.assertFalse(self.login_form(owner.email).is_valid())
        self.assertTrue(self.login_form(owner.email.upper(),cls=RootAuthenticationForm).is_valid())
        self.assertFalse(self.login_form(self.user.email,cls=RootAuthenticationForm).is_valid())
        self.assertFalse(self.login_form(self.user.email,cls=NorthboundAdminAuthenticationForm).is_valid())
        self.assertTrue(self.login_form(owner.email,cls=site.login_form).is_valid())
        staff=User.objects.create_user('staff','staff@example.invalid',PASSWORD,is_staff=True)
        self.assertTrue(self.login_form(staff.email,cls=site.login_form).is_valid())

    def test_required_registration_and_duplicate_names(self):
        for field in ('username','email','display_name'):
            form=self.registration(**{field:''});self.assertFalse(form.is_valid());self.assertIn(field,form.errors)
        form=self.registration();self.assertTrue(form.is_valid(),form.errors)
        user=form.save();profile=user.northbound_profile
        self.assertEqual(profile.discord_username,'Public Reader')
        self.assertIsNotNone(profile.display_name_confirmed_at)
        self.assertTrue(user.check_password(PASSWORD))

    def test_identifiers_all_creation_and_edit_forms(self):
        for changes in ({'username':'READER.ONE'},{'email':'READER@EXAMPLE.INVALID'},
                        {'username':'reader@example.invalid'}):
            self.assertFalse(self.registration(**changes).is_valid())
        # Email-shaped usernames are valid, but cannot collide with someone else's email.
        for cls in (FirstRunSetupForm,PlatformOwnerAcceptanceForm,AdminAccountCreationForm):
            form=cls({'username':'fresh','email':'READER@EXAMPLE.INVALID','display_name':'Name','password1':PASSWORD,'password2':PASSWORD})
            self.assertFalse(form.is_valid());self.assertIn('email',form.errors)
        member=MemberCreateForm({'username':'fresh','email':'READER@EXAMPLE.INVALID','display_name':'Name','role':'member','temporary_password':PASSWORD})
        self.assertFalse(member.is_valid());self.assertIn('email',member.errors)
        other=User.objects.create_user('other','other@example.invalid',PASSWORD)
        for cls in (AccountProfileForm,PlatformAccountIdentityForm):
            form=cls({'username':'READER.ONE','email':other.email,'display_name':'Name'},instance=other)
            self.assertFalse(form.is_valid());self.assertIn('username',form.errors)
            form=cls({'username':other.username,'email':'READER@EXAMPLE.INVALID','display_name':'Name'},instance=other)
            self.assertFalse(form.is_valid());self.assertIn('email',form.errors)

    def test_database_lower_guards_and_cross_namespace(self):
        for username,email in [('READER.ONE','another@example.invalid'),('another','READER@EXAMPLE.INVALID'),
                               ('reader@example.invalid','another@example.invalid')]:
            with self.assertRaises(IntegrityError),transaction.atomic():
                User.objects.create_user(username,email,PASSWORD)

    def test_username_change_releases_old_identifier(self):
        pk=self.user.pk;password=self.user.password
        form=AccountProfileForm({'username':'renamed','email':self.user.email,'display_name':'New Public','selected_avatar':''},instance=self.user)
        self.assertTrue(form.is_valid(),form.errors);form.save()
        replacement=User.objects.create_user('Reader.One','replacement@example.invalid',PASSWORD)
        self.user.refresh_from_db()
        self.assertEqual(self.user.pk,pk);self.assertEqual(self.user.password,password)
        self.assertNotEqual(replacement.pk,pk);self.assertEqual(public_name(self.user),'New Public')

    def test_fallback_is_not_truncated_or_stored(self):
        self.profile.discord_username='';self.profile.save()
        self.user.username='x'*150;self.user.save()
        self.user.refresh_from_db()
        self.assertEqual(public_name(self.user),'x'*150)
        self.profile.refresh_from_db();self.assertEqual(self.profile.discord_username,'')
        self.assertIsNone(self.profile.display_name_confirmed_at)

    def test_existing_session_confirmation_returns_to_account(self):
        self.client.force_login(self.user,backend='django.contrib.auth.backends.ModelBackend')
        session_key=self.client.session.session_key
        response=self.client.get('/?original=yes',HTTP_ACCEPT='text/html')
        self.assertRedirects(response,'/account/',fetch_redirect_response=False)
        page=self.client.get('/account/');self.assertContains(page,'Confirm Your Display Name')
        self.assertContains(page,'Public Reader')
        response=self.client.post('/account/',{'display_name':'Public Reader'})
        self.assertRedirects(response,'/account/',fetch_redirect_response=False)
        self.profile.refresh_from_db();confirmed=self.profile.display_name_confirmed_at
        self.assertIsNotNone(confirmed);self.assertEqual(self.client.session.session_key,session_key)
        self.assertEqual(self.client.session['_auth_user_backend'],'django.contrib.auth.backends.ModelBackend')
        self.assertEqual(self.client.get('/',HTTP_ACCEPT='text/html').status_code,200)
        self.assertEqual(self.client.get('/account/').context['confirming_display_name'],False)
        self.profile.refresh_from_db();self.assertEqual(self.profile.display_name_confirmed_at,confirmed)

    def test_blank_confirmation_and_unrelated_identifiers_not_required(self):
        self.profile.discord_username='';self.profile.save()
        # Legacy account with a missing email can still confirm its public name.
        self.user.email='';self.user.save(update_fields=['email'])
        self.client.force_login(self.user)
        response=self.client.post('/account/',{'display_name':'   '})
        self.assertEqual(response.status_code,200)
        self.profile.refresh_from_db();self.assertIsNone(self.profile.display_name_confirmed_at)
        self.client.post('/account/',{'display_name':'Confirmed'})
        self.profile.refresh_from_db();self.assertIsNotNone(self.profile.display_name_confirmed_at)
        self.user.refresh_from_db();self.assertEqual(self.user.email,'')

    def test_superuser_missing_profile_and_malicious_return(self):
        owner=User.objects.create_superuser('owner','owner@example.invalid',PASSWORD)
        self.client.force_login(owner)
        self.assertRedirects(self.client.get('/config/',HTTP_ACCEPT='text/html'),'/account/',fetch_redirect_response=False)
        self.client.get('/account/')
        self.assertTrue(UserProfile.objects.filter(user=owner).exists())
        session=self.client.session;session['display_name_return_to']='https://evil.invalid/';session.save()
        self.assertRedirects(self.client.post('/account/',{'display_name':'Owner'}),'/account/',fetch_redirect_response=False)
        self.assertEqual(self.client.get('/config/',HTTP_ACCEPT='text/html').status_code,200)

    def test_middleware_exempts_mutations_background_and_management(self):
        factory=RequestFactory();middleware=RequireDisplayNameConfirmationMiddleware(lambda request:HttpResponse('ok'))
        for method,path,headers in [('post','/',{'HTTP_ACCEPT':'text/html'}),('get','/',{'HTTP_ACCEPT':'application/json'}),
            ('get','/',{'HTTP_ACCEPT':'text/html','HTTP_X_REQUESTED_WITH':'XMLHttpRequest'}),
            ('get','/',{'HTTP_ACCEPT':'text/html','HTTP_SEC_FETCH_DEST':'empty'}),
            ('get','/health/',{'HTTP_ACCEPT':'text/html'}),('get','/accounts/logout/',{'HTTP_ACCEPT':'text/html'}),
            ('get','/accounts/password/change/',{'HTTP_ACCEPT':'text/html'}),('get','/account/hardcover/oauth/callback/',{'HTTP_ACCEPT':'text/html'}),
            ('get','/config/audit/export/',{'HTTP_ACCEPT':'text/html'}),('get','/account/',{'HTTP_ACCEPT':'text/html'})]:
            request=getattr(factory,method)(path,**headers);request.user=self.user
            self.assertEqual(middleware(request).status_code,200,(method,path,headers))

    def test_existing_forced_password_workflow_precedes_confirmation(self):
        self.profile.must_change_password=True;self.profile.save()
        self.client.force_login(self.user)
        self.assertRedirects(self.client.get('/',HTTP_ACCEPT='text/html'),'/accounts/password/change/',fetch_redirect_response=False)

    def test_admin_visibility_permissions_and_temporary_password(self):
        owner=User.objects.create_superuser('owner','owner@example.invalid',PASSWORD)
        self.client.force_login(owner)
        response=self.client.get(reverse('config-user-detail',args=[self.user.pk]))
        for value in (self.user.username,'Public Reader',self.user.email,'First Name','Last Name','Date Joined','Last Sign-In'):
            self.assertContains(response,value)
        self.assertNotContains(response,self.user.password)
        self.assertEqual(self.client.get(reverse('config-user-detail',args=[owner.pk])).status_code,200)
        response=self.client.post(reverse('config-user-password-reset',args=[self.user.pk]))
        self.assertContains(response,'Temporary Password');self.assertTrue(response.context['temporary_password'])
        self.user.refresh_from_db();self.assertNotContains(response,self.user.password)
        UserProfile.objects.filter(user=self.user).update(must_change_password=False)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse('config-user-detail',args=[owner.pk])).status_code,403)

    def test_setup_owner_acceptance_and_admin_creation_confirm_names(self):
        for i,cls in enumerate((FirstRunSetupForm,PlatformOwnerAcceptanceForm,AdminAccountCreationForm)):
            form=cls({'username':f'created-owner-{i}','email':f'created{i}@example.invalid',
                      'display_name':'Chosen Name','password1':PASSWORD,'password2':PASSWORD})
            self.assertTrue(form.is_valid(),form.errors)
            user=form.save()
            self.assertEqual(user.northbound_profile.discord_username,'Chosen Name')
            self.assertIsNotNone(user.northbound_profile.display_name_confirmed_at)
            self.assertTrue(user.check_password(PASSWORD))

    def test_admin_actual_creation_and_no_hash_in_change_view(self):
        owner=User.objects.create_superuser('admin-owner','admin-owner@example.invalid',PASSWORD)
        self.client.force_login(owner)
        response=self.client.post('/platform-admin/auth/user/add/',{
            'username':'admin-created','email':'admin-created@example.invalid','display_name':'New Reader',
            'password1':PASSWORD,'password2':PASSWORD,'_save':'Save'})
        self.assertEqual(response.status_code,302)
        created=User.objects.get(username='admin-created')
        self.assertEqual(created.northbound_profile.discord_username,'New Reader')
        self.assertIsNotNone(created.northbound_profile.display_name_confirmed_at)
        response=self.client.get(f'/platform-admin/auth/user/{created.pk}/change/')
        self.assertEqual(response.status_code,200)
        self.assertNotContains(response,created.password)

    def test_preupgrade_account_post_is_preserved_without_confirming(self):
        self.client.force_login(self.user)
        response=self.client.post('/account/',{'username':'updated-before-confirmation','email':self.user.email,
            'first_name':'Kept','last_name':'Names','discord_username':'Legacy Posted Name','selected_avatar':''})
        self.assertEqual(response.status_code,302)
        self.user.refresh_from_db();self.profile.refresh_from_db()
        self.assertEqual(self.user.first_name,'Kept');self.assertEqual(self.user.username,'updated-before-confirmation')
        self.assertEqual(self.profile.discord_username,'Legacy Posted Name')
        self.assertIsNone(self.profile.display_name_confirmed_at)

    def test_previous_return_destination_is_discarded_and_confirmation_persists(self):
        self.client.force_login(self.user)
        session=self.client.session;session['display_name_return_to']='http://testserver/?safe=yes';session.save()
        self.assertRedirects(self.client.post('/account/',{'display_name':'Public Reader'}),'/account/',fetch_redirect_response=False)
        self.profile.refresh_from_db();confirmed=self.profile.display_name_confirmed_at
        self.client.post('/account/',{'username':self.user.username,'email':self.user.email,'display_name':'Changed Later','selected_avatar':''})
        self.profile.refresh_from_db();self.assertEqual(self.profile.display_name_confirmed_at,confirmed)


class IdentityMigrationTests(TransactionTestCase):
    previous=('core','0084_platform_email_settings')
    target=('core','0085_account_identity')

    def setUp(self):
        executor=MigrationExecutor(connection);executor.migrate([self.previous])
        self.old_apps=executor.loader.project_state([self.previous]).apps

    def tearDown(self):
        # Clear deliberately conflicting historical fixtures before restoring schema.
        self.old_apps.get_model('auth','User').objects.all().delete()
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_conflict_aborts_before_schema_change(self):
        UserOld=self.old_apps.get_model('auth','User')
        a=UserOld.objects.create(username='a',email='duplicate@example.invalid',password='preserved')
        b=UserOld.objects.create(username='b',email='DUPLICATE@example.invalid',password='preserved')
        with self.assertRaisesRegex(RuntimeError,'Conflicting account IDs'):
            MigrationExecutor(connection).migrate([self.target])
        columns={col.name for col in connection.introspection.get_table_description(connection.cursor(),'core_userprofile')}
        self.assertNotIn('display_name_confirmed_at',columns)
        self.assertEqual(UserOld.objects.get(pk=a.pk).password,'preserved')

    def test_profiles_values_and_passwords_preserved(self):
        UserOld=self.old_apps.get_model('auth','User');ProfileOld=self.old_apps.get_model('core','UserProfile')
        users=[UserOld.objects.create(username=f'legacy{i}',email=f'legacy{i}@example.invalid',password=f'old-hash-{i}') for i in range(3)]
        ProfileOld.objects.create(user_id=users[0].pk,discord_username='Private Name',discord_username_is_public=False)
        ProfileOld.objects.create(user_id=users[1].pk,discord_username='')
        MigrationExecutor(connection).migrate([self.target])
        for i,user in enumerate(users):
            self.assertEqual(User.objects.get(pk=user.pk).password,f'old-hash-{i}')
            self.assertIsNone(UserProfile.objects.get(user_id=user.pk).display_name_confirmed_at)
        self.assertEqual(UserProfile.objects.get(user_id=users[0].pk).discord_username,'Private Name')
        self.assertEqual(UserProfile.objects.get(user_id=users[1].pk).discord_username,'')
        self.assertEqual(UserProfile.objects.get(user_id=users[2].pk).discord_username,'')


class IdentityPresentationTests(TestCase):
    from .test_team_planning_foundation import TeamPlanningFoundationTests as Fixture
    setUp = Fixture.setUp
    member = Fixture.member
    book = Fixture.book
    historical_book = Fixture.historical_book

    def test_duplicate_names_and_username_reuse_preserve_scoring_and_ownership(self):
        from .reader_rumble import build_reader_rumble
        from .models import BookSubmission, TeamAssignment, MonthEnrollment, AuditEvent, Membership
        from .score_aggregation import challenge_score_totals
        UserProfile.objects.filter(user__in=[self.reader_user,self.leader_a_user]).update(discord_username='Same Public Name')
        before = (list(BookSubmission.objects.values()),list(TeamAssignment.objects.values()),
                  list(MonthEnrollment.objects.values()),list(Membership.objects.values()),list(AuditEvent.objects.values()))
        scores=challenge_score_totals(month=self.month)
        old_username=self.reader_user.username
        self.reader_user.username='changed-login';self.reader_user.save(update_fields=['username'])
        replacement=User.objects.create_user(old_username,'replacement@example.invalid',PASSWORD)
        rumble=build_reader_rumble(actor=self.host_user,team=self.team)
        same=[row for row in rumble['rows'] if row['display_name']=='Same Public Name']
        self.assertEqual({row['participant_id'] for row in same},{self.reader.pk,self.leader_a.pk})
        self.assertEqual(sorted(row['value'] for row in same),[100,200])
        self.assertEqual(challenge_score_totals(month=self.month),scores)
        self.assertNotIn(replacement.pk,list(Membership.objects.values_list('user_id',flat=True)))
        after = (list(BookSubmission.objects.values()),list(TeamAssignment.objects.values()),
                 list(MonthEnrollment.objects.values()),list(Membership.objects.values()),list(AuditEvent.objects.values()))
        self.assertEqual(before,after)

    def test_public_surfaces_current_names_search_and_wrapped(self):
        import json
        from .recaps import build_recap
        from .recap_slides import build_wrapped_slides
        from .models import BookSubmission
        UserProfile.objects.filter(user=self.reader_user).update(discord_username='Current Public Name',discord_username_is_public=False)
        type(self.month).objects.filter(pk=self.month.pk).update(status='active');self.month.refresh_from_db()
        self.client.force_login(self.host_user)
        for route,args in [('participant-list',[self.group.slug]),('team-detail',[self.group.slug,self.month.pk,self.team.pk]),
                           ('month-participant-list',[self.group.slug,self.month.pk])]:
            response=self.client.get(reverse(route,args=args))
            self.assertEqual(response.status_code,200,(route,response.status_code))
            self.assertContains(response,'Current Public Name')
        self.client.force_login(self.platform_owner)
        self.assertContains(self.client.get(reverse('month-detail',args=[self.group.slug,self.month.pk])),'Current Public Name')
        self.client.force_login(self.host_user)
        book=BookSubmission.objects.filter(month=self.month,participant=self.reader).first()
        self.assertContains(self.client.get(reverse('submission-history',args=[self.group.slug,self.month.pk,book.pk])),'Current Public Name')
        search=self.client.get(reverse('global-search'),{'q':'Current Public Name'})
        self.assertContains(search,'Current Public Name')
        self.reader_user.refresh_from_db()
        recap=build_recap(viewer=self.reader_user,perspective='reader',challenge_id=self.month.pk)
        self.assertIn('Current Public Name',recap['heading'])
        type(self.month).objects.filter(pk=self.month.pk).update(status='completed');self.month.refresh_from_db()
        recap=build_recap(viewer=self.reader_user,perspective='reader',challenge_id=self.month.pk)
        slides=build_wrapped_slides(recap)
        self.assertIn('Current Public Name',json.dumps(slides,default=str))

    def test_staff_created_account_requires_real_identity_and_is_confirmed(self):
        form=MemberCreateForm({'username':'staff-created','email':'staff-created@example.invalid','display_name':'New Reader',
                               'role':'member','temporary_password':PASSWORD})
        self.assertTrue(form.is_valid(),form.errors)
        membership=form.save(self.group)
        self.assertEqual(membership.user.northbound_profile.discord_username,'New Reader')
        self.assertIsNotNone(membership.user.northbound_profile.display_name_confirmed_at)
        self.assertTrue(membership.user.check_password(PASSWORD))

    def test_nonplatform_roles_cannot_inspect_private_account_information(self):
        for actor in (self.owner_user,self.host_user,self.leader_a_user,self.floater_user,self.reader_user):
            self.client.force_login(actor)
            self.assertEqual(self.client.get(reverse('config-user-detail',args=[self.reader_user.pk])).status_code,403)
