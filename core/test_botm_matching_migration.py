from datetime import date

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class BotmMatchingMigrationTests(TransactionTestCase):
    migrate_from = [("core", "0045_botm_schema_foundation")]
    migrate_to = [("core", "0046_botm_matching_foundation")]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        self.ids = self._create_existing_data(old_apps)
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def _create_existing_data(self, apps):
        User = apps.get_model("auth", "User")
        ReadingGroup = apps.get_model("core", "ReadingGroup")
        Membership = apps.get_model("core", "Membership")
        ChallengeMonth = apps.get_model("core", "ChallengeMonth")
        MonthEnrollment = apps.get_model("core", "MonthEnrollment")
        BotmBook = apps.get_model("core", "BotmBook")
        BookSubmission = apps.get_model("core", "BookSubmission")
        user = User.objects.create(username="pre-match-reader")
        group = ReadingGroup.objects.create(name="Pre-match Group", slug="pre-match-group")
        reader = Membership.objects.create(group=group, user=user, display_name="Reader")
        month = ChallengeMonth.objects.create(
            group=group, name="Pre-match Challenge", starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31), status="active", botm_enabled=True,
        )
        MonthEnrollment.objects.create(month=month, participant=reader)
        book = BotmBook.objects.create(
            month=month, position=1, title_snapshot="Existing BOTM", author_snapshot="Author",
            page_count_snapshot=200, normalized_title="existing botm", normalized_author="author",
        )
        submission = BookSubmission.objects.create(
            month=month, participant=reader, title="Existing BOTM", author="Author",
            book_format="ebook", completed_on=date(2026, 8, 10), submitted_pages=200,
            approved_pages=200, final_scored_pages=200, status="approved",
        )
        return {"book": book.pk, "submission": submission.pk}

    def test_0046_preserves_configuration_and_submission_without_fabricating_matches(self):
        BotmBook = self.apps.get_model("core", "BotmBook")
        BookSubmission = self.apps.get_model("core", "BookSubmission")
        BotmMatch = self.apps.get_model("core", "BotmMatch")
        self.assertEqual(BotmBook.objects.get(pk=self.ids["book"]).title_snapshot, "Existing BOTM")
        submission = BookSubmission.objects.get(pk=self.ids["submission"])
        self.assertEqual((submission.status, submission.final_scored_pages), ("approved", 200))
        self.assertFalse(BotmMatch.objects.exists())
