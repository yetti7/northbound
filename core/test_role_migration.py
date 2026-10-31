from datetime import date

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class GroupRoleMigrationTests(TransactionTestCase):
    migrate_from = [("core", "0026_platformsettings")]
    migrate_to = [("core", "0027_group_role_migration_and_compression")]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        self._create_legacy_records(old_apps)

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def _create_legacy_records(self, apps):
        User = apps.get_model("auth", "User")
        ReadingGroup = apps.get_model("core", "ReadingGroup")
        Membership = apps.get_model("core", "Membership")
        ChallengeMonth = apps.get_model("core", "ChallengeMonth")
        Team = apps.get_model("core", "Team")
        MonthEnrollment = apps.get_model("core", "MonthEnrollment")
        TeamAssignment = apps.get_model("core", "TeamAssignment")
        BookSubmission = apps.get_model("core", "BookSubmission")
        MonthTheme = apps.get_model("core", "MonthTheme")
        ThemeClaim = apps.get_model("core", "ThemeClaim")
        AuditEvent = apps.get_model("core", "AuditEvent")

        owner_user = User.objects.create(username="legacy-owner")
        admin_user = User.objects.create(username="legacy-admin")
        moderator_user = User.objects.create(username="legacy-moderator")
        game_manager_user = User.objects.create(username="legacy-game-manager")
        reader_user = User.objects.create(username="legacy-reader")
        group = ReadingGroup.objects.create(name="Legacy Group", slug="legacy-group")

        self.owner_id = Membership.objects.create(
            group=group, user=owner_user, role="owner", display_name="Owner"
        ).pk
        self.admin_id = Membership.objects.create(
            group=group,
            user=admin_user,
            role="admin",
            display_name="Administrator",
            permission_overrides={"manage_teams": False, "custom_legacy_key": True},
        ).pk
        self.moderator_id = Membership.objects.create(
            group=group,
            user=moderator_user,
            role="moderator",
            display_name="Moderator",
            permission_overrides={"review_submissions": False},
        ).pk
        self.game_manager_id = Membership.objects.create(
            group=group,
            user=game_manager_user,
            role="game_manager",
            display_name="Game Manager",
            is_active=False,
            permission_overrides={"view_hidden_stats": False},
        ).pk
        reader = Membership.objects.create(
            group=group,
            user=reader_user,
            role="reader",
            display_name="Reader",
            permission_overrides={"manage_announcements": True},
        )
        self.reader_id = reader.pk
        self.reader_joined_at = reader.joined_at

        month = ChallengeMonth.objects.create(
            group=group,
            name="Legacy Month",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            status="open",
        )
        team = Team.objects.create(month=month, name="Legacy Team")
        enrollment = MonthEnrollment.objects.create(
            month=month, participant=reader, enrolled_by=admin_user
        )
        assignment = TeamAssignment.objects.create(month=month, team=team, participant=reader)
        submission = BookSubmission.objects.create(
            month=month,
            participant=reader,
            title="Legacy Book",
            author="Legacy Author",
            book_format="ebook",
            completed_on=date(2026, 8, 10),
            submitted_pages=200,
            approved_pages=200,
            final_scored_pages=200,
            status="approved",
            reviewed_by=admin_user,
        )
        theme = MonthTheme.objects.create(
            month=month,
            name="Legacy Theme",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
        )
        claim = ThemeClaim.objects.create(
            submission=submission,
            theme=theme,
            status="approved",
            reviewed_by=admin_user,
        )
        audit = AuditEvent.objects.create(
            actor=admin_user,
            group=group,
            action="legacy.reviewed",
            object_type="BookSubmission",
            object_id=str(submission.pk),
            summary="Legacy review record",
        )
        self.relationship_ids = {
            "enrollment": enrollment.pk,
            "assignment": assignment.pk,
            "submission": submission.pk,
            "claim": claim.pk,
            "audit": audit.pk,
        }

    def test_legacy_roles_and_relationships_are_preserved(self):
        from core.models import AuditEvent, BookSubmission, Membership, MonthEnrollment, TeamAssignment, ThemeClaim
        from core.permissions import membership_has_capability

        owner = Membership.objects.get(pk=self.owner_id)
        admin = Membership.objects.get(pk=self.admin_id)
        moderator = Membership.objects.get(pk=self.moderator_id)
        game_manager = Membership.objects.get(pk=self.game_manager_id)
        reader = Membership.objects.get(pk=self.reader_id)

        self.assertEqual(owner.role, Membership.Role.OWNER)
        self.assertEqual(moderator.role, Membership.Role.MODERATOR)
        self.assertEqual(admin.role, Membership.Role.MODERATOR)
        self.assertEqual(game_manager.role, Membership.Role.MEMBER)
        self.assertEqual(reader.role, Membership.Role.MEMBER)

        self.assertFalse(game_manager.is_active)
        self.assertEqual(reader.joined_at, self.reader_joined_at)
        self.assertEqual(game_manager.permission_overrides, {"view_hidden_stats": False})
        self.assertEqual(reader.permission_overrides, {"manage_announcements": True})
        self.assertEqual(moderator.permission_overrides, {"review_submissions": False})

        self.assertFalse(admin.permission_overrides["manage_teams"])
        self.assertTrue(admin.permission_overrides["custom_legacy_key"])
        for capability in ("manage_group_settings", "manage_participants", "manage_months"):
            self.assertTrue(admin.permission_overrides[capability])
            self.assertTrue(membership_has_capability(admin, capability))
        self.assertTrue(membership_has_capability(admin, "manage_announcements"))
        self.assertFalse(membership_has_capability(admin, "review_submissions"))
        self.assertFalse(membership_has_capability(admin, "view_hidden_stats"))
        self.assertFalse(membership_has_capability(admin, "manage_teams"))
        self.assertFalse(membership_has_capability(admin, "remove_content"))
        self.assertFalse(membership_has_capability(admin, "manage_permissions"))

        self.assertTrue(MonthEnrollment.objects.filter(pk=self.relationship_ids["enrollment"], participant=reader).exists())
        self.assertTrue(TeamAssignment.objects.filter(pk=self.relationship_ids["assignment"], participant=reader).exists())
        self.assertTrue(BookSubmission.objects.filter(pk=self.relationship_ids["submission"], participant=reader).exists())
        self.assertTrue(ThemeClaim.objects.filter(pk=self.relationship_ids["claim"]).exists())
        self.assertTrue(AuditEvent.objects.filter(pk=self.relationship_ids["audit"]).exists())
