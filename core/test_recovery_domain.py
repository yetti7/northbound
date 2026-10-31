from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from .test_recovery_fixtures import create_protected_recovery_graph
from .models import (
    BookSubmission, BotmBook, BotmCompletionAward, BotmMatch, CatalogBook,
    ChallengeMonth, ChallengeSignupAnswer, ChallengeSignupQuestion,
    ChallengeStaffAssignment, Game, GameRewardApplication, GameRewardRecipient,
    Membership, ModifierProvenance, MonthEnrollment, PersonalTBR,
    PersonalTBRBook, PersonalTBRCompletionAward, PersonalTBRMatch,
    ProgressCheckpoint, ProgressCheckpointResult, ReadingGroup,
    RecoveryOperation, Team, TeamAssignment, UserProfile,
)
from .recovery import RecoveryRequest
from .recovery_domain import (
    challenge_purge_impact, correct_enrollment_origin, correct_membership_role, correct_staffing_role,
    delete_unused_team, purge_challenge, reassign_team_assignment,
    set_enrollment_active, set_group_active, set_membership_active,
    set_staffing_active, set_team_archived, set_team_assignment_active,
    set_user_active, transfer_group_ownership,
)


def recovery_request(actor, *, tier, action, target, label, reason="Correct corrupted operational state.", password="", group=None, challenge=None):
    return RecoveryRequest(
        actor=actor, tier=tier, action=action,
        target_type=type(target).__name__, target_id=str(target.pk), target_label=label,
        reason=reason, required_confirmation=label, supplied_confirmation=label,
        current_password=password,
        confirmation_method="exact target label" + (" plus current password" if tier == 3 else ""),
        group=group, challenge=challenge,
        before_state={"label": label},
    )


