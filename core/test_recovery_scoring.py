from datetime import date

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .botm_matching import synchronize_reader as synchronize_botm_reader
from .models import (
    BookSubmission, BotmBook, BotmCompletionAward, BotmMatch, CatalogBook,
    ChallengeMonth, ChallengeStaffAssignment, Membership, ModifierProvenance,
    MonthEnrollment, MonthTheme, PersonalTBR, PersonalTBRBook,
    PersonalTBRMatch, ReadingGroup, RecoveryOperation, Team, TeamAssignment,
    ThemeClaim,
)
from .recovery import RecoveryRequest
from .recovery_scoring import (
    correct_unused_theme, purge_malformed_provenance, purge_submission,
    rebuild_provenance, recover_theme_claim, set_submission_removed,
    set_theme_active, submission_purge_impact, submission_recovery_label,
    void_provenance,
)
from .score_aggregation import challenge_score_totals
from .scoring import refresh_submission_score


class RecoveryScoringTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.root = User.objects.create_superuser(
            "scoring-recovery-owner", password="owner-password-481!",
        )
        self.reader_user = User.objects.create_user("scoring-reader", password="test-password")
        self.owner_user = User.objects.create_user("group-owner", password="test-password")
        self.moderator_user = User.objects.create_user("moderator", password="test-password")
        self.host_user = User.objects.create_user("host", password="test-password")
        self.leader_user = User.objects.create_user("leader", password="test-password")
        self.floater_user = User.objects.create_user("floater", password="test-password")
        self.outsider_user = User.objects.create_user("ordinary-account", password="test-password")
        self.group = ReadingGroup.objects.create(name="Scoring Recovery Group", slug="scoring-recovery")

        def member(user, name, role=Membership.Role.MEMBER):
            return Membership.objects.create(group=self.group, user=user, display_name=name, role=role)

        self.reader = member(self.reader_user, "Scoring Reader")
        self.group_owner = member(self.owner_user, "Group Owner", Membership.Role.OWNER)
        self.moderator = member(self.moderator_user, "Moderator", Membership.Role.MODERATOR)
        self.host = member(self.host_user, "Host")
        self.leader = member(self.leader_user, "Leader")
        self.floater = member(self.floater_user, "Floater")
        self.month = ChallengeMonth.objects.create(
            group=self.group, name="August Recovery Challenge",
            starts_on=date(2026, 8, 1), ends_on=date(2026, 8, 31),
            status=ChallengeMonth.Status.ACTIVE, botm_enabled=True,
            botm_completion_bonus_pages=200,
        )
        self.enrollment = MonthEnrollment.objects.create(
            month=self.month, participant=self.reader, enrolled_by=self.owner_user,
        )
        self.team = Team.objects.create(month=self.month, name="Recovery Team")
        TeamAssignment.objects.create(month=self.month, participant=self.reader, team=self.team)
        ChallengeStaffAssignment.objects.create(
            month=self.month, membership=self.host,
            role=ChallengeStaffAssignment.Role.HOST,
        )
        self.theme = MonthTheme.objects.create(bonus_rule="legacy_fixed",
            month=self.month, name="Recovery Theme",
            starts_on=self.month.starts_on, ends_on=self.month.ends_on,
            bonus_pages=50,
        )
        self.catalog = CatalogBook.objects.create(
            provider="hardcover", provider_book_id="recovery-work",
            title="Recovery Book", author="Writer",
        )
        self.submission = BookSubmission.objects.create(
            month=self.month, participant=self.reader, catalog_book=self.catalog,
            title="Recovery Book", author="Writer",
            book_format=BookSubmission.Format.EBOOK,
            completed_on=date(2026, 8, 10), submitted_pages=300,
            approved_pages=300, status=BookSubmission.Status.APPROVED,
        )
        self.claim = ThemeClaim.objects.create(
            submission=self.submission, theme=self.theme,
            status=ThemeClaim.Status.APPROVED, approved_bonus_pages=50,
            reviewed_by=self.host_user, reviewed_at=timezone.now(),
        )
        refresh_submission_score(self.submission)

    def request(self, *, action, target, tier=2, reason="Repair test data", password=""):
        return RecoveryRequest(
            actor=self.root, tier=tier, action=action,
            target_type=target.__class__.__name__, target_id=str(target.pk),
            target_label=(submission_recovery_label(target) if isinstance(target, BookSubmission) else str(target)),
            reason=reason,
            required_confirmation=(submission_recovery_label(target) if isinstance(target, BookSubmission) else str(target)),
            supplied_confirmation=(submission_recovery_label(target) if isinstance(target, BookSubmission) else str(target)),
            current_password=password,
            confirmation_method="exact target label" + (" plus current password" if tier == 3 else ""),
            group=self.group, challenge=self.month,
        )

    def score(self):
        return challenge_score_totals(
            month=self.month, participant_ids=[self.reader.pk],
        )[self.reader.pk]

    def test_platform_owner_soft_remove_and_restore_reconcile_score_and_history(self):
        self.assertEqual(self.score(), {"base_pages": 300, "modifier_pages": 50, "total_pages": 350})
        set_submission_removed(
            submission=self.submission, removed=True,
            recovery_request=self.request(action="submission.soft_remove", target=self.submission),
        )
        self.submission.refresh_from_db()
        self.assertTrue(self.submission.is_removed)
        self.assertEqual((self.submission.bonus_pages, self.submission.final_scored_pages), (0, None))
        self.assertEqual(self.score(), {"base_pages": 0, "modifier_pages": 0, "total_pages": 0})
        self.assertFalse(self.submission.modifier_provenance.get(
            source_type=ModifierProvenance.SourceType.THEME_BONUS,
        ).is_active)

        set_submission_removed(
            submission=self.submission, removed=False,
            recovery_request=self.request(action="submission.restore", target=self.submission),
        )
        self.submission.refresh_from_db()
        self.assertFalse(self.submission.is_removed)
        self.assertEqual(self.score(), {"base_pages": 300, "modifier_pages": 50, "total_pages": 350})
        self.assertEqual(
            RecoveryOperation.objects.filter(target_id=str(self.submission.pk), result="succeeded").count(), 2,
        )

    def test_submission_purge_preview_counts_theme_botm_tbr_provenance_and_history(self):
        botm = BotmBook.objects.create(
            month=self.month, position=1, catalog_book=self.catalog,
            title_snapshot="Recovery Book", author_snapshot="Writer",
            page_count_snapshot=300, bonus_pages=25,
        )
        synchronize_botm_reader(month=self.month, participant=self.reader)
        tbr = PersonalTBR.objects.create(enrollment=self.enrollment)
        tbr_book = PersonalTBRBook.objects.create(
            personal_tbr=tbr, position=1, catalog_book=self.catalog,
            title_snapshot="Recovery Book", author_snapshot="Writer",
        )
        PersonalTBR.objects.filter(pk=tbr.pk).update(confirmed_at=timezone.now())
        tbr.refresh_from_db()
        match = PersonalTBRMatch.objects.create(
            personal_tbr_book=tbr_book, month=self.month, participant=self.reader,
            submission=self.submission, method=PersonalTBRMatch.Method.CATALOG_WORK,
            status=PersonalTBRMatch.Status.CONFIRMED, is_qualifying=True,
            tbr_title_snapshot="Recovery Book", tbr_author_snapshot="Writer",
            submission_title_snapshot="Recovery Book", submission_author_snapshot="Writer",
        )
        ModifierProvenance.objects.create(
            month=self.month, participant=self.reader, submission=self.submission,
            source_type=ModifierProvenance.SourceType.TBR_BOOK,
            source_reference=f"personal_tbr_match:{match.pk}", source_label="Recovery Book",
            amount=10, effective_date=self.submission.completed_on,
        )
        impact = {item.label: item.count for item in submission_purge_impact(self.submission).items}
        self.assertEqual(impact["Theme claims"], 1)
        self.assertEqual(impact["Theme modifier provenance"], 1)
        self.assertEqual(impact["BOTM matches"], 1)
        self.assertEqual(impact["BOTM per-book provenance"], 1)
        self.assertEqual(impact["Personal TBR matches"], 1)
        self.assertEqual(impact["Personal TBR per-book provenance"], 1)
        self.assertEqual(impact["Completion awards potentially affected"], 1)
        self.assertEqual(botm.month, self.month)

    def test_submission_hard_purge_reconciles_dependencies_and_preserves_shared_and_unrelated_data(self):
        BotmBook.objects.create(
            month=self.month, position=1, catalog_book=self.catalog,
            title_snapshot="Recovery Book", author_snapshot="Writer",
            page_count_snapshot=300, bonus_pages=25,
        )
        synchronize_botm_reader(month=self.month, participant=self.reader)
        self.assertTrue(BotmCompletionAward.objects.filter(month=self.month, participant=self.reader, is_qualifying=True).exists())
        other = BookSubmission.objects.create(
            month=self.month, participant=self.reader, title="Other Book", author="Writer",
            book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 8, 12),
            submitted_pages=100, approved_pages=100, status=BookSubmission.Status.APPROVED,
        )
        operation = purge_submission(
            submission=self.submission,
            recovery_request=self.request(
                action="submission.purge", target=self.submission, tier=3,
                password="owner-password-481!",
            ),
        )
        self.assertFalse(BookSubmission.objects.filter(pk=self.submission.pk).exists())
        self.assertTrue(BookSubmission.objects.filter(pk=other.pk).exists())
        self.assertTrue(Membership.objects.filter(pk=self.reader.pk).exists())
        self.assertTrue(ChallengeMonth.objects.filter(pk=self.month.pk).exists())
        self.assertTrue(CatalogBook.objects.filter(pk=self.catalog.pk).exists())
        self.assertTrue(RecoveryOperation.objects.filter(pk=operation.pk).exists())
        award = BotmCompletionAward.objects.get(month=self.month, participant=self.reader)
        self.assertFalse(award.is_qualifying)
        self.assertEqual(self.score()["total_pages"], 100)

    def test_submission_purge_retains_completion_when_alternate_submission_qualifies(self):
        BotmBook.objects.create(
            month=self.month, position=1, catalog_book=self.catalog,
            title_snapshot="Recovery Book", author_snapshot="Writer",
            page_count_snapshot=300, bonus_pages=25,
        )
        alternate = BookSubmission.objects.create(
            month=self.month, participant=self.reader, catalog_book=self.catalog,
            title="Recovery Book Alternate", author="Writer",
            book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 8, 11),
            submitted_pages=200, approved_pages=200, status=BookSubmission.Status.APPROVED,
        )
        synchronize_botm_reader(month=self.month, participant=self.reader)
        purge_submission(
            submission=self.submission,
            recovery_request=self.request(
                action="submission.purge", target=self.submission, tier=3,
                password="owner-password-481!",
            ),
        )
        award = BotmCompletionAward.objects.get(month=self.month, participant=self.reader)
        self.assertTrue(award.is_qualifying)
        self.assertTrue(BotmMatch.objects.filter(submission=alternate, is_qualifying=True).exists())

    def test_submission_purge_failure_rolls_back_submission_sources_scores_and_has_no_false_success(self):
        before = self.score()
        request = self.request(
            action="submission.purge", target=self.submission, tier=3,
            password="owner-password-481!",
        )
        with self.assertRaises(RuntimeError):
            purge_submission(
                submission=self.submission, recovery_request=request,
                fail_after_step="provenance",
            )
        self.submission.refresh_from_db()
        self.assertFalse(self.submission.is_removed)
        self.assertTrue(ThemeClaim.objects.filter(pk=self.claim.pk).exists())
        self.assertEqual(self.score(), before)
        operation = RecoveryOperation.objects.get(operation_id=request.operation_id)
        self.assertEqual(operation.result, RecoveryOperation.Result.FAILED)
        self.assertFalse(RecoveryOperation.objects.filter(
            operation_id=request.operation_id, result=RecoveryOperation.Result.SUCCEEDED,
        ).exists())

    def test_submission_purge_tier_three_validation_blocks_reason_confirmation_and_password(self):
        url = reverse("recovery-submission-purge", kwargs={"pk": self.submission.pk})
        label = submission_recovery_label(self.submission)
        self.client.force_login(self.root)
        for payload in (
            {"reason": "", "confirmation": label, "current_password": "owner-password-481!"},
            {"reason": "Needed", "confirmation": "wrong", "current_password": "owner-password-481!"},
            {"reason": "Needed", "confirmation": label, "current_password": "wrong"},
        ):
            with self.subTest(payload=payload):
                response = self.client.post(url, payload)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(BookSubmission.objects.filter(pk=self.submission.pk).exists())

    def test_theme_retire_reactivate_and_unused_correction_preserve_claim_history(self):
        set_theme_active(
            theme=self.theme, active=False,
            recovery_request=self.request(action="theme.retire", target=self.theme),
        )
        self.theme.refresh_from_db()
        self.assertFalse(self.theme.is_active)
        self.assertTrue(ThemeClaim.objects.filter(pk=self.claim.pk).exists())
        set_theme_active(
            theme=self.theme, active=True,
            recovery_request=self.request(action="theme.reactivate", target=self.theme),
        )
        unused = MonthTheme.objects.create(bonus_rule="legacy_fixed",
            month=self.month, name="Unused", starts_on=self.month.starts_on,
            ends_on=self.month.ends_on, bonus_pages=10,
        )
        correct_unused_theme(
            theme=unused,
            values={
                "name": "Corrected Unused", "description": "Corrected",
                "starts_on": self.month.starts_on, "ends_on": self.month.ends_on,
                "bonus_pages": 20, "allow_stacking": True, "prompt": "",
                "is_visible": True,
            },
            recovery_request=self.request(action="theme.correct_unused", target=unused),
        )
        unused.refresh_from_db()
        self.assertEqual(unused.name, "Corrected Unused")
        with self.assertRaises(ValidationError):
            correct_unused_theme(
                theme=self.theme, values={"name": "Forbidden"},
                recovery_request=self.request(action="theme.correct_unused", target=self.theme),
            )

    def test_claim_recovery_approval_rejection_deduplication_and_wrong_amount_rebuild(self):
        canonical = self.submission.modifier_provenance.get(
            source_type=ModifierProvenance.SourceType.THEME_BONUS,
        )
        ModifierProvenance.objects.create(
            month=self.month, participant=self.reader, submission=self.submission,
            source_type=ModifierProvenance.SourceType.THEME_BONUS,
            source_reference="malformed-duplicate", source_label=self.theme.name,
            source_context=f"Approved Theme claim {self.claim.pk}", amount=999,
            effective_date=self.submission.completed_on,
        )
        ModifierProvenance.objects.filter(pk=canonical.pk).update(amount=999)
        recover_theme_claim(
            claim=self.claim, status=ThemeClaim.Status.APPROVED, force_rebuild=True,
            recovery_request=self.request(action="theme_claim.reconcile", target=self.claim),
        )
        active = ModifierProvenance.objects.filter(
            submission=self.submission, source_type=ModifierProvenance.SourceType.THEME_BONUS,
            is_active=True,
        )
        self.assertEqual(active.count(), 1)
        self.assertEqual(active.get().amount, 50)
        self.assertFalse(ModifierProvenance.objects.filter(source_reference="malformed-duplicate").exists())
        recover_theme_claim(
            claim=self.claim, status=ThemeClaim.Status.REJECTED,
            recovery_request=self.request(action="theme_claim.reconcile", target=self.claim),
        )
        self.assertFalse(ModifierProvenance.objects.get(
            submission=self.submission,
            source_type=ModifierProvenance.SourceType.THEME_BONUS,
        ).is_active)
        self.assertEqual(self.score(), {"base_pages": 300, "modifier_pages": 0, "total_pages": 300})
        self.theme.bonus_pages = 80
        self.theme.save(update_fields=["bonus_pages"])
        recover_theme_claim(
            claim=self.claim, status=ThemeClaim.Status.APPROVED,
            recovery_request=self.request(action="theme_claim.reconcile", target=self.claim),
        )
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.approved_bonus_pages, 50)

    def test_provenance_void_restore_orphan_purge_and_later_sources_fail_closed(self):
        recover_theme_claim(
            claim=self.claim, status=ThemeClaim.Status.REJECTED,
            recovery_request=self.request(action="theme_claim.reconcile", target=self.claim),
        )
        provenance = self.submission.modifier_provenance.get(
            source_type=ModifierProvenance.SourceType.THEME_BONUS,
        )
        void_provenance(
            provenance=provenance,
            recovery_request=self.request(action="provenance.void", target=provenance),
        )
        recover_theme_claim(
            claim=self.claim, status=ThemeClaim.Status.APPROVED,
            recovery_request=self.request(action="theme_claim.reconcile", target=self.claim),
        )
        provenance = self.submission.modifier_provenance.get(
            source_type=ModifierProvenance.SourceType.THEME_BONUS,
        )
        rebuild_provenance(
            provenance=provenance,
            recovery_request=self.request(action="provenance.rebuild", target=provenance),
        )
        self.assertEqual(self.submission.modifier_provenance.filter(is_active=True).get().amount, 50)

        orphan = ModifierProvenance.objects.create(
            month=self.month, participant=self.reader, submission=self.submission,
            source_type=ModifierProvenance.SourceType.THEME_BONUS,
            source_reference="theme_claim:999999", source_label="Orphan",
            amount=10, effective_date=self.submission.completed_on,
        )
        purge_malformed_provenance(
            provenance=orphan,
            recovery_request=self.request(
                action="provenance.purge", target=orphan, tier=3,
                password="owner-password-481!",
            ),
        )
        self.assertFalse(ModifierProvenance.objects.filter(pk=orphan.pk).exists())

        later = ModifierProvenance.objects.create(
            month=self.month, participant=self.reader, submission=self.submission,
            source_type=ModifierProvenance.SourceType.BOTM_BOOK,
            source_reference="botm_match:999999", source_label="Later Domain",
            amount=10, effective_date=self.submission.completed_on,
        )
        with self.assertRaises(ValidationError):
            rebuild_provenance(
                provenance=later,
                recovery_request=self.request(action="provenance.rebuild", target=later),
            )

    def test_recovery_ui_has_no_arbitrary_amount_field_and_all_normal_roles_are_denied(self):
        self.client.force_login(self.root)
        for route in (
            reverse("advanced-recovery"),
            reverse("recovery-submission-list"),
            reverse("recovery-submission-detail", kwargs={"pk": self.submission.pk}),
            reverse("recovery-theme-list"),
            reverse("recovery-theme-detail", kwargs={"pk": self.theme.pk}),
            reverse("recovery-theme-correct", kwargs={"pk": self.theme.pk}),
            reverse("recovery-theme-claim", kwargs={"pk": self.claim.pk}),
            reverse("recovery-provenance-list"),
        ):
            with self.subTest(platform_owner_route=route):
                self.assertEqual(self.client.get(route).status_code, 200)
        detail = self.client.get(reverse("recovery-provenance-detail", kwargs={
            "pk": self.submission.modifier_provenance.get().pk,
        }))
        self.assertContains(detail, "No arbitrary editing")
        self.assertNotContains(detail, 'name="amount"')
        routes = (
            reverse("recovery-submission-list"),
            reverse("recovery-submission-detail", kwargs={"pk": self.submission.pk}),
            reverse("recovery-submission-status", kwargs={"pk": self.submission.pk}),
            reverse("recovery-submission-purge", kwargs={"pk": self.submission.pk}),
            reverse("recovery-theme-list"),
            reverse("recovery-theme-detail", kwargs={"pk": self.theme.pk}),
            reverse("recovery-theme-status", kwargs={"pk": self.theme.pk}),
            reverse("recovery-theme-correct", kwargs={"pk": self.theme.pk}),
            reverse("recovery-theme-claim", kwargs={"pk": self.claim.pk}),
            reverse("recovery-provenance-list"),
            reverse("recovery-provenance-detail", kwargs={"pk": self.submission.modifier_provenance.get().pk}),
            reverse("recovery-provenance-void", kwargs={"pk": self.submission.modifier_provenance.get().pk}),
            reverse("recovery-provenance-rebuild", kwargs={"pk": self.submission.modifier_provenance.get().pk}),
            reverse("recovery-provenance-purge", kwargs={"pk": self.submission.modifier_provenance.get().pk}),
        )
        for user in (
            self.owner_user, self.moderator_user, self.host_user,
            self.leader_user, self.floater_user, self.reader_user, self.outsider_user,
        ):
            self.client.force_login(user)
            for route in routes:
                with self.subTest(user=user.username, route=route):
                    self.assertEqual(self.client.get(route).status_code, 403)
        self.assertEqual(Membership.objects.filter(user=self.root).count(), 0)
