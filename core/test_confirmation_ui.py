"""Focused confirmation presentation and regular-account regression coverage."""
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from .forms import AccountProfileForm, PlatformAccountIdentityForm
from .models import UserProfile


class ConfirmationUITests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('confirmation-reader', 'confirmation@example.invalid', 'Test-Password-321!')
        self.profile = UserProfile.objects.create(user=self.user, discord_username='Existing Display Name')
        self.client.force_login(self.user)

    def test_unconfirmed_view_is_focused_and_prefilled(self):
        with patch('core.views.reader_sync_health') as health:
            response = self.client.get(reverse('account'))
            health.assert_not_called()
        self.assertTemplateUsed(response, 'core/display_name_confirmation.html')
        self.assertContains(response, 'Confirm Your Display Name')
        self.assertContains(response, 'value="Existing Display Name"')
        self.assertContains(response, 'name="display_name"')
        self.assertContains(response, 'Sign Out')
        for text in ('Personal Hardcover Connection', 'Hardcover Synchronization', 'name="username"',
                     'name="email"', 'name="profile_picture"', 'name="first_name"', 'Change Password'):
            self.assertNotContains(response, text)
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.display_name_confirmed_at)

    def test_blank_display_name_cannot_be_confirmed(self):
        response = self.client.post(reverse('account'), {'confirm_display_name':'1', 'display_name':'   '})
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'core/display_name_confirmation.html')
        self.assertFormError(response.context['form'], 'display_name', 'This field is required.')
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.display_name_confirmed_at)
        self.assertEqual(self.profile.discord_username, 'Existing Display Name')

    def test_save_redirects_to_account_with_success_and_updated_name(self):
        session = self.client.session
        session['display_name_return_to'] = '/?previous=destination'
        session.save()
        response = self.client.post(reverse('account'), {'confirm_display_name':'1', 'display_name':'Updated Display Name'}, follow=True)
        self.assertEqual(response.redirect_chain, [(reverse('account'), 302)])
        self.assertTemplateUsed(response, 'core/account.html')
        self.assertContains(response, 'Your Display Name has been confirmed.')
        self.assertContains(response, 'value="Updated Display Name"')
        self.assertContains(response, 'Personal Hardcover Connection')
        self.assertNotContains(response, 'Confirm Your Display Name')
        self.assertNotIn('display_name_return_to', self.client.session)
        self.profile.refresh_from_db()
        confirmed = self.profile.display_name_confirmed_at
        self.assertIsNotNone(confirmed)
        self.client.get(reverse('account'))
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.display_name_confirmed_at, confirmed)

    def test_unchanged_name_is_accepted_and_normal_edit_preserves_timestamp(self):
        self.assertRedirects(self.client.post(reverse('account'), {'display_name':'Existing Display Name'}), reverse('account'))
        self.profile.refresh_from_db()
        confirmed = self.profile.display_name_confirmed_at
        response = self.client.post(reverse('account'), {'username':self.user.username, 'email':self.user.email,
            'display_name':'Later Name', 'first_name':'', 'last_name':'', 'selected_avatar':''}, follow=True)
        self.assertContains(response, 'Your account details were updated.')
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.display_name_confirmed_at, confirmed)
        self.assertEqual(self.profile.discord_username, 'Later Name')

    def test_optional_labels_and_regular_account_settings_remain_available(self):
        self.profile.display_name_confirmed_at = timezone.now()
        self.profile.save(update_fields=['display_name_confirmed_at'])
        response = self.client.get(reverse('account'))
        for text in ('First Name (Optional)', 'Last Name (Optional)', 'Personal Hardcover Connection',
                     'Hardcover Synchronization', 'Change Password', 'name="profile_picture"', 'name="username"', 'name="email"'):
            self.assertContains(response, text)
        for cls in (AccountProfileForm, PlatformAccountIdentityForm):
            form = cls(instance=self.user)
            self.assertFalse(form.fields['first_name'].required)
            self.assertFalse(form.fields['last_name'].required)
            self.assertEqual(form.fields['first_name'].label, 'First Name (Optional)')
            self.assertEqual(form.fields['last_name'].label, 'Last Name (Optional)')
            self.assertTrue(form.fields['username'].required)
            self.assertTrue(form.fields['email'].required)
            self.assertTrue(form.fields['display_name'].required)

    def test_administrator_confirmation_preserves_configuration_and_accounts_access(self):
        owner = get_user_model().objects.create_superuser('confirmation-owner', 'owner@example.invalid', 'Test-Password-321!')
        UserProfile.objects.create(user=owner, discord_username='Owner Display Name')
        self.client.force_login(owner)
        self.assertRedirects(self.client.get(reverse('config-dashboard'), HTTP_ACCEPT='text/html'), reverse('account'), fetch_redirect_response=False)
        self.assertTemplateUsed(self.client.get(reverse('account')), 'core/display_name_confirmation.html')
        self.assertRedirects(self.client.post(reverse('account'), {'display_name':'Owner Display Name'}), reverse('account'))
        owner.refresh_from_db()
        self.assertTrue(owner.is_superuser)
        self.assertTrue(owner.is_staff)
        self.assertEqual(self.client.get(reverse('config-dashboard'), HTTP_ACCEPT='text/html').status_code, 200)
        self.assertEqual(self.client.get(reverse('config-user-list'), HTTP_ACCEPT='text/html').status_code, 200)
