"""Focused presentation checks; no database or permission logic changes."""
from types import SimpleNamespace as NS

from django.template.loader import render_to_string
from django.test import SimpleTestCase
from django.urls import resolve, reverse


def member_context(**overrides):
    member = NS(
        pk=42, user_id=2, is_active=True, get_role_display="Member",
        user=NS(public_name="A Readable Display Name", northbound_profile=NS(avatar_url="/static/avatars/owl.svg")),
        visible_discord_username="Visible Discord", planning=NS(completed_challenges=7, average_pages_display="1,234"),
    )
    context = dict(group=NS(slug="reading-group", name="Reading Group"), participants=[member],
                   user=NS(id=1, is_superuser=False), can_manage=True,
                   can_manage_permissions=True, can_remove=True)
    context.update(overrides)
    return context


class GroupMembersMobileTests(SimpleTestCase):
    def render_cards(self, context):
        html = render_to_string("core/participant_list.html", context)
        desktop, mobile = html.split('<div class="participant-planning-mobile">', 1)
        self.assertIn('table-wrap participant-planning-desktop', desktop)
        self.assertIn('<table class="sticky-identity">', desktop)
        return desktop, mobile

    def test_cards_preserve_desktop_information_and_routes(self):
        desktop, mobile = self.render_cards(member_context())
        for content in ("A Readable Display Name", "Member", "7", "1,234", "Active", "/static/avatars/owl.svg"):
            self.assertIn(content, desktop)
            self.assertIn(content, mobile)
        for section in (desktop, mobile):
            self.assertNotIn("Discord:", section)
            self.assertNotIn("Visible Discord", section)
        for route in ("participant-detail", "participant-permissions-edit", "participant-deactivate"):
            url = reverse(route, args=["reading-group", 42])
            self.assertIn('href="' + url + '"', mobile)
            self.assertEqual(resolve(url).url_name, route)
        self.assertIn('card participant-planning-card', mobile)
        self.assertIn('details planning-card-details', mobile)

    def test_hidden_information_and_actions_remain_hidden(self):
        context = member_context(can_manage=False, can_manage_permissions=False, can_remove=False)
        member = context["participants"][0]
        member.planning = None
        member.visible_discord_username = ""
        member.user.public_name = "username-fallback"
        member.user.northbound_profile.avatar_url = ""
        member.is_active = False
        _, mobile = self.render_cards(context)
        for content in ("username-fallback", "profile-picture-fallback", "Inactive", "—"):
            self.assertIn(content, mobile)
        for content in ("Visible Discord", "Permissions</a>", "Remove</a>", 'src=""'):
            self.assertNotIn(content, mobile)

    def test_self_actions_and_empty_state(self):
        context = member_context(user=NS(id=2, is_superuser=False))
        _, mobile = self.render_cards(context)
        self.assertNotIn("Permissions</a>", mobile)
        self.assertNotIn("Remove</a>", mobile)
        desktop, mobile = self.render_cards(member_context(participants=[]))
        for section in (desktop, mobile):
            self.assertIn("No members have been added.", section)
