from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group as AuthGroup
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .demo_data import (
    ACCOUNT_SPECS,
    DATASET_KEY,
    DEMO_AUTH_GROUP,
    DEMO_CATALOG_PROVIDER,
    DEMO_PASSWORD,
    GROUP_SPECS,
)
from .models import (
    AuditEvent,
    BookSubmission,
    CatalogBook,
    ChallengeMonth,
    Membership,
    MonthEnrollment,
    MonthTheme,
    PlatformOwnerInvitation,
    ReadingGroup,
    Team,
    TeamAssignment,
    ThemeClaim,
)


@override_settings(
    DEBUG=True,
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class DemoDataSeederTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_superuser(
            "existing-platform-owner",
            "owner@example.com",
            "owner-password-482!",
        )
        self.invitation = PlatformOwnerInvitation.objects.create(
            token_hash="a" * 64,
            created_by=self.owner,
            expires_at=timezone.now() + timedelta(days=7),
        )

    def seed(self, *arguments):
        output = StringIO()
        call_command("seed_demo_data", *arguments, stdout=output)
        return output.getvalue()

    def owner_snapshot(self):
        self.owner.refresh_from_db()
        return {
            "username": self.owner.username,
            "email": self.owner.email,
            "password": self.owner.password,
            "is_active": self.owner.is_active,
            "is_staff": self.owner.is_staff,
            "is_superuser": self.owner.is_superuser,
            "date_joined": self.owner.date_joined,
            "last_login": self.owner.last_login,
        }

    @override_settings(DEBUG=False)
    def test_production_configuration_refuses_seed_and_reset(self):
        with self.assertRaisesMessage(CommandError, "DJANGO_DEBUG=0"):
            self.seed()
        with self.assertRaisesMessage(CommandError, "DJANGO_DEBUG=0"):
            self.seed("--reset")
        self.assertEqual(get_user_model().objects.count(), 1)
        self.assertFalse(AuthGroup.objects.filter(name=DEMO_AUTH_GROUP).exists())

    def test_platform_owner_and_invitations_survive_seed_and_reset_unchanged(self):
        before = self.owner_snapshot()
        invitation_before = PlatformOwnerInvitation.objects.values().get(pk=self.invitation.pk)

        output = self.seed()
        self.assertIn("Existing Platform Owners preserved: 1", output)
        self.assertEqual(self.owner_snapshot(), before)
        self.assertFalse(Membership.objects.filter(user=self.owner).exists())
        self.assertEqual(PlatformOwnerInvitation.objects.values().get(pk=self.invitation.pk), invitation_before)

        old_demo_user_id = get_user_model().objects.get(username="maren.holt").pk
        self.seed("--reset")
        self.assertEqual(self.owner_snapshot(), before)
        self.assertFalse(Membership.objects.filter(user=self.owner).exists())
        self.assertEqual(PlatformOwnerInvitation.objects.values().get(pk=self.invitation.pk), invitation_before)
        self.assertNotEqual(get_user_model().objects.get(username="maren.holt").pk, old_demo_user_id)

    def test_reset_refuses_if_a_platform_owner_is_ever_attached_to_demo_marker(self):
        self.seed()
        marker = AuthGroup.objects.get(name=DEMO_AUTH_GROUP)
        marker.user_set.add(self.owner)
        before = self.owner_snapshot()
        demo_count = get_user_model().objects.filter(groups=marker, is_superuser=False).count()

        with self.assertRaisesMessage(CommandError, "Platform Owner is associated"):
            self.seed("--reset")

        self.assertEqual(self.owner_snapshot(), before)
        self.assertEqual(get_user_model().objects.filter(groups=marker, is_superuser=False).count(), demo_count)
        self.assertTrue(marker.user_set.filter(pk=self.owner.pk).exists())

    def logical_snapshot(self):
        demo_usernames = [username for username, _, _ in ACCOUNT_SPECS]
        return {
            "accounts": list(get_user_model().objects.filter(username__in=demo_usernames).order_by("username").values_list(
                "username", "first_name", "last_name", "email", "is_active", "is_staff", "is_superuser"
            )),
            "groups": list(ReadingGroup.objects.filter(slug__in=[spec["slug"] for spec in GROUP_SPECS.values()]).order_by("slug").values_list(
                "slug", "name", "timezone", "announcement_enabled", "announcement", "join_code", "is_active"
            )),
            "memberships": list(Membership.objects.filter(group__slug__in=[spec["slug"] for spec in GROUP_SPECS.values()]).order_by(
                "group__slug", "user__username"
            ).values_list("group__slug", "user__username", "role", "display_name", "is_active")),
            "months": list(ChallengeMonth.objects.filter(group__slug__in=[spec["slug"] for spec in GROUP_SPECS.values()]).order_by(
                "group__slug", "starts_on"
            ).values_list("group__slug", "name", "starts_on", "ends_on", "status", "announcement_mode", "announcement")),
            "teams": list(Team.objects.filter(month__group__slug__in=[spec["slug"] for spec in GROUP_SPECS.values()]).order_by(
                "month__group__slug", "month__starts_on", "name"
            ).values_list("month__group__slug", "month__name", "name", "color")),
            "themes": list(MonthTheme.objects.filter(month__group__slug__in=[spec["slug"] for spec in GROUP_SPECS.values()]).order_by(
                "month__group__slug", "month__starts_on", "name"
            ).values_list("month__group__slug", "month__name", "name", "bonus_pages", "allow_stacking", "prompt")),
            "submissions": list(BookSubmission.objects.filter(month__group__slug__in=[spec["slug"] for spec in GROUP_SPECS.values()]).order_by(
                "month__group__slug", "month__starts_on", "participant__user__username", "title"
            ).values_list(
                "month__group__slug", "month__name", "participant__user__username", "title", "status",
                "submitted_pages", "approved_pages", "bonus_pages", "final_scored_pages", "verification_method"
            )),
            "claims": list(ThemeClaim.objects.filter(submission__month__group__slug__in=[spec["slug"] for spec in GROUP_SPECS.values()]).order_by(
                "submission__month__group__slug", "submission__title", "theme__name"
            ).values_list("submission__title", "theme__name", "status", "approved_bonus_pages", "response")),
        }

    def test_seed_is_deterministic_idempotent_and_reset_recreates_same_dataset(self):
        first_output = self.seed()
        first = self.logical_snapshot()
        self.assertIn("Created canonical", first_output)
        self.assertTrue(all(get_user_model().objects.get(username=username).check_password(DEMO_PASSWORD) for username, _, _ in ACCOUNT_SPECS))

        second_output = self.seed()
        self.assertIn("Verified existing", second_output)
        self.assertEqual(self.logical_snapshot(), first)

        Team.objects.filter(month__group__slug="lantern-leaf-society").first().delete()
        with self.assertRaisesMessage(CommandError, "incomplete or changed"):
            self.seed()
        self.seed("--reset")
        self.assertEqual(self.logical_snapshot(), first)
        self.assertEqual(AuditEvent.objects.filter(action="demo.dataset_seeded", object_id=DATASET_KEY).count(), 1)

    def test_reset_preserves_unrelated_records_and_platform_owner_audit_history(self):
        unrelated_user = get_user_model().objects.create_user("local-reader", password="local-password")
        unrelated_group = ReadingGroup.objects.create(name="Local Reading Circle", slug="local-reading-circle")
        unrelated_membership = Membership.objects.create(
            group=unrelated_group,
            user=unrelated_user,
            role=Membership.Role.OWNER,
            display_name="Local Reader",
        )
        unrelated_month = ChallengeMonth.objects.create(
            group=unrelated_group,
            name="Local Month",
            starts_on=timezone.localdate().replace(day=1),
            ends_on=timezone.localdate().replace(day=28),
            status=ChallengeMonth.Status.DRAFT,
        )
        owner_event = AuditEvent.objects.create(
            actor=self.owner,
            group=unrelated_group,
            action="platform.local_history",
            object_type="ReadingGroup",
            object_id=str(unrelated_group.pk),
            summary="Unrelated Platform Owner history.",
        )
        self.seed()
        demo_group_owner_event = AuditEvent.objects.create(
            actor=self.owner,
            group=ReadingGroup.objects.get(slug="lantern-leaf-society"),
            action="platform.demo_group_reviewed",
            object_type="ReadingGroup",
            summary="Platform Owner reviewed a demo group without joining it.",
        )
        self.seed("--reset")

        self.assertTrue(get_user_model().objects.filter(pk=unrelated_user.pk).exists())
        self.assertTrue(ReadingGroup.objects.filter(pk=unrelated_group.pk).exists())
        self.assertTrue(Membership.objects.filter(pk=unrelated_membership.pk).exists())
        self.assertTrue(ChallengeMonth.objects.filter(pk=unrelated_month.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(pk=owner_event.pk).exists())
        demo_group_owner_event.refresh_from_db()
        self.assertEqual(demo_group_owner_event.actor_id, self.owner.pk)
        self.assertIsNone(demo_group_owner_event.group_id)

    def test_relationships_roles_month_states_and_scoring_are_consistent(self):
        self.seed()
        demo_slugs = [spec["slug"] for spec in GROUP_SPECS.values()]
        marker = AuthGroup.objects.get(name=DEMO_AUTH_GROUP)
        self.assertEqual(marker.user_set.filter(is_superuser=False).count(), 20)
        self.assertFalse(marker.user_set.filter(is_superuser=True).exists())
        self.assertEqual(ReadingGroup.objects.filter(slug__in=demo_slugs).count(), 2)
        self.assertEqual(Membership.objects.filter(group__slug__in=demo_slugs).count(), 22)

        for group_key, spec in GROUP_SPECS.items():
            memberships = Membership.objects.filter(group__slug=spec["slug"])
            self.assertEqual(memberships.count(), 11)
            for role in Membership.Role.values:
                self.assertTrue(memberships.filter(role=role).exists(), f"{group_key} is missing {role}")
            months = ChallengeMonth.objects.filter(group__slug=spec["slug"])
            self.assertEqual(months.count(), 2)
            self.assertEqual(months.filter(status=ChallengeMonth.Status.ACTIVE).count(), 1)
            self.assertEqual(months.filter(status__in=[ChallengeMonth.Status.COMPLETED, ChallengeMonth.Status.ARCHIVED]).count(), 1)

        self.assertEqual(Membership.objects.get(group__slug="lantern-leaf-society", user__username="nora.kim").role, Membership.Role.MEMBER)
        self.assertEqual(Membership.objects.get(group__slug="midnight-quill-guild", user__username="nora.kim").role, Membership.Role.MEMBER)
        self.assertEqual(Membership.objects.get(group__slug="midnight-quill-guild", user__username="jonah.vale").role, Membership.Role.MODERATOR)
        self.assertEqual(Membership.objects.get(group__slug="lantern-leaf-society", user__username="jonah.vale").role, Membership.Role.MEMBER)

        for month in ChallengeMonth.objects.filter(group__slug__in=demo_slugs):
            self.assertEqual(month.teams.count(), 2)
            self.assertGreaterEqual(month.themes.count(), 2)
            self.assertTrue(all(month.starts_on <= theme.starts_on <= theme.ends_on <= month.ends_on for theme in month.themes.all()))
            for assignment in TeamAssignment.objects.filter(month=month).select_related("team", "participant"):
                self.assertEqual(assignment.team.month_id, month.pk)
                self.assertEqual(assignment.participant.group_id, month.group_id)
                self.assertTrue(MonthEnrollment.objects.filter(month=month, participant=assignment.participant).exists())

        for month in ChallengeMonth.objects.filter(group__slug__in=demo_slugs, status=ChallengeMonth.Status.ACTIVE):
            active_enrollments = month.enrollments.filter(is_active=True).count()
            current_assignments = month.team_assignments.filter(ended_at__isnull=True).count()
            self.assertEqual(active_enrollments - current_assignments, 1)
            self.assertTrue(month.submissions.filter(status=BookSubmission.Status.PENDING).exists())
            self.assertTrue(ThemeClaim.objects.filter(submission__month=month, status=ThemeClaim.Status.PENDING).exists())

        lantern_history_team = TeamAssignment.objects.get(
            month__name="Summer Pages", participant__user__username="nora.kim"
        ).team.name
        lantern_current_team = TeamAssignment.objects.get(
            month__name="Stories Under the Stars", participant__user__username="nora.kim"
        ).team.name
        self.assertNotEqual(lantern_history_team, lantern_current_team)

        withdrawn = MonthEnrollment.objects.get(
            month__name="Stories Under the Stars",
            participant__user__username="jonah.vale",
        )
        self.assertFalse(withdrawn.is_active)
        self.assertEqual(withdrawn.inactive_reason, MonthEnrollment.InactiveReason.WITHDRAWN)
        self.assertTrue(
            TeamAssignment.objects.filter(
                month=withdrawn.month,
                participant=withdrawn.participant,
                ended_at__isnull=False,
            ).exists()
        )

        approved = BookSubmission.objects.filter(month__group__slug__in=demo_slugs, status=BookSubmission.Status.APPROVED)
        pending = BookSubmission.objects.filter(month__group__slug__in=demo_slugs, status=BookSubmission.Status.PENDING)
        self.assertGreater(approved.count(), 20)
        self.assertGreaterEqual(pending.count(), 4)
        for submission in approved:
            expected_bonus = sum(
                submission.theme_claims.filter(status=ThemeClaim.Status.APPROVED).values_list("approved_bonus_pages", flat=True)
            )
            self.assertEqual(submission.bonus_pages, expected_bonus)
            self.assertEqual(submission.final_scored_pages, submission.approved_pages + expected_bonus)
        for submission in pending:
            self.assertIsNone(submission.approved_pages)
            self.assertEqual(submission.bonus_pages, 0)
            self.assertIsNone(submission.final_scored_pages)

        self.assertEqual(CatalogBook.objects.filter(provider=DEMO_CATALOG_PROVIDER).count(), 20)
        self.assertFalse(Membership.objects.filter(user__is_superuser=True).exists())

    def test_representative_reader_owner_and_moderator_pages_load(self):
        self.seed()
        lantern = ReadingGroup.objects.get(slug="lantern-leaf-society")
        current = lantern.challenge_months.get(status=ChallengeMonth.Status.ACTIVE)

        self.assertTrue(self.client.login(username="nora.kim", password=DEMO_PASSWORD))
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)
        self.assertEqual(self.client.get(reverse("group-detail", kwargs={"group_slug": lantern.slug})).status_code, 200)
        self.assertEqual(self.client.get(reverse("month-detail", kwargs={"group_slug": lantern.slug, "pk": current.pk})).status_code, 200)
        self.assertEqual(self.client.get(reverse("my-stats")).status_code, 200)

        self.client.logout()
        self.assertTrue(self.client.login(username="priya.shah", password=DEMO_PASSWORD))
        queue = self.client.get(reverse("review-queue", kwargs={"group_slug": lantern.slug, "month_pk": current.pk}))
        self.assertEqual(queue.status_code, 200)
        self.assertContains(queue, "The Last Tea Shop on Alder Street")

        self.client.logout()
        self.assertTrue(self.client.login(username="maren.holt", password=DEMO_PASSWORD))
        self.assertEqual(self.client.get(reverse("participant-list", kwargs={"group_slug": lantern.slug})).status_code, 200)
