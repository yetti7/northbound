from datetime import date, timedelta
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .botm_matching import synchronize_reader as synchronize_botm_reader
from .game_rewards import apply_game_reward
from .models import (
    BookSubmission, BotmBook, BotmCompletionAward, BotmMatch, CatalogBook,
    ChallengeMonth, ChallengeStaffAssignment, Game, GameRewardApplication,
    GameRewardRecipient, Membership, ModifierProvenance, MonthEnrollment,
    PersonalTBR, PersonalTBRBook, PersonalTBRCompletionAward,
    PersonalTBRMatch, ReadingGroup, RecoveryOperation, Team, TeamAssignment,
)
from .personal_tbr_matching import synchronize_reader as synchronize_tbr_reader
from .recovery import RecoveryRequest
from .recovery_modifiers import (
    botm_match_label, game_application_label, purge_botm_match,
    rebuild_locked_tbr, recover_botm_match, recover_game_application,
    recover_tbr_match, repair_tbr_entry, set_botm_book_retired,
    recreate_game_application, set_game_active, tbr_label, tbr_match_label,
)
from .score_aggregation import challenge_score_totals


class ModifierDomainRecoveryTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.root = User.objects.create_superuser("modifier-root", password="root-password-581!")
        self.reader_user = User.objects.create_user("modifier-reader", password="test-password")
        self.owner_user = User.objects.create_user("modifier-group-owner", password="test-password")
        self.moderator_user = User.objects.create_user("modifier-moderator", password="test-password")
        self.host_user = User.objects.create_user("modifier-host", password="test-password")
        self.leader_user = User.objects.create_user("modifier-leader", password="test-password")
        self.floater_user = User.objects.create_user("modifier-floater", password="test-password")
        self.outsider = User.objects.create_user("modifier-outsider", password="test-password")
        self.group = ReadingGroup.objects.create(name="Modifier Recovery Group", slug="modifier-recovery")

        def member(user, name, role=Membership.Role.MEMBER):
            return Membership.objects.create(group=self.group, user=user, display_name=name, role=role)

        self.reader = member(self.reader_user, "Recovery Reader")
        self.group_owner = member(self.owner_user, "Owner", Membership.Role.OWNER)
        self.moderator = member(self.moderator_user, "Moderator", Membership.Role.MODERATOR)
        self.host = member(self.host_user, "Host")
        self.leader = member(self.leader_user, "Leader")
        self.floater = member(self.floater_user, "Floater")
        self.month = ChallengeMonth.objects.create(
            group=self.group, name="Modifier Recovery Challenge",
            starts_on=date(2026, 8, 1), ends_on=date(2026, 8, 31),
            status=ChallengeMonth.Status.ACTIVE, botm_enabled=True,
            botm_completion_bonus_pages=200, tbr_enabled=True,
            tbr_book_bonus_pages=30, tbr_completion_bonus_pages=900,
            games_enabled=True,
        )
        self.enrollment = MonthEnrollment.objects.create(month=self.month, participant=self.reader)
        self.team = Team.objects.create(month=self.month, name="Recovery Team")
        TeamAssignment.objects.create(month=self.month, participant=self.reader, team=self.team)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.host, role=ChallengeStaffAssignment.Role.HOST)
        self.catalog_one = CatalogBook.objects.create(provider="hardcover", provider_book_id="mod-one", title="Stacked Book", author="Writer")
        self.catalog_two = CatalogBook.objects.create(provider="hardcover", provider_book_id="mod-two", title="Second Book", author="Writer")
        self.submission = self.make_submission(self.catalog_one, "Stacked Book", 300, date(2026, 8, 5))
        self.second_submission = self.make_submission(self.catalog_two, "Second Book", 200, date(2026, 8, 6))
        self.botm_one = BotmBook.objects.create(
            month=self.month, position=1, catalog_book=self.catalog_one,
            title_snapshot="Stacked Book", author_snapshot="Writer", page_count_snapshot=300, bonus_pages=20,
        )
        self.botm_two = BotmBook.objects.create(
            month=self.month, position=2, catalog_book=self.catalog_two,
            title_snapshot="Second Book", author_snapshot="Writer", page_count_snapshot=200, bonus_pages=20,
        )
        self.tbr = PersonalTBR.objects.create(enrollment=self.enrollment)
        self.tbr_one = PersonalTBRBook.objects.create(
            personal_tbr=self.tbr, position=1, catalog_book=self.catalog_one,
            title_snapshot="Stacked Book", author_snapshot="Writer", page_count_snapshot=300,
        )
        self.tbr_two = PersonalTBRBook.objects.create(
            personal_tbr=self.tbr, position=2, catalog_book=self.catalog_two,
            title_snapshot="Second Book", author_snapshot="Writer", page_count_snapshot=200,
        )
        PersonalTBR.objects.filter(pk=self.tbr.pk).update(confirmed_at=timezone.now())
        self.tbr.refresh_from_db()
        synchronize_botm_reader(month=self.month, participant=self.reader)
        synchronize_tbr_reader(month=self.month, participant=self.reader)
        self.botm_match = BotmMatch.objects.get(submission=self.submission, botm_book=self.botm_one)
        self.tbr_match = PersonalTBRMatch.objects.get(submission=self.submission, personal_tbr_book=self.tbr_one)
        self.game = Game.objects.create(
            month=self.month, name="Recovery Game", starts_at=timezone.now(),
            ends_at=timezone.now() + timedelta(hours=1), advertised_bonus_pages=100,
        )
        self.application, _ = apply_game_reward(
            game=self.game, actor=self.root, target_type=GameRewardApplication.TargetType.READER,
            target_participant=self.reader, amount=40, reason="Recovery fixture", idempotency_key=uuid4(),
        )

    def make_submission(self, catalog, title, pages, completed_on):
        return BookSubmission.objects.create(
            month=self.month, participant=self.reader, catalog_book=catalog,
            title=title, author="Writer", book_format=BookSubmission.Format.EBOOK,
            completed_on=completed_on, submitted_pages=pages, approved_pages=pages,
            status=BookSubmission.Status.APPROVED,
        )

    def request(self, *, action, target, label, tier=2, password=""):
        return RecoveryRequest(
            actor=self.root, tier=tier, action=action,
            target_type=target.__class__.__name__, target_id=str(target.pk), target_label=label,
            reason="Repair malformed modifier-domain state", required_confirmation=label,
            supplied_confirmation=label, current_password=password,
            confirmation_method="exact target label" + (" plus current password" if tier == 3 else ""),
            group=self.group, challenge=self.month,
        )

    def score(self):
        return challenge_score_totals(month=self.month, participant_ids=[self.reader.pk])[self.reader.pk]

    def test_botm_match_reopen_confirm_retarget_and_provenance_reconcile_preserves_tbr_stack(self):
        label = botm_match_label(self.botm_match)
        self.assertTrue(ModifierProvenance.objects.filter(source_reference=f"botm_match:{self.botm_match.pk}", is_active=True).exists())
        self.assertTrue(ModifierProvenance.objects.filter(source_reference=f"personal_tbr_match:{self.tbr_match.pk}", is_active=True).exists())
        recover_botm_match(
            match=self.botm_match, decision="pending", target_book=self.botm_one,
            recovery_request=self.request(action="botm_match.recover", target=self.botm_match, label=label),
        )
        self.botm_match.refresh_from_db()
        self.assertEqual(self.botm_match.status, BotmMatch.Status.PENDING_REVIEW)
        self.assertFalse(ModifierProvenance.objects.get(source_reference=f"botm_match:{self.botm_match.pk}").is_active)
        self.assertTrue(ModifierProvenance.objects.get(source_reference=f"personal_tbr_match:{self.tbr_match.pk}").is_active)
        competing = BotmMatch.objects.get(botm_book=self.botm_two, submission=self.second_submission)
        recover_botm_match(
            match=competing, decision="rejected", target_book=self.botm_two,
            recovery_request=self.request(
                action="botm_match.recover", target=competing,
                label=botm_match_label(competing),
            ),
        )
        recover_botm_match(
            match=self.botm_match, decision="confirmed", target_book=self.botm_two,
            recovery_request=self.request(action="botm_match.recover", target=self.botm_match, label=label),
        )
        replacement = BotmMatch.objects.get(botm_book=self.botm_two, submission=self.submission)
        self.assertTrue(replacement.is_qualifying)
        self.assertEqual(ModifierProvenance.objects.filter(source_reference=f"botm_match:{replacement.pk}", is_active=True).count(), 1)

    def test_botm_rejection_deactivates_completion_and_alternate_qualification_can_restore_it(self):
        self.botm_two.is_retired = True
        self.botm_two.save(update_fields=["is_retired"])
        synchronize_botm_reader(month=self.month, participant=self.reader)
        award = BotmCompletionAward.objects.filter(
            month=self.month, participant=self.reader, configured_book_count=1,
        ).latest("pk")
        self.assertTrue(award.is_qualifying)
        recover_botm_match(
            match=self.botm_match, decision="rejected", target_book=self.botm_one,
            recovery_request=self.request(action="botm_match.recover", target=self.botm_match, label=botm_match_label(self.botm_match)),
        )
        award.refresh_from_db()
        self.assertFalse(award.is_qualifying)
        alternate = self.make_submission(self.catalog_one, "Alternate Stacked Book", 150, date(2026, 8, 7))
        synchronize_botm_reader(month=self.month, participant=self.reader)
        award.refresh_from_db()
        self.assertTrue(award.is_qualifying)
        self.assertTrue(BotmMatch.objects.filter(submission=alternate, is_qualifying=True).exists())

    def test_botm_book_retire_reactivate_and_historical_delete_guard(self):
        set_botm_book_retired(
            book=self.botm_one, retired=True,
            recovery_request=self.request(action="botm_book.retire", target=self.botm_one, label=str(self.botm_one)),
        )
        self.botm_one.refresh_from_db()
        self.assertTrue(self.botm_one.is_retired)
        set_botm_book_retired(
            book=self.botm_one, retired=False,
            recovery_request=self.request(action="botm_book.reactivate", target=self.botm_one, label=str(self.botm_one)),
        )
        with self.assertRaises(ValidationError):
            self.botm_one.delete()

    def test_botm_match_purge_failure_rolls_back_match_and_provenance(self):
        request = self.request(
            action="botm_match.purge", target=self.botm_match,
            label=botm_match_label(self.botm_match), tier=3, password="root-password-581!",
        )
        with self.assertRaises(RuntimeError):
            purge_botm_match(match=self.botm_match, recovery_request=request, fail_after_step="provenance")
        self.assertTrue(BotmMatch.objects.filter(pk=self.botm_match.pk).exists())
        self.assertTrue(ModifierProvenance.objects.filter(source_reference=f"botm_match:{self.botm_match.pk}").exists())
        self.assertEqual(RecoveryOperation.objects.get(operation_id=request.operation_id).result, RecoveryOperation.Result.FAILED)

    def test_tbr_match_reopen_reject_confirm_and_single_active_provenance(self):
        label = tbr_match_label(self.tbr_match)
        recover_tbr_match(
            match=self.tbr_match, decision="pending", target_book=self.tbr_one,
            recovery_request=self.request(action="tbr_match.recover", target=self.tbr_match, label=label),
        )
        self.tbr_match.refresh_from_db()
        self.assertEqual(self.tbr_match.status, PersonalTBRMatch.Status.PENDING_REVIEW)
        self.assertFalse(ModifierProvenance.objects.get(source_reference=f"personal_tbr_match:{self.tbr_match.pk}").is_active)
        recover_tbr_match(
            match=self.tbr_match, decision="confirmed", target_book=self.tbr_one,
            recovery_request=self.request(action="tbr_match.recover", target=self.tbr_match, label=label),
        )
        self.assertEqual(ModifierProvenance.objects.filter(source_reference=f"personal_tbr_match:{self.tbr_match.pk}", is_active=True).count(), 1)

    def test_match_recovery_removes_malformed_duplicate_domain_provenance(self):
        for match, source_type, source_reference, source_context, recover, label in (
            (
                self.botm_match, ModifierProvenance.SourceType.BOTM_BOOK,
                f"botm_match:{self.botm_match.pk}",
                f"Qualifying BOTM match {self.botm_match.pk}", recover_botm_match,
                botm_match_label(self.botm_match),
            ),
            (
                self.tbr_match, ModifierProvenance.SourceType.TBR_BOOK,
                f"personal_tbr_match:{self.tbr_match.pk}",
                f"Qualifying Personal TBR match {self.tbr_match.pk}", recover_tbr_match,
                tbr_match_label(self.tbr_match),
            ),
        ):
            canonical = ModifierProvenance.objects.get(
                source_type=source_type, source_reference=source_reference,
            )
            malformed = ModifierProvenance.objects.create(
                month=self.month, participant=self.reader,
                submission=self.second_submission, source_type=source_type,
                source_reference=f"malformed:{source_type}:{match.pk}",
                source_label=canonical.source_label, source_context=source_context,
                amount=canonical.amount, effective_date=canonical.effective_date,
                applied_at=canonical.applied_at, is_system_generated=True,
                is_active=True,
            )
            target = match.botm_book if isinstance(match, BotmMatch) else match.personal_tbr_book
            recover(
                match=match, decision="confirmed", target_book=target,
                recovery_request=self.request(
                    action=f"{source_type}.recover", target=match, label=label,
                ),
            )
            self.assertFalse(ModifierProvenance.objects.filter(pk=malformed.pk).exists())
            self.assertEqual(
                ModifierProvenance.objects.filter(
                    source_type=source_type, source_reference=source_reference,
                    is_active=True,
                ).count(),
                1,
            )

    def test_locked_tbr_entry_repair_requires_tier_three_and_reruns_matching(self):
        values = {
            "catalog_book": self.catalog_one, "catalog_edition": None,
            "title_snapshot": "Corrected Stacked Book", "author_snapshot": "Writer",
            "page_count_snapshot": 310, "cover_url_snapshot": "", "source_url_snapshot": "",
        }
        with self.assertRaises(ValidationError):
            repair_tbr_entry(
                book=self.tbr_one, values=values,
                recovery_request=self.request(action="tbr.repair", target=self.tbr_one, label=str(self.tbr_one)),
            )
        repair_tbr_entry(
            book=self.tbr_one, values=values,
            recovery_request=self.request(
                action="tbr.repair", target=self.tbr_one, label=str(self.tbr_one),
                tier=3, password="root-password-581!",
            ),
        )
        self.tbr_one.refresh_from_db()
        self.assertEqual((self.tbr_one.title_snapshot, self.tbr_one.position), ("Corrected Stacked Book", 1))
        self.assertTrue(PersonalTBRMatch.objects.filter(personal_tbr_book=self.tbr_one).exists())

    def test_whole_locked_tbr_rebuild_validates_max_duplicates_order_subnine_and_rolls_back(self):
        label = tbr_label(self.tbr)
        request = self.request(
            action="tbr.rebuild", target=self.tbr, label=label, tier=3,
            password="root-password-581!",
        )
        with self.assertRaises(ValidationError):
            rebuild_locked_tbr(
                tbr=self.tbr,
                books=[{"position": index + 1, "title_snapshot": f"Book {index}", "author_snapshot": "Writer", "catalog_book": None, "catalog_edition": None, "page_count_snapshot": 100, "cover_url_snapshot": "", "source_url_snapshot": ""} for index in range(10)],
                recovery_request=request,
            )
        duplicate = {"position": 1, "title_snapshot": "Same", "author_snapshot": "Writer", "catalog_book": None, "catalog_edition": None, "page_count_snapshot": 100, "cover_url_snapshot": "", "source_url_snapshot": ""}
        with self.assertRaises(ValidationError):
            rebuild_locked_tbr(tbr=self.tbr, books=[duplicate, {**duplicate, "position": 2}], recovery_request=request)
        before = list(self.tbr.books.values_list("position", "title_snapshot"))
        failure_request = self.request(action="tbr.rebuild", target=self.tbr, label=label, tier=3, password="root-password-581!")
        with self.assertRaises(RuntimeError):
            rebuild_locked_tbr(
                tbr=self.tbr, books=[duplicate], recovery_request=failure_request,
                fail_after_step="cleared",
            )
        self.assertEqual(list(self.tbr.books.values_list("position", "title_snapshot")), before)
        rebuild_locked_tbr(tbr=self.tbr, books=[duplicate], recovery_request=request)
        self.assertEqual(list(self.tbr.books.values_list("position", "title_snapshot")), [(1, "Same")])
        self.assertFalse(PersonalTBRCompletionAward.objects.filter(personal_tbr=self.tbr, is_qualifying=True).exists())

    def test_exact_nine_rebuild_can_qualify_but_shorter_complete_list_never_does(self):
        books = []
        for index in range(1, 10):
            catalog = CatalogBook.objects.create(provider="hardcover", provider_book_id=f"nine-{index}", title=f"Nine Book {index}", author="Writer")
            self.make_submission(catalog, f"Nine Book {index}", 100, date(2026, 8, 10 + index))
            books.append({"position": index, "catalog_book": catalog, "catalog_edition": None, "title_snapshot": f"Nine Book {index}", "author_snapshot": "Writer", "page_count_snapshot": 100, "cover_url_snapshot": "", "source_url_snapshot": ""})
        rebuild_locked_tbr(
            tbr=self.tbr, books=books,
            recovery_request=self.request(action="tbr.rebuild", target=self.tbr, label=tbr_label(self.tbr), tier=3, password="root-password-581!"),
        )
        self.assertEqual(PersonalTBRMatch.objects.filter(personal_tbr_book__personal_tbr=self.tbr, is_qualifying=True).count(), 9)
        self.assertTrue(PersonalTBRCompletionAward.objects.get(personal_tbr=self.tbr).is_qualifying)

    def test_game_retire_void_restore_repair_uses_frozen_amount_and_recipients(self):
        set_game_active(
            game=self.game, active=False,
            recovery_request=self.request(action="game.retire", target=self.game, label=str(self.game)),
        )
        self.game.refresh_from_db()
        self.assertFalse(self.game.is_active)
        set_game_active(
            game=self.game, active=True,
            recovery_request=self.request(action="game.reactivate", target=self.game, label=str(self.game)),
        )
        label = game_application_label(self.application)
        recover_game_application(
            application=self.application, mode="void",
            recovery_request=self.request(action="game_reward.void", target=self.application, label=label),
        )
        self.application.refresh_from_db()
        self.assertTrue(self.application.is_voided)
        recipient_ids = list(self.application.recipients.values_list("participant_id", flat=True))
        recover_game_application(
            application=self.application, mode="restore",
            recovery_request=self.request(action="game_reward.restore", target=self.application, label=label),
        )
        self.application.refresh_from_db()
        self.assertEqual((self.application.is_voided, self.application.amount), (False, 40))
        self.assertEqual(list(self.application.recipients.values_list("participant_id", flat=True)), recipient_ids)
        recipient = self.application.recipients.get()
        old_provenance = recipient.provenance
        GameRewardRecipient.objects.filter(pk=recipient.pk).update(provenance=None)
        old_provenance.delete()
        recover_game_application(
            application=self.application, mode="repair",
            recovery_request=self.request(action="game_reward.repair", target=self.application, label=label),
        )
        recipient.refresh_from_db()
        self.assertEqual((recipient.provenance.amount, recipient.provenance.is_active), (40, True))
        self.assertIsNone(recipient.provenance.submission_id)

    def test_game_restore_failure_rolls_back_application_and_provenance(self):
        label = game_application_label(self.application)
        recover_game_application(
            application=self.application, mode="void",
            recovery_request=self.request(action="game_reward.void", target=self.application, label=label),
        )
        recipient = self.application.recipients.get()
        request = self.request(action="game_reward.restore", target=self.application, label=label)
        with self.assertRaises(RuntimeError):
            recover_game_application(
                application=self.application, mode="restore", recovery_request=request,
                fail_after_step=f"recipient:{recipient.pk}",
            )
        self.application.refresh_from_db()
        recipient.provenance.refresh_from_db()
        self.assertTrue(self.application.is_voided)
        self.assertFalse(recipient.provenance.is_active)
        self.assertEqual(RecoveryOperation.objects.get(operation_id=request.operation_id).result, RecoveryOperation.Result.FAILED)

    def test_wrong_game_amount_is_replaced_without_changing_frozen_recipients(self):
        label = game_application_label(self.application)
        recover_game_application(
            application=self.application, mode="void",
            recovery_request=self.request(
                action="game_reward.void", target=self.application, label=label,
            ),
        )
        operation = recreate_game_application(
            application=self.application, amount=65,
            recovery_request=self.request(
                action="game_reward.recreate", target=self.application, label=label,
            ),
        )
        replacement = GameRewardApplication.objects.get(
            pk=operation.after_state["replacement_application_id"],
        )
        self.assertEqual(replacement.amount, 65)
        self.assertEqual(
            list(replacement.recipients.values_list("participant_id", flat=True)),
            [self.reader.pk],
        )
        self.application.refresh_from_db()
        self.assertTrue(self.application.is_voided)

    def test_scores_are_base_plus_active_cross_domain_modifiers_and_no_cache_double_count(self):
        score = self.score()
        active = ModifierProvenance.objects.filter(month=self.month, participant=self.reader, is_active=True)
        expected_modifier = sum(active.values_list("amount", flat=True))
        self.assertEqual(score["base_pages"], 500)
        self.assertEqual(score["modifier_pages"], expected_modifier)
        self.assertEqual(score["total_pages"], score["base_pages"] + expected_modifier)
        self.assertEqual(self.team.approved_pages, score["total_pages"])

    def test_all_normal_roles_denied_and_recovery_creates_no_identity(self):
        routes = (
            reverse("recovery-botm-list"),
            reverse("recovery-botm-book-detail", kwargs={"pk": self.botm_one.pk}),
            reverse("recovery-botm-match", kwargs={"pk": self.botm_match.pk}),
            reverse("recovery-tbr-list"),
            reverse("recovery-tbr-detail", kwargs={"pk": self.tbr.pk}),
            reverse("recovery-tbr-entry", kwargs={"pk": self.tbr_one.pk}),
            reverse("recovery-game-list"),
            reverse("recovery-game-detail", kwargs={"pk": self.game.pk}),
            reverse("recovery-game-application", kwargs={"pk": self.application.pk, "mode": "repair"}),
        )
        for user in (self.owner_user, self.moderator_user, self.host_user, self.leader_user, self.floater_user, self.reader_user, self.outsider):
            self.client.force_login(user)
            for route in routes:
                with self.subTest(user=user.username, route=route):
                    self.assertEqual(self.client.get(route).status_code, 403)
        self.assertFalse(Membership.objects.filter(user=self.root).exists())
        self.assertFalse(ChallengeStaffAssignment.objects.filter(membership__user=self.root).exists())
        self.assertFalse(MonthEnrollment.objects.filter(participant__user=self.root).exists())

    def test_tier_three_ui_requires_reason_exact_confirmation_and_password(self):
        self.client.force_login(self.root)
        url = reverse("recovery-tbr-entry", kwargs={"pk": self.tbr_one.pk})
        label = f"Locked TBR entry #{self.tbr_one.pk}: {self.reader.display_name} — position {self.tbr_one.position}"
        base = {
            "catalog_book": self.catalog_one.pk, "catalog_edition": "",
            "title_snapshot": self.tbr_one.title_snapshot,
            "author_snapshot": self.tbr_one.author_snapshot,
            "page_count_snapshot": 300, "cover_url_snapshot": "", "source_url_snapshot": "",
        }
        for changes in (
            {"reason": "", "confirmation": label, "current_password": "root-password-581!"},
            {"reason": "Repair", "confirmation": "wrong", "current_password": "root-password-581!"},
            {"reason": "Repair", "confirmation": label, "current_password": "wrong"},
        ):
            response = self.client.post(url, {**base, **changes})
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Backup Advisory")
            self.tbr_one.refresh_from_db()
            self.assertEqual(self.tbr_one.title_snapshot, "Stacked Book")
