from datetime import date

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class PersonalTbrMatchingMigrationTests(TransactionTestCase):
    migrate_from = [("core", "0049_personaltbr_personaltbrbook_and_more")]
    migrate_to = [("core", "0050_personal_tbr_matching_foundation")]

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
        PersonalTBR = apps.get_model("core", "PersonalTBR")
        PersonalTBRBook = apps.get_model("core", "PersonalTBRBook")
        BookSubmission = apps.get_model("core", "BookSubmission")
        user = User.objects.create(username="pre-tbr-match-reader")
        group = ReadingGroup.objects.create(name="Pre-match TBR Group", slug="pre-match-tbr-group")
        reader = Membership.objects.create(group=group, user=user, display_name="Reader")
        month = ChallengeMonth.objects.create(
            group=group, name="Pre-match TBR Challenge", starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31), status="active", tbr_enabled=True,
        )
        enrollment = MonthEnrollment.objects.create(month=month, participant=reader)
        tbr = PersonalTBR.objects.create(enrollment=enrollment, confirmed_at="2026-08-01T00:00:00Z")
        book = PersonalTBRBook.objects.create(
            personal_tbr=tbr, position=1, title_snapshot="Existing TBR", author_snapshot="Author",
            normalized_title="existing tbr", normalized_author="author",
        )
        submission = BookSubmission.objects.create(
            month=month, participant=reader, title="Existing TBR", author="Author",
            book_format="ebook", completed_on=date(2026, 8, 10), submitted_pages=200,
            approved_pages=200, final_scored_pages=200, status="approved",
        )
        return {"book": book.pk, "submission": submission.pk}

    def test_0050_preserves_locked_tbr_and_submission_without_fabricating_matches(self):
        PersonalTBRBook = self.apps.get_model("core", "PersonalTBRBook")
        BookSubmission = self.apps.get_model("core", "BookSubmission")
        PersonalTBRMatch = self.apps.get_model("core", "PersonalTBRMatch")
        self.assertEqual(PersonalTBRBook.objects.get(pk=self.ids["book"]).title_snapshot, "Existing TBR")
        self.assertEqual(BookSubmission.objects.get(pk=self.ids["submission"]).status, "approved")
        self.assertFalse(PersonalTBRMatch.objects.exists())
