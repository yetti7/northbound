from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .game_rewards import apply_game_reward, void_game_reward
from .score_aggregation import challenge_score_totals
from .models import (
    AuditEvent,
    BookSubmission,
    ChallengeMonth,
    ChallengeStaffAssignment,
    Game,
    GameRewardApplication,
    Membership,
    ModifierProvenance,
    MonthEnrollment,
    ReadingGroup,
    Team,
    TeamAssignment,
)


class GamesManagementUITests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.root = User.objects.create_superuser("games-ui-root", "root@example.com", "password")
        self.host_user = User.objects.create_user("games-ui-host")
        self.owner_user = User.objects.create_user("games-ui-owner")
        self.reader_user = User.objects.create_user("games-ui-reader")
        self.floater_user = User.objects.create_user("games-ui-floater")
        self.leader_user = User.objects.create_user("games-ui-leader")
        self.ended_user = User.objects.create_user("games-ui-ended")
        self.group = ReadingGroup.objects.create(name="Games UI Group", slug="games-ui-group")
        self.host = self.member(self.host_user, "Host")
        self.owner = self.member(self.owner_user, "Owner", Membership.Role.OWNER)
        self.reader = self.member(self.reader_user, "Reader")
        self.floater = self.member(self.floater_user, "Floater")
        self.leader = self.member(self.leader_user, "Leader")
        self.ended = self.member(self.ended_user, "Ended Host")
        self.month = ChallengeMonth.objects.create(
            group=self.group,
            name="Games UI Challenge",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            status=ChallengeMonth.Status.ACTIVE,
            games_enabled=True,
        )
        ChallengeStaffAssignment.objects.create(
            month=self.month, membership=self.host, role=ChallengeStaffAssignment.Role.HOST
        )
        ChallengeStaffAssignment.objects.create(
            month=self.month, membership=self.floater, role=ChallengeStaffAssignment.Role.FLOATER
        )
        ChallengeStaffAssignment.objects.create(
            month=self.month, membership=self.ended, role=ChallengeStaffAssignment.Role.HOST,
            ended_at=timezone.now(),
        )
        self.team = Team.objects.create(month=self.month, name="Games Team")
        for participant in (self.reader, self.leader):
            MonthEnrollment.objects.create(month=self.month, participant=participant)
            TeamAssignment.objects.create(month=self.month, participant=participant, team=self.team)
        ChallengeStaffAssignment.objects.create(
            month=self.month, membership=self.leader, team=self.team,
            role=ChallengeStaffAssignment.Role.TEAM_LEADER,
        )
        now = timezone.now()
        self.game = Game.objects.create(
            month=self.month,
            name="Reading Sprint",
            starts_at=now - timedelta(days=1),
            ends_at=now + timedelta(days=2),
            advertised_bonus_pages=50,
        )
        self.list_url = reverse("game-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        self.detail_url = reverse("game-detail", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.game.pk})
        self.settings_url = reverse("challenge-games-settings", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        self.apply_url = reverse("game-reward-apply", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.game.pk})

    def member(self, user, display_name, role=Membership.Role.MEMBER):
        return Membership.objects.create(group=self.group, user=user, display_name=display_name, role=role)

    def game_payload(self, **overrides):
        starts = timezone.localtime(self.game.starts_at, timezone.get_current_timezone())
        values = {
            "name": "Updated Sprint",
            "starts_at": starts.strftime("%Y-%m-%dT%H:%M"),
            "ends_at": (starts + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M"),
            "advertised_bonus_pages": "60",
        }
        values.update(overrides)
        return values

    def apply_reward(self):
        return apply_game_reward(
            game=self.game,
            actor=self.host_user,
            target_type=GameRewardApplication.TargetType.TEAM,
            target_team=self.team,
            amount=40,
            reason="Finished the team sprint.",
            idempotency_key=uuid4(),
        )[0]

    def test_settings_default_render_host_root_and_unauthorized_authority(self):
        disabled = ChallengeMonth.objects.create(group=self.group, name="Default Games", status=ChallengeMonth.Status.DRAFT)
        self.assertFalse(disabled.games_enabled)
        for user in (self.host_user, self.root):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertContains(self.client.get(self.settings_url), "Enable Games")
                response = self.client.post(self.settings_url, {"games_enabled": ""})
                self.assertRedirects(response, reverse("challenge-settings", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}))
        for user in (self.owner_user, self.reader_user, self.floater_user, self.leader_user, self.ended_user):
            with self.subTest(user=user.username):
                self.month.games_enabled = False
                self.month.save(update_fields=["games_enabled"])
                self.client.force_login(user)
                self.assertEqual(self.client.post(self.settings_url, {"games_enabled": "on"}).status_code, 403)
                self.month.refresh_from_db()
                self.assertFalse(self.month.games_enabled)

    def test_existing_configurable_lifecycle_lock_applies_to_game_mutations(self):
        ChallengeMonth.objects.filter(pk=self.month.pk).update(status=ChallengeMonth.Status.FINALIZING)
        self.month.refresh_from_db()
        self.client.force_login(self.host_user)
        settings = self.client.get(reverse("challenge-settings", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}))
        self.assertNotContains(settings, "Manage Games Setting")
        games = self.client.get(self.list_url)
        self.assertNotContains(games, "Add Game")
        self.assertRedirects(self.client.post(self.settings_url, {"games_enabled": ""}), reverse("challenge-settings", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}))
        self.month.refresh_from_db()
        self.assertTrue(self.month.games_enabled)

    def test_disabling_preserves_game_reward_provenance_score_and_book_caches(self):
        application = self.apply_reward()
        provenance_ids = list(ModifierProvenance.objects.filter(month=self.month).values_list("pk", flat=True))
        book = BookSubmission.objects.create(
            month=self.month, participant=self.reader, title="Cache Book", author="Author",
            book_format=BookSubmission.Format.EBOOK, completed_on=self.month.starts_on,
            submitted_pages=100, approved_pages=100, bonus_pages=0, final_scored_pages=100,
            status=BookSubmission.Status.APPROVED,
        )
        score_before = challenge_score_totals(month=self.month, participant_ids=[self.reader.pk])[self.reader.pk]
        self.client.force_login(self.host_user)
        self.client.post(self.settings_url, {"games_enabled": ""})
        self.month.refresh_from_db()
        book.refresh_from_db()
        self.assertFalse(self.month.games_enabled)
        self.assertTrue(Game.objects.filter(pk=self.game.pk).exists())
        self.assertTrue(GameRewardApplication.objects.filter(pk=application.pk).exists())
        self.assertEqual(list(ModifierProvenance.objects.filter(month=self.month).values_list("pk", flat=True)), provenance_ids)
        self.assertEqual((book.approved_pages, book.bonus_pages, book.final_scored_pages), (100, 0, 100))
        self.assertEqual(challenge_score_totals(month=self.month, participant_ids=[self.reader.pk])[self.reader.pk], score_before)
        self.assertEqual(AuditEvent.objects.filter(action="challenge.games_setting_updated").count(), 1)

    def test_challenge_detail_enabled_empty_populated_and_disabled_states(self):
        detail = self.month.get_absolute_url()
        self.client.force_login(self.reader_user)
        response = self.client.get(detail)
        self.assertContains(response, f'href="{self.list_url}"><span>Games</span>')
        self.assertContains(response, "Reading Sprint")
        self.assertNotContains(response, "View Games")
        self.assertNotContains(response, "Apply Reward")
        self.assertNotContains(response, "Void Reward")

        self.game.delete()
        response = self.client.get(detail)
        self.assertContains(response, '<div class="empty-state challenge-section-empty">No Games have been added.</div>')

        self.month.games_enabled = False
        self.month.save(update_fields=["games_enabled"])
        response = self.client.get(detail)
        self.assertNotContains(response, '<span>Games</span>')

    def test_games_pages_are_reader_safe_and_management_is_operator_only(self):
        for user in (self.reader_user, self.owner_user, self.floater_user, self.leader_user, self.ended_user):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertContains(self.client.get(self.list_url), self.game.name)
                detail = self.client.get(self.detail_url)
                self.assertEqual(detail.status_code, 200)
                self.assertNotContains(detail, "Reward History")
                self.assertNotContains(detail, "Edit Game")
                for route in ("game-edit", "game-active-toggle", "game-delete"):
                    url = reverse(route, kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.game.pk})
                    self.assertEqual(self.client.get(url).status_code, 403)
        self.month.games_enabled = False
        self.month.save(update_fields=["games_enabled"])
        self.client.force_login(self.reader_user)
        self.assertEqual(self.client.get(self.list_url).status_code, 403)
        self.client.force_login(self.host_user)
        self.assertEqual(self.client.get(self.list_url).status_code, 200)

    def test_game_status_badges_use_compact_intrinsic_alignment(self):
        self.client.force_login(self.host_user)
        active = self.client.get(self.list_url)
        self.assertContains(active, 'class="pill status-badge status-success">Active</span>')

        self.game.is_active = False
        self.game.save(update_fields=["is_active"])
        retired = self.client.get(self.list_url)
        self.assertContains(retired, 'class="pill status-badge status-inactive">Retired</span>')

        self.game.is_active = True
        self.game.save(update_fields=["is_active"])
        application = self.apply_reward()
        void_game_reward(application=application, actor=self.host_user, reason="Duplicate result entry.")
        detail = self.client.get(self.detail_url)
        self.assertContains(detail, 'status-destructive">Voided</span>')

        css_path = finders.find("css/theme.css")
        self.assertIsNotNone(css_path)
        css = Path(css_path).read_text()
        self.assertIn(".feature-card>.status-badge{align-self:flex-start;max-width:100%}", css)

    def test_host_and_platform_owner_create_with_validation_fixed_ownership_and_audit(self):
        other_group = ReadingGroup.objects.create(name="Other", slug="games-ui-other")
        other_month = ChallengeMonth.objects.create(group=other_group, name="Other Challenge")
        create_url = reverse("game-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        for user, suffix in ((self.host_user, "Host"), (self.root, "Root")):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                payload = self.game_payload(name=f"Created by {suffix}", month=other_month.pk)
                response = self.client.post(create_url, payload)
                created = Game.objects.get(month=self.month, name=f"Created by {suffix}")
                self.assertRedirects(response, reverse("game-detail", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": created.pk}))
                self.assertFalse(Game.objects.filter(month=other_month, name=f"Created by {suffix}").exists())
        self.assertEqual(AuditEvent.objects.filter(action="game.created").count(), 2)
        self.client.force_login(self.host_user)
        for overrides, error in (
            ({"name": ""}, "This field is required"),
            ({"advertised_bonus_pages": "0"}, "Advertised bonus pages must be positive"),
            ({"ends_at": self.game_payload()["starts_at"]}, "Game ending date/time must be after its starting date/time"),
        ):
            with self.subTest(overrides=overrides):
                payload = self.game_payload(name=f"Invalid {uuid4()}")
                payload.update(overrides)
                response = self.client.post(create_url, payload)
                self.assertContains(response, error)

    def test_edit_preserves_application_snapshots_provenance_and_creates_audit(self):
        application = self.apply_reward()
        snapshots = (application.game_name_snapshot, application.advertised_bonus_pages_snapshot)
        provenance = list(application.recipients.values_list("provenance_id", "provenance__amount"))
        edit_url = reverse("game-edit", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.game.pk})
        self.client.force_login(self.host_user)
        self.assertRedirects(self.client.post(edit_url, self.game_payload()), self.detail_url)
        self.game.refresh_from_db()
        application.refresh_from_db()
        self.assertEqual(self.game.name, "Updated Sprint")
        self.assertEqual((application.game_name_snapshot, application.advertised_bonus_pages_snapshot), snapshots)
        self.assertEqual(list(application.recipients.values_list("provenance_id", "provenance__amount")), provenance)
        self.assertEqual(AuditEvent.objects.filter(action="game.updated", object_id=str(self.game.pk)).count(), 1)

    def test_retire_reactivate_preserve_reward_history_effect_and_audit(self):
        application = self.apply_reward()
        provenance_ids = list(application.recipients.values_list("provenance_id", flat=True))
        toggle_url = reverse("game-active-toggle", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.game.pk})
        self.client.force_login(self.host_user)
        self.assertContains(self.client.get(toggle_url), "prevents future reward application")
        self.client.post(toggle_url)
        self.game.refresh_from_db()
        application.refresh_from_db()
        self.assertFalse(self.game.is_active)
        self.assertFalse(application.is_voided)
        self.assertEqual(ModifierProvenance.objects.filter(pk__in=provenance_ids, is_active=True).count(), len(provenance_ids))
        self.assertContains(self.client.get(self.detail_url), "Retired")
        self.client.post(toggle_url)
        self.game.refresh_from_db()
        self.assertTrue(self.game.is_active)
        self.assertTrue(AuditEvent.objects.filter(action="game.retired").exists())
        self.assertTrue(AuditEvent.objects.filter(action="game.reactivated").exists())

    def test_delete_requires_confirmation_allows_unused_and_denies_history(self):
        delete_url = reverse("game-delete", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.game.pk})
        self.client.force_login(self.host_user)
        self.assertContains(self.client.get(delete_url), "Confirm Delete")
        self.assertTrue(Game.objects.filter(pk=self.game.pk).exists())
        deleted_game_id = self.game.pk
        self.client.post(delete_url)
        self.assertFalse(Game.objects.filter(pk=deleted_game_id).exists())
        self.assertTrue(AuditEvent.objects.filter(action="game.deleted", object_id=str(deleted_game_id)).exists())

        now = timezone.now()
        self.game = Game.objects.create(month=self.month, name="Protected Game", starts_at=now, ends_at=now + timedelta(days=1), advertised_bonus_pages=50)
        self.detail_url = reverse("game-detail", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.game.pk})
        self.apply_reward()
        delete_url = reverse("game-delete", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.game.pk})
        self.assertRedirects(self.client.post(delete_url), self.detail_url)
        self.assertTrue(Game.objects.filter(pk=self.game.pk).exists())
        self.assertTrue(self.game.reward_applications.exists())

    def test_operator_history_is_frozen_read_only_and_void_context_is_visible(self):
        application = self.apply_reward()
        frozen_names = list(application.recipients.values_list("participant__display_name", flat=True))
        TeamAssignment.objects.filter(month=self.month, participant=self.reader, ended_at__isnull=True).update(ended_at=timezone.now())
        self.client.force_login(self.host_user)
        response = self.client.get(self.detail_url)
        self.assertContains(response, "Reward History")
        self.assertContains(response, "Finished the team sprint.")
        for name in frozen_names:
            self.assertContains(response, name)
        self.assertContains(response, "Apply Reward")
        self.assertContains(response, "Void Reward")

        void_game_reward(application=application, actor=self.host_user, reason="Duplicate result entry.")
        response = self.client.get(self.detail_url)
        self.assertContains(response, "Voided")
        self.assertContains(response, "status-destructive")
        self.assertContains(response, "Duplicate result entry.")
        self.assertContains(response, self.host_user.username)
        self.assertNotContains(response, ">Void Reward<")

    def reward_payload(self, **overrides):
        values = {
            "amount": "40",
            "target_type": GameRewardApplication.TargetType.READER,
            "target_participant": str(self.reader.pk),
            "target_team": "",
            "reason": "Won the reading sprint.",
            "idempotency_key": str(uuid4()),
        }
        values.update(overrides)
        return values

    def test_apply_ui_authority_and_lifecycle_unavailable_states(self):
        for user in (self.host_user, self.root):
            with self.subTest(allowed=user.username):
                self.client.force_login(user)
                self.assertContains(self.client.get(self.apply_url), "Review Recipients")
        for user in (self.owner_user, self.reader_user, self.floater_user, self.leader_user, self.ended_user):
            with self.subTest(denied=user.username):
                self.client.force_login(user)
                self.assertEqual(self.client.get(self.apply_url).status_code, 403)
        self.client.force_login(self.host_user)
        for status in (
            ChallengeMonth.Status.DRAFT,
            ChallengeMonth.Status.UPCOMING,
            ChallengeMonth.Status.COMPLETED,
            ChallengeMonth.Status.ARCHIVED,
        ):
            with self.subTest(status=status):
                ChallengeMonth.objects.filter(pk=self.month.pk).update(status=status)
                response = self.client.get(self.apply_url)
                self.assertContains(response, "only while the Challenge is Active or Finalizing")
                self.assertNotContains(response, "Review Recipients")
        for status in (ChallengeMonth.Status.ACTIVE, ChallengeMonth.Status.FINALIZING):
            with self.subTest(status=status):
                ChallengeMonth.objects.filter(pk=self.month.pk).update(status=status)
                self.assertContains(self.client.get(self.apply_url), "Review Recipients")
        Game.objects.filter(pk=self.game.pk).update(is_active=False)
        self.assertContains(self.client.get(self.apply_url), "retired Game")

    def test_apply_form_defaults_amount_validates_values_and_route_owned_targets(self):
        self.client.force_login(self.host_user)
        response = self.client.get(self.apply_url)
        self.assertEqual(response.context["form"].fields["amount"].initial, 50)
        for amount, error in (("0", "greater than or equal to 1"), ("-1", "greater than or equal to 1"), ("51", "cannot exceed"), ("nope", "whole number")):
            with self.subTest(amount=amount):
                response = self.client.post(self.apply_url, self.reward_payload(amount=amount))
                self.assertContains(response, error)
        other_group = ReadingGroup.objects.create(name="Other Rewards", slug="other-rewards")
        other_member = Membership.objects.create(
            group=other_group,
            user=get_user_model().objects.create_user("other-reward-reader"),
            display_name="Other Reader",
        )
        other_month = ChallengeMonth.objects.create(group=other_group, name="Other Reward Challenge")
        other_team = Team.objects.create(month=other_month, name="Other Team")
        for overrides in (
            {"target_participant": str(other_member.pk)},
            {"target_type": GameRewardApplication.TargetType.TEAM, "target_participant": "", "target_team": str(other_team.pk)},
        ):
            with self.subTest(overrides=overrides):
                response = self.client.post(self.apply_url, self.reward_payload(**overrides))
                self.assertContains(response, "Select a valid choice")
        self.assertFalse(GameRewardApplication.objects.exists())

    def test_preview_is_read_only_and_final_apply_is_idempotent(self):
        self.client.force_login(self.host_user)
        payload = self.reward_payload(amount="25")
        preview = self.client.post(self.apply_url, payload)
        self.assertContains(preview, "Confirm Reward")
        self.assertContains(preview, self.reader.display_name)
        self.assertContains(preview, "Resolved Recipients")
        self.assertFalse(GameRewardApplication.objects.exists())
        payload["step"] = "apply"
        self.assertRedirects(self.client.post(self.apply_url, payload), self.detail_url)
        self.assertRedirects(self.client.post(self.apply_url, payload), self.detail_url)
        application = GameRewardApplication.objects.get()
        self.assertEqual(application.amount, 25)
        self.assertEqual(application.recipients.count(), 1)
        self.assertEqual(ModifierProvenance.objects.filter(source_type=ModifierProvenance.SourceType.GAME_REWARD).count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="game.reward_applied").count(), 1)

    def test_final_apply_revalidates_preview_and_zero_recipient_failure_writes_nothing(self):
        self.client.force_login(self.host_user)
        payload = self.reward_payload()
        self.assertContains(self.client.post(self.apply_url, payload), "Confirm Reward")
        MonthEnrollment.objects.filter(month=self.month, participant=self.reader).update(is_active=False)
        payload["step"] = "apply"
        response = self.client.post(self.apply_url, payload)
        self.assertContains(response, "no eligible Readers")
        self.assertFalse(GameRewardApplication.objects.exists())
        self.assertFalse(ModifierProvenance.objects.exists())

        payload = self.reward_payload(
            target_type=GameRewardApplication.TargetType.TEAM,
            target_participant="",
            target_team=str(Team.objects.create(month=self.month, name="Empty Team").pk),
        )
        response = self.client.post(self.apply_url, payload)
        self.assertContains(response, "no eligible Readers")
        self.assertFalse(GameRewardApplication.objects.exists())

    def test_team_and_challenge_previews_use_service_recipient_semantics(self):
        MonthEnrollment.objects.create(month=self.month, participant=self.host)
        TeamAssignment.objects.create(month=self.month, participant=self.host, team=self.team)
        self.client.force_login(self.host_user)
        team_preview = self.client.post(self.apply_url, self.reward_payload(
            target_type=GameRewardApplication.TargetType.TEAM,
            target_participant="",
            target_team=str(self.team.pk),
        ))
        for participant in (self.reader, self.leader, self.host):
            self.assertContains(team_preview, participant.display_name)
        self.assertNotContains(team_preview, self.floater.display_name)
        self.assertNotContains(team_preview, self.root.username)
        challenge_preview = self.client.post(self.apply_url, self.reward_payload(
            target_type=GameRewardApplication.TargetType.CHALLENGE,
            target_participant="",
            target_team="",
        ))
        self.assertContains(challenge_preview, "Confirm Reward")
        self.assertFalse(GameRewardApplication.objects.exists())

    def test_void_ui_requires_authority_reason_and_preserves_history(self):
        application = self.apply_reward()
        void_url = reverse("game-reward-void", kwargs={
            "group_slug": self.group.slug,
            "month_pk": self.month.pk,
            "pk": self.game.pk,
            "application_pk": application.pk,
        })
        for user in (self.reader_user, self.owner_user, self.floater_user, self.leader_user, self.ended_user):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self.client.get(void_url).status_code, 403)
        self.client.force_login(self.host_user)
        response = self.client.get(void_url)
        self.assertContains(response, application.reason)
        for recipient in application.recipients.all():
            self.assertContains(response, recipient.participant.display_name)
        self.assertContains(self.client.post(void_url, {"reason": ""}), "This field is required")
        recipients = list(application.recipients.values_list("pk", flat=True))
        provenances = list(application.recipients.values_list("provenance_id", flat=True))
        self.assertRedirects(self.client.post(void_url, {"reason": "Wrong result."}), self.detail_url)
        application.refresh_from_db()
        self.assertTrue(application.is_voided)
        self.assertEqual(list(application.recipients.values_list("pk", flat=True)), recipients)
        self.assertEqual(ModifierProvenance.objects.filter(pk__in=provenances, is_active=False).count(), len(provenances))
        self.assertContains(self.client.get(void_url), "already voided")
        self.assertNotContains(self.client.get(void_url), "Confirm Void Reward")
        self.assertEqual(self.client.get(self.apply_url).context["logical_parent_url"], self.detail_url)
        self.assertEqual(self.client.get(void_url).context["logical_parent_url"], self.detail_url)

    def test_management_ui_does_not_create_rewards_or_provenance_and_uses_logical_parents(self):
        self.client.force_login(self.host_user)
        self.client.post(reverse("game-edit", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.game.pk}), self.game_payload())
        self.client.post(reverse("game-active-toggle", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.game.pk}))
        self.assertFalse(GameRewardApplication.objects.exists())
        self.assertFalse(ModifierProvenance.objects.exists())
        self.assertEqual(self.client.get(self.list_url).context["logical_parent_url"], self.month.get_absolute_url())
        self.assertEqual(self.client.get(self.detail_url).context["logical_parent_url"], self.list_url)
