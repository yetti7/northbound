from datetime import date

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class BotmCompletionMigrationTests(TransactionTestCase):
    migrate_from = [("core", "0046_botm_matching_foundation")]
    migrate_to = [("core", "0047_botm_completion_awards")]

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
        BotmMatch = apps.get_model("core", "BotmMatch")
        ModifierProvenance = apps.get_model("core", "ModifierProvenance")
        user = User.objects.create(username="pre-completion-reader")
        group = ReadingGroup.objects.create(name="Pre-completion", slug="pre-completion")
        reader = Membership.objects.create(group=group, user=user, display_name="Reader")
        month = ChallengeMonth.objects.create(
            group=group, name="Existing Challenge", starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31), status="active", botm_enabled=True,
            botm_completion_bonus_pages=200,
        )
        MonthEnrollment.objects.create(month=month, participant=reader)
        book = BotmBook.objects.create(
            month=month, position=1, title_snapshot="Existing", author_snapshot="Author",
            page_count_snapshot=100, normalized_title="existing", normalized_author="author", bonus_pages=25,
        )
        submission = BookSubmission.objects.create(
            month=month, participant=reader, title="Existing", author="Author", book_format="ebook",
            completed_on=date(2026, 8, 10), submitted_pages=100, approved_pages=100,
            bonus_pages=25, final_scored_pages=125, status="approved",
        )
        match = BotmMatch.objects.create(
            botm_book=book, month=month, participant=reader, submission=submission,
            method="normalized_title_author", status="confirmed", is_qualifying=True,
            botm_title_snapshot="Existing", botm_author_snapshot="Author",
            submission_title_snapshot="Existing", submission_author_snapshot="Author",
        )
        provenance = ModifierProvenance.objects.create(
            month=month, participant=reader, submission=submission, source_type="botm_book",
            source_reference=f"botm_match:{match.pk}", source_label="Existing", amount=25,
            effective_date=date(2026, 8, 10), is_active=True,
        )
        return {"month": month.pk, "match": match.pk, "provenance": provenance.pk}

    def test_0047_preserves_existing_matches_and_rewards_without_fabricating_completion(self):
        BotmMatch = self.apps.get_model("core", "BotmMatch")
        ModifierProvenance = self.apps.get_model("core", "ModifierProvenance")
        BotmCompletionAward = self.apps.get_model("core", "BotmCompletionAward")
        BotmCompletionAwardBook = self.apps.get_model("core", "BotmCompletionAwardBook")
        self.assertTrue(BotmMatch.objects.filter(pk=self.ids["match"], is_qualifying=True).exists())
        provenance = ModifierProvenance.objects.get(pk=self.ids["provenance"])
        self.assertEqual((provenance.source_type, provenance.amount, provenance.is_active), ("botm_book", 25, True))
        self.assertFalse(BotmCompletionAward.objects.exists())
        self.assertFalse(BotmCompletionAwardBook.objects.exists())
