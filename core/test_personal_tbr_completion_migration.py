from datetime import date

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class PersonalTbrCompletionMigrationTests(TransactionTestCase):
    migrate_from = [("core", "0050_personal_tbr_matching_foundation")]
    migrate_to = [("core", "0051_personal_tbr_completion_awards")]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection); executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        self.ids = self._create_existing_data(old_apps)
        executor = MigrationExecutor(connection); executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

    def tearDown(self):
        executor = MigrationExecutor(connection); executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def _create_existing_data(self, apps):
        User = apps.get_model("auth", "User")
        ReadingGroup = apps.get_model("core", "ReadingGroup")
        Membership = apps.get_model("core", "Membership")
        ChallengeMonth = apps.get_model("core", "ChallengeMonth")
        MonthEnrollment = apps.get_model("core", "MonthEnrollment")
        PersonalTBR = apps.get_model("core", "PersonalTBR")
        PersonalTBRBook = apps.get_model("core", "PersonalTBRBook")
        user = User.objects.create(username="pre-tbr-completion-reader")
        group = ReadingGroup.objects.create(name="Pre-completion Group", slug="pre-completion-group")
        reader = Membership.objects.create(group=group, user=user, display_name="Reader")
        month = ChallengeMonth.objects.create(
            group=group, name="Pre-completion Challenge", starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31), status="active", tbr_enabled=True,
        )
        enrollment = MonthEnrollment.objects.create(month=month, participant=reader)
        tbr = PersonalTBR.objects.create(enrollment=enrollment, confirmed_at="2026-08-01T00:00:00Z")
        book = PersonalTBRBook.objects.create(
            personal_tbr=tbr, position=1, title_snapshot="Existing TBR", author_snapshot="Author",
            normalized_title="existing tbr", normalized_author="author",
        )
        return {"book": book.pk}

    def test_0051_preserves_existing_tbr_without_fabricating_completion_history(self):
        PersonalTBRBook = self.apps.get_model("core", "PersonalTBRBook")
        PersonalTBRCompletionAward = self.apps.get_model("core", "PersonalTBRCompletionAward")
        self.assertEqual(PersonalTBRBook.objects.get(pk=self.ids["book"]).title_snapshot, "Existing TBR")
        self.assertFalse(PersonalTBRCompletionAward.objects.exists())
