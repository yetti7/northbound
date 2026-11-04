from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from . import tests as fixture
from .models import Membership


class PlatformGroupTableTests(TestCase):
    setUp = fixture.PlatformGroupManagementTests.setUp

    def test_five_columns_owner_disclosure_and_no_personal_details(self):
        user = get_user_model().objects.create_user('additional-owner', email='private@example.invalid')
        Membership.objects.create(group=self.group, user=user, role='owner', display_name='Zeta Owner')
        self.client.force_login(self.root)
        response = self.client.get(reverse('config-group-list'))
        self.assertEqual(response.status_code, 200)
        for label in ('Group', 'Owners', 'Members', 'Current Challenge', 'Actions'):
            self.assertContains(response, f'<th>{label}</th>', html=True)
        self.assertContains(response, '+1')
        self.assertContains(response, 'Zeta Owner')
        self.assertContains(response, 'group-owner-list')
        self.assertContains(response, 'Open →')
        self.assertNotContains(response, 'private@example.invalid')
        self.assertNotContains(response, '<th>Recent Activity</th>', html=True)
        self.assertNotContains(response, '/groups/central-reading-group/</span>')
        self.assertContains(response, 'data-group-search')
        self.assertContains(response, 'data-group-status')
        self.assertContains(response, 'Create Group')
