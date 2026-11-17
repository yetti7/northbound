"""Navigation-only login links and the existing credential boundaries."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


class LoginNavigationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = 'Navigation-Password-73!'
        User = get_user_model()
        cls.reader = User.objects.create_user('NavReader', 'nav-reader@example.invalid', cls.password)
        cls.owner = User.objects.create_superuser('NavOwner', 'nav-owner@example.invalid', cls.password)

    def test_reader_link_uses_existing_administrator_route(self):
        response = self.client.get(reverse('login'))
        self.assertContains(response, '<p class="muted"><small><a href="' + reverse('config-login') + '">Platform Administration →</a></small></p>', html=True)
        self.assertEqual(reverse('config-login'), '/config/login/')

    def test_administrator_link_uses_existing_reader_route(self):
        response = self.client.get(reverse('config-login'))
        self.assertContains(response, '<p class="muted"><small><a href="' + reverse('login') + '">← Reader Login</a></small></p>', html=True)
        self.assertEqual(reverse('login'), '/accounts/login/')

    def test_username_email_case_insensitivity_on_both_login_paths(self):
        for route, user in [('login', self.reader), ('config-login', self.owner)]:
            for identifier in (user.username.upper(), user.email.upper()):
                with self.subTest(route=route, identifier=identifier):
                    self.client.logout()
                    response = self.client.post(reverse(route), {'username': identifier, 'password': self.password})
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(int(self.client.session['_auth_user_id']), user.pk)

    def test_reader_and_administrator_login_boundaries(self):
        for route, user in [('config-login', self.reader), ('login', self.owner)]:
            for identifier in (user.username, user.email):
                with self.subTest(route=route, identifier=identifier):
                    response = self.client.post(reverse(route), {'username': identifier, 'password': self.password})
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.context['form'].errors)
                    self.assertNotIn('_auth_user_id', self.client.session)

    def test_passwords_remain_case_sensitive_on_both_paths(self):
        for route, user in [('login', self.reader), ('config-login', self.owner)]:
            with self.subTest(route=route):
                response = self.client.post(reverse(route), {'username': user.email, 'password': self.password.swapcase()})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context['form'].errors)
                self.assertNotIn('_auth_user_id', self.client.session)