@override_settings(DEBUG=True, PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ChallengePurgeRecoveryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_superuser("recovery-owner", password="owner-password-482!")
        create_protected_recovery_graph(cls.owner)

    def primary(self):
        return ChallengeMonth.objects.get(name="Stories Under the Stars")

    def request_for(self, month, **overrides):
        values = {
            "actor": self.owner, "tier": 3, "action": "challenge.purge",
            "target_type": "ChallengeMonth", "target_id": str(month.pk), "target_label": month.name,
            "reason": "Remove a corrupted Challenge aggregate.",
            "required_confirmation": month.name, "supplied_confirmation": month.name,
            "current_password": "owner-password-482!", "confirmation_method": "exact target label plus current password",
            "group": month.group, "impact": challenge_purge_impact(month),
            "before_state": {"name": month.name, "status": month.status},
        }
        values.update(overrides)
        return RecoveryRequest(**values)

    def test_preview_counts_are_accurate_and_platform_owner_can_open_active_preview(self):
        month = self.primary()
        counts = {item.label: item.count for item in challenge_purge_impact(month).items}
        self.assertEqual(counts["Enrollments"], MonthEnrollment.objects.filter(month=month).count())
        self.assertEqual(counts["Signup answers"], ChallengeSignupAnswer.objects.filter(enrollment__month=month).count())
        self.assertEqual(counts["Submissions"], BookSubmission.objects.filter(month=month).count())
        self.assertEqual(counts["BOTM matches"], BotmMatch.objects.filter(month=month).count())
        self.assertEqual(counts["Personal TBR books"], PersonalTBRBook.objects.filter(personal_tbr__enrollment__month=month).count())
        self.assertEqual(counts["Modifier provenance"], ModifierProvenance.objects.filter(month=month).count())
        self.assertEqual(counts["Checkpoint results"], ProgressCheckpointResult.objects.filter(checkpoint__month=month).count())
        self.client.force_login(self.owner)
        response = self.client.get(reverse("recovery-challenge-purge", kwargs={"pk": month.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Current Impact Preview")
        self.assertContains(response, "Active Challenge")

    def test_tier3_form_blocks_wrong_password_name_and_blank_reason_for_active_and_completed(self):
        self.client.force_login(self.owner)
        for month in (self.primary(), ChallengeMonth.objects.get(status=ChallengeMonth.Status.COMPLETED)):
            url = reverse("recovery-challenge-purge", kwargs={"pk": month.pk})
            cases = (
                {"reason": "Needed", "confirmation": month.name, "current_password": "wrong"},
                {"reason": "Needed", "confirmation": "wrong name", "current_password": "owner-password-482!"},
                {"reason": "   ", "confirmation": month.name, "current_password": "owner-password-482!"},
            )
            for payload in cases:
                with self.subTest(month=month.name, payload=payload):
                    response = self.client.post(url, payload)
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(ChallengeMonth.objects.filter(pk=month.pk).exists())
        self.assertFalse(RecoveryOperation.objects.exists())

    def test_purge_removes_complete_protected_graph_and_preserves_shared_and_unrelated_data(self):
        month = self.primary()
        month_id = month.pk
        unrelated = ChallengeMonth.objects.exclude(pk=month_id).first()
        user_ids = set(get_user_model().objects.values_list("pk", flat=True))
        membership_ids = set(Membership.objects.values_list("pk", flat=True))
        catalog_ids = set(CatalogBook.objects.values_list("pk", flat=True))
        expected_impact = challenge_purge_impact(month).as_dict()

        operation = purge_challenge(month=month, recovery_request=self.request_for(month))

        self.assertFalse(ChallengeMonth.objects.filter(pk=month_id).exists())
        for queryset in (
            MonthEnrollment.objects.filter(month_id=month_id),
            ChallengeStaffAssignment.objects.filter(month_id=month_id),
            Team.objects.filter(month_id=month_id), TeamAssignment.objects.filter(month_id=month_id),
            BookSubmission.objects.filter(month_id=month_id), BotmBook.objects.filter(month_id=month_id),
            BotmMatch.objects.filter(month_id=month_id), BotmCompletionAward.objects.filter(month_id=month_id),
            PersonalTBR.objects.filter(enrollment__month_id=month_id), PersonalTBRMatch.objects.filter(month_id=month_id),
            PersonalTBRCompletionAward.objects.filter(month_id=month_id), Game.objects.filter(month_id=month_id),
            GameRewardApplication.objects.filter(game__month_id=month_id), GameRewardRecipient.objects.filter(application__game__month_id=month_id),
            ModifierProvenance.objects.filter(month_id=month_id), ProgressCheckpoint.objects.filter(month_id=month_id),
            ProgressCheckpointResult.objects.filter(checkpoint__month_id=month_id),
        ):
            self.assertFalse(queryset.exists(), queryset.model.__name__)
        self.assertEqual(set(get_user_model().objects.values_list("pk", flat=True)), user_ids)
        self.assertEqual(set(Membership.objects.values_list("pk", flat=True)), membership_ids)
        self.assertEqual(set(CatalogBook.objects.values_list("pk", flat=True)), catalog_ids)
        self.assertTrue(ChallengeMonth.objects.filter(pk=unrelated.pk).exists())
        operation.refresh_from_db()
        self.assertEqual(operation.result, RecoveryOperation.Result.SUCCEEDED)
        self.assertIsNone(operation.challenge)
        self.assertEqual(operation.impact, expected_impact)

    def test_injected_failure_rolls_back_everything_and_records_no_false_success(self):
        month = self.primary()
        before = challenge_purge_impact(month).as_dict()
        unrelated_count = ChallengeMonth.objects.exclude(pk=month.pk).count()
        with self.assertRaisesMessage(RuntimeError, "Injected"):
            purge_challenge(month=month, recovery_request=self.request_for(month), fail_after_step="modifier_provenance")
        self.assertTrue(ChallengeMonth.objects.filter(pk=month.pk).exists())
        self.assertEqual(challenge_purge_impact(month).as_dict(), before)
        self.assertEqual(ChallengeMonth.objects.exclude(pk=month.pk).count(), unrelated_count)
        self.assertFalse(RecoveryOperation.objects.filter(result=RecoveryOperation.Result.SUCCEEDED).exists())
        failure = RecoveryOperation.objects.get(result=RecoveryOperation.Result.FAILED)
        self.assertEqual(failure.target_id, str(month.pk))
        self.assertEqual(self.client.get(month.get_absolute_url()).status_code, 302)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ParticipationRecoveryTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_superuser("platform-owner", password="owner-password-482!")
        self.other_owner = User.objects.create_superuser("other-owner", password="other-password")
        self.reader_user = User.objects.create_user("reader", password="reader-password")
        self.second_user = User.objects.create_user("second", password="second-password")
        UserProfile.objects.create(user=self.reader_user)
        UserProfile.objects.create(user=self.second_user)
        self.group = ReadingGroup.objects.create(name="Recovery Circle", slug="recovery-circle")
        self.membership = Membership.objects.create(group=self.group, user=self.reader_user, role=Membership.Role.OWNER, display_name="Reader One")
        self.second_membership = Membership.objects.create(group=self.group, user=self.second_user, display_name="Reader Two")
        self.month = ChallengeMonth.objects.create(group=self.group, name="Recovery Month", starts_on=date(2026, 8, 1), ends_on=date(2026, 8, 31), status=ChallengeMonth.Status.ACTIVE)
        self.team = Team.objects.create(month=self.month, name="North Team")
        self.other_team = Team.objects.create(month=self.month, name="South Team")
        self.enrollment = MonthEnrollment.objects.create(month=self.month, participant=self.membership, origin=MonthEnrollment.Origin.SELF)
        self.assignment = TeamAssignment.objects.create(month=self.month, team=self.team, participant=self.membership, assigned_by=self.owner)

    def req(self, target, label, action="recovery.test", tier=2, **kwargs):
        return recovery_request(self.owner, tier=tier, action=action, target=target, label=label, group=self.group, challenge=self.month, password="owner-password-482!" if tier == 3 else "", **kwargs)

    def test_group_deactivate_reactivate_and_atomic_ownership_transfer(self):
        set_group_active(group=self.group, active=False, recovery_request=self.req(self.group, self.group.name))
        self.group.refresh_from_db(); self.assertFalse(self.group.is_active)
        set_group_active(group=self.group, active=True, recovery_request=self.req(self.group, self.group.name))
        self.group.refresh_from_db(); self.assertTrue(self.group.is_active)
        transfer_group_ownership(group=self.group, target_membership=self.second_membership, recovery_request=self.req(self.group, self.group.name))
        self.membership.refresh_from_db(); self.second_membership.refresh_from_db()
        self.assertEqual(self.membership.role, Membership.Role.MEMBER)
        self.assertEqual(self.second_membership.role, Membership.Role.OWNER)
        self.assertEqual(self.group.memberships.filter(is_active=True, role=Membership.Role.OWNER).count(), 1)

    def test_user_deactivate_reactivate_preserves_history_and_owner_safety(self):
        reader_client = Client()
        self.assertTrue(reader_client.login(username="reader", password="reader-password"))
        reader_session_key = reader_client.session.session_key
        historical_ids = (self.membership.pk, self.enrollment.pk, self.assignment.pk)
        set_user_active(user=self.reader_user, active=False, recovery_request=self.req(self.reader_user, self.reader_user.username))
        self.reader_user.refresh_from_db(); self.assertFalse(self.reader_user.is_active)
        from django.contrib.sessions.models import Session
        self.assertFalse(Session.objects.filter(session_key=reader_session_key).exists())
        self.assertTrue(Membership.objects.filter(pk=historical_ids[0]).exists())
        self.assertTrue(MonthEnrollment.objects.filter(pk=historical_ids[1]).exists())
        self.assertTrue(TeamAssignment.objects.filter(pk=historical_ids[2]).exists())
        set_user_active(user=self.reader_user, active=True, recovery_request=self.req(self.reader_user, self.reader_user.username))
        self.reader_user.refresh_from_db(); self.assertTrue(self.reader_user.is_active)
        with self.assertRaisesMessage(ValidationError, "own Platform Owner"):
            set_user_active(user=self.owner, active=False, recovery_request=self.req(self.owner, self.owner.username, tier=3))
        self.other_owner.is_active = False; self.other_owner.save(update_fields=["is_active"])
        with self.assertRaisesMessage(ValidationError, "at least one"):
            set_user_active(user=self.other_owner, active=False, recovery_request=self.req(self.other_owner, self.other_owner.username, tier=3))

    def test_membership_status_role_and_final_owner_invariants(self):
        self.second_membership.role = Membership.Role.OWNER; self.second_membership.save(update_fields=["role"])
        with self.assertRaisesMessage(ValidationError, "active Challenge"):
            set_membership_active(membership=self.membership, active=False, recovery_request=self.req(self.membership, self.membership.display_name))
        set_enrollment_active(enrollment=self.enrollment, active=False, recovery_request=self.req(self.enrollment, f"{self.membership.display_name} — {self.month.name}"))
        self.second_membership.role = Membership.Role.MEMBER; self.second_membership.save(update_fields=["role"])
        with self.assertRaisesMessage(ValidationError, "final active Owner"):
            set_membership_active(membership=self.membership, active=False, recovery_request=self.req(self.membership, self.membership.display_name))
        self.second_membership.role = Membership.Role.OWNER; self.second_membership.save(update_fields=["role"])
        set_membership_active(membership=self.membership, active=False, recovery_request=self.req(self.membership, self.membership.display_name))
        self.membership.refresh_from_db(); self.assertFalse(self.membership.is_active)
        set_membership_active(membership=self.membership, active=True, recovery_request=self.req(self.membership, self.membership.display_name))
        correct_membership_role(membership=self.membership, role=Membership.Role.MODERATOR, recovery_request=self.req(self.membership, self.membership.display_name))
        self.membership.refresh_from_db(); self.assertEqual(self.membership.role, Membership.Role.MODERATOR)

    def test_staffing_end_restore_correction_and_mutual_exclusivity(self):
        host = ChallengeStaffAssignment.objects.create(month=self.month, membership=self.second_membership, role=ChallengeStaffAssignment.Role.HOST, assigned_by=self.owner)
        set_staffing_active(assignment=host, active=False, recovery_request=self.req(host, str(host)))
        host.refresh_from_db(); self.assertIsNotNone(host.ended_at)
        set_staffing_active(assignment=host, active=True, recovery_request=self.req(host, str(host)))
        host.refresh_from_db(); self.assertIsNone(host.ended_at)
        replacement_op = correct_staffing_role(assignment=host, role=ChallengeStaffAssignment.Role.FLOATER, team=None, recovery_request=self.req(host, str(host)))
        host.refresh_from_db(); self.assertIsNotNone(host.ended_at)
        self.assertTrue(ChallengeStaffAssignment.objects.filter(month=self.month, membership=self.second_membership, role=ChallengeStaffAssignment.Role.FLOATER, ended_at__isnull=True).exists())
        self.assertEqual(replacement_op.result, RecoveryOperation.Result.SUCCEEDED)
        with self.assertRaises(ValidationError):
            set_staffing_active(assignment=host, active=True, recovery_request=self.req(host, str(host)))

    def test_enrollment_withdraw_reactivate_preserves_answers_tbr_and_rejects_missing_required(self):
        question = ChallengeSignupQuestion.objects.create(month=self.month, wording="Required", question_type=ChallengeSignupQuestion.QuestionType.SHORT_TEXT, is_required=True)
        answer = ChallengeSignupAnswer.objects.create(enrollment=self.enrollment, question=question, value="Preserve me")
        tbr = PersonalTBR.objects.create(enrollment=self.enrollment)
        set_enrollment_active(enrollment=self.enrollment, active=False, recovery_request=self.req(self.enrollment, f"{self.membership.display_name} — {self.month.name}"))
        self.enrollment.refresh_from_db(); self.assertFalse(self.enrollment.is_active)
        self.assertTrue(ChallengeSignupAnswer.objects.filter(pk=answer.pk).exists())
        self.assertTrue(PersonalTBR.objects.filter(pk=tbr.pk).exists())
        set_enrollment_active(enrollment=self.enrollment, active=True, recovery_request=self.req(self.enrollment, f"{self.membership.display_name} — {self.month.name}"))
        self.enrollment.refresh_from_db(); self.assertTrue(self.enrollment.is_active)
        correct_enrollment_origin(enrollment=self.enrollment, origin=MonthEnrollment.Origin.STAFF, recovery_request=self.req(self.enrollment, f"{self.membership.display_name} — {self.month.name}"))
        self.enrollment.refresh_from_db(); self.assertEqual(self.enrollment.origin, MonthEnrollment.Origin.STAFF)
        set_enrollment_active(enrollment=self.enrollment, active=False, recovery_request=self.req(self.enrollment, f"{self.membership.display_name} — {self.month.name}"))
        answer.delete()
        with self.assertRaisesMessage(ValidationError, "Required registration answers"):
            set_enrollment_active(enrollment=self.enrollment, active=True, recovery_request=self.req(self.enrollment, f"{self.membership.display_name} — {self.month.name}"))

    def test_team_archive_reactivate_and_unused_delete_guards(self):
        set_team_archived(team=self.team, archived=True, recovery_request=self.req(self.team, self.team.name))
        self.team.refresh_from_db(); self.assertTrue(self.team.is_archived)
        set_team_archived(team=self.team, archived=False, recovery_request=self.req(self.team, self.team.name))
        self.team.refresh_from_db(); self.assertFalse(self.team.is_archived)
        with self.assertRaisesMessage(ValidationError, "Draft"):
            delete_unused_team(team=self.other_team, recovery_request=self.req(self.other_team, self.other_team.name, tier=1))
        draft = ChallengeMonth.objects.create(group=self.group, name="Draft", starts_on=date(2026, 9, 1), ends_on=date(2026, 9, 30))
        unused = Team.objects.create(month=draft, name="Unused")
        delete_unused_team(team=unused, recovery_request=recovery_request(self.owner, tier=1, action="team.delete_unused", target=unused, label=unused.name, group=self.group, challenge=draft))
        self.assertFalse(Team.objects.filter(pk=unused.pk).exists())
        used = Team.objects.create(month=draft, name="Used")
        draft_enrollment = MonthEnrollment.objects.create(month=draft, participant=self.second_membership)
        TeamAssignment.objects.create(month=draft, team=used, participant=self.second_membership)
        with self.assertRaisesMessage(ValidationError, "Historically used"):
            delete_unused_team(team=used, recovery_request=recovery_request(self.owner, tier=1, action="team.delete_unused", target=used, label=used.name, group=self.group, challenge=draft))

    def test_team_assignment_end_restore_and_atomic_reassign_preserve_history_and_leader_coherence(self):
        leader = ChallengeStaffAssignment.objects.create(month=self.month, membership=self.membership, team=self.team, role=ChallengeStaffAssignment.Role.TEAM_LEADER, assigned_by=self.owner)
        set_team_assignment_active(assignment=self.assignment, active=False, recovery_request=self.req(self.assignment, "Reader One → North Team"))
        self.assignment.refresh_from_db(); leader.refresh_from_db()
        self.assertIsNotNone(self.assignment.ended_at); self.assertIsNotNone(leader.ended_at)
        set_team_assignment_active(assignment=self.assignment, active=True, recovery_request=self.req(self.assignment, "Reader One → North Team"))
        self.assignment.refresh_from_db(); self.assertIsNone(self.assignment.ended_at)
        operation = reassign_team_assignment(assignment=self.assignment, team=self.other_team, recovery_request=self.req(self.assignment, "Reader One → North Team"))
        self.assignment.refresh_from_db()
        replacement = TeamAssignment.objects.get(pk=operation.after_state["replacement_assignment_id"])
        self.assertIsNotNone(self.assignment.ended_at)
        self.assertEqual(replacement.team, self.other_team)
        self.assertNotEqual(replacement.pk, self.assignment.pk)

    def test_all_non_platform_roles_are_denied_and_access_creates_no_identity(self):
        host = ChallengeStaffAssignment.objects.create(month=self.month, membership=self.second_membership, role=ChallengeStaffAssignment.Role.HOST)
        users = [self.reader_user, self.second_user, get_user_model().objects.create_user("ordinary")]
        baseline = (Membership.objects.count(), ChallengeStaffAssignment.objects.count(), MonthEnrollment.objects.count(), TeamAssignment.objects.count())
        urls = (
            reverse("recovery-challenge-list"), reverse("recovery-group-list"), reverse("recovery-account-list"),
            reverse("recovery-enrollment-status", kwargs={"pk": self.enrollment.pk}),
            reverse("recovery-team-assignment-status", kwargs={"pk": self.assignment.pk}),
        )
        for user in users:
            self.client.force_login(user)
            for url in urls:
                self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(baseline, (Membership.objects.count(), ChallengeStaffAssignment.objects.count(), MonthEnrollment.objects.count(), TeamAssignment.objects.count()))
