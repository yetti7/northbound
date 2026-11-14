from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from threading import Event
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connections, transaction
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone

from .administrative_matching import can_manage_credit
from .game_rewards import preview_game_reward
from .models import (
    AuditEvent, BookSubmission, ChallengeMonth, ChallengeStaffAssignment, Game,
    Membership, MonthEnrollment, ReadingGroup, RecoveryOperation, Team,
    TeamAssignment, UserProfile,
)
from .participation import activate_participation, assign_participant_to_team, deactivate_participation
from .permissions import can_review_submission, can_view_reader_scores, can_view_team_standings
from .recovery import RecoveryRequest
from .recovery_domain import restore_inactive_host_scope, set_enrollment_active


class ParticipatingHostScopeTests(TestCase):
    def setUp(self):
        self.group = ReadingGroup.objects.create(name="Scoped Hosts", slug="scoped-hosts")
        self.month = ChallengeMonth.objects.create(
            group=self.group, name="Challenge", status=ChallengeMonth.Status.ACTIVE,
            starts_on=date(2026, 8, 1), ends_on=date(2026, 8, 31),
            reader_scores_visibility=ChallengeMonth.CompetitionVisibility.HOSTS,
            team_standings_visibility=ChallengeMonth.CompetitionVisibility.HOSTS,
        )
        self.a = Team.objects.create(month=self.month, name="A")
        self.b = Team.objects.create(month=self.month, name="B")
        self.host = self.member("scoped-host", Membership.Role.OWNER, host=True, team=self.a)
        self.admin = self.member("wide-host", host=True)
        self.other_host = self.member("other-host", host=True, team=self.b)
        self.reader = self.member("team-b-reader", team=self.b)
        self.leader = self.member("team-a-leader", team=self.a)
        ChallengeStaffAssignment.objects.create(
            month=self.month, membership=self.leader, team=self.a, role=ChallengeStaffAssignment.Role.TEAM_LEADER,
        )
        self.enrollment = self.month.enrollments.get(participant=self.host)
        self.own_book = self.book(self.host)
        self.other_book = self.book(self.reader)
        self.root = get_user_model().objects.create_superuser("scope-root")

    def member(self, name, group_role=Membership.Role.MEMBER, *, host=False, team=None):
        user = get_user_model().objects.create_user(name)
        member = Membership.objects.create(group=self.group, user=user, role=group_role, display_name=name)
        UserProfile.objects.create(user=user, discord_username=f"private-{name}")
        if host:
            ChallengeStaffAssignment.objects.create(
                month=self.month, membership=member, role=ChallengeStaffAssignment.Role.HOST,
            )
        if team:
            MonthEnrollment.objects.create(month=self.month, participant=member)
            TeamAssignment.objects.create(month=self.month, participant=member, team=team)
        return member

    def book(self, member):
        return BookSubmission.objects.create(
            month=self.month, participant=member, title=f"Book by {member.display_name}",
            author="Writer", book_format=BookSubmission.Format.PAPERBACK,
            completed_on=date(2026, 8, 5), submitted_pages=123,
        )

    def url(self, name, *args):
        return reverse(name, args=[self.group.slug, self.month.pk, *args])

    def approve_payload(self, book):
        response = self.client.get(self.url("submission-review", book.pk))
        self.assertEqual(response.status_code, 200)
        return {"status": "approved", "approved_pages": 123,
                "matching_version": response.context["matching_form"]["matching_version"].value()}

    def remove(self, enrollment, actor):
        return deactivate_participation(
            enrollment=enrollment, actor=actor, reason=MonthEnrollment.InactiveReason.REMOVED,
            ordinary_staff_removal=True,
        )

    def test_team_scope_applies_despite_group_oversight_and_preserves_public_results(self):
        for role in (Membership.Role.OWNER, Membership.Role.MODERATOR):
            self.host.role = role
            self.host.permission_overrides = {"manage_months": True}
            self.host.save(update_fields=["role", "permission_overrides"])
            self.assertTrue(can_view_reader_scores(self.host.user, self.month, self.a))
            self.assertFalse(can_view_reader_scores(self.host.user, self.month, self.b))
            self.assertFalse(can_view_team_standings(self.host.user, self.month, self.b))
            self.assertFalse(can_review_submission(self.host.user, self.other_book))
            self.assertFalse(can_manage_credit(self.host.user, self.other_book))
        self.assertTrue(can_review_submission(self.admin.user, self.other_book))
        self.client.force_login(self.host.user)
        for name, pk in (("submission-review", self.other_book.pk), ("submission-edit", self.other_book.pk),
                         ("submission-manage", self.other_book.pk), ("submission-remove", self.other_book.pk),
                         ("challenge-participant-detail", self.reader.pk),
                         ("challenge-registration-detail", self.month.enrollments.get(participant=self.reader).pk)):
            with self.subTest(route=name):
                self.assertEqual(self.client.get(self.url(name, pk)).status_code, 403)
                self.assertEqual(self.client.post(self.url(name, pk), {}).status_code, 403)
        self.assertEqual(self.client.get(self.url("challenge-general-settings")).status_code, 200)
        self.assertEqual(self.client.post(self.url("botm-match-synchronize")).status_code, 403)
        signup_url = self.url("challenge-signup-settings")
        self.assertEqual(self.client.get(signup_url).status_code, 200)
        self.assertEqual(self.client.post(signup_url, {"action": "save_tbr_settings"}).status_code, 403)
        self.assertNotContains(self.client.get(self.url("review-queue")), self.other_book.title)
        self.assertNotContains(self.client.get(self.url("team-detail", self.b.pk), {
            "show_discord": "1", "show_average": "1",
        }), "private-team-b-reader")
        self.assertNotContains(self.client.get(self.url("month-participant-list"), {"sort": "average"}),
                               "private-team-b-reader")
        self.month.team_standings_visibility = ChallengeMonth.CompetitionVisibility.EVERYBODY
        self.assertTrue(can_view_team_standings(self.host.user, self.month, self.b))
        self.assertFalse(can_view_reader_scores(self.host.user, self.month, self.b))
        for status in (ChallengeMonth.Status.COMPLETED, ChallengeMonth.Status.ARCHIVED):
            self.month.status = status
            self.assertTrue(can_view_reader_scores(self.host.user, self.month, self.b))
            self.assertFalse(can_review_submission(self.host.user, self.other_book))

    def test_authorized_removal_restores_and_reactivation_clears_scope(self):
        for actor in (self.host.user, self.other_host.user, self.reader.user):
            with self.subTest(actor=actor.username), self.assertRaises(PermissionDenied):
                self.remove(self.enrollment, actor)
        self.client.force_login(self.host.user)
        self.assertEqual(self.client.post(self.url("challenge-withdraw")).status_code, 403)
        self.enrollment.refresh_from_db()
        self.assertTrue(self.enrollment.is_active)
        self.remove(self.enrollment, self.admin.user)
        self.enrollment.refresh_from_db()
        self.assertFalse(self.enrollment.is_active)
        self.assertTrue(self.enrollment.host_scope_restored)
        self.assertEqual(self.enrollment.inactivated_by, self.admin.user)
        self.assertTrue(can_review_submission(self.host.user, self.other_book))
        activate_participation(month=self.month, participant=self.host, actor=self.admin.user,
                               origin=MonthEnrollment.Origin.STAFF)
        self.enrollment.refresh_from_db()
        self.assertFalse(self.enrollment.host_scope_restored)
        self.assertFalse(can_review_submission(self.host.user, self.other_book))
        self.assertFalse(can_review_submission(self.host.user, self.own_book))

    def test_recovery_removal_requires_separate_restoration_decision(self):
        operation = RecoveryRequest(
            actor=self.root, tier=2, action="enrollment.withdraw", target_type="MonthEnrollment",
            target_id=str(self.enrollment.pk), target_label="Host enrollment", reason="Historical correction",
            required_confirmation="Host enrollment", supplied_confirmation="Host enrollment",
        )
        set_enrollment_active(enrollment=self.enrollment, active=False, recovery_request=operation)
        self.enrollment.refresh_from_db()
        self.assertFalse(self.enrollment.host_scope_restored)
        self.assertFalse(can_review_submission(self.host.user, self.other_book))
        url = reverse("recovery-host-scope-restore", args=[self.enrollment.pk])
        self.client.force_login(self.admin.user)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.client.force_login(self.root)
        self.assertEqual(self.client.post(url, {
            "reason": "Restore operational role", "confirmation": f"{self.host.user.username} — {self.month.name}",
        }).status_code, 302)
        self.enrollment.refresh_from_db()
        self.assertTrue(self.enrollment.host_scope_restored)
        self.assertTrue(RecoveryOperation.objects.filter(
            actor=self.root, action="enrollment.host_scope_restored", target_id=str(self.enrollment.pk),
        ).exists())

    def test_ordinary_self_approval_and_stale_review(self):
        for member, book in ((self.host, self.own_book), (self.leader, self.book(self.leader))):
            self.client.force_login(member.user)
            data = self.approve_payload(book)
            url = self.url("submission-review", book.pk)
            self.assertEqual(self.client.post(url, data).status_code, 302)
            book.refresh_from_db()
            self.assertEqual((book.status, book.final_scored_pages, book.reviewed_by_id),
                             (BookSubmission.Status.APPROVED, 123, member.user_id))
            self.assertTrue(AuditEvent.objects.filter(
                action="submission.approved", object_id=str(book.pk), actor=member.user,
            ).exists())
            self.assertContains(self.client.post(url, data), "Nothing was saved")

    def test_scope_is_rechecked_when_team_changes_between_form_and_post(self):
        target = self.book(self.leader)
        self.client.force_login(self.host.user)
        data = self.approve_payload(target)
        with self.assertRaises(PermissionDenied):
            assign_participant_to_team(month=self.month, participant=self.host, team=self.b, actor=self.host.user)
        assign_participant_to_team(month=self.month, participant=self.host, team=self.b, actor=self.admin.user)
        self.assertEqual(self.client.post(self.url("submission-review", target.pk), data).status_code, 403)
        target.refresh_from_db()
        self.assertEqual(target.status, BookSubmission.Status.PENDING)
        self.assertTrue(can_review_submission(self.host.user, self.other_book))

    def test_game_target_is_scoped_at_service_boundary(self):
        self.month.games_enabled = True
        self.month.save(update_fields=["games_enabled"])
        game = Game.objects.create(
            month=self.month, name="Game", starts_at=timezone.now(),
            ends_at=timezone.now() + timezone.timedelta(hours=1), advertised_bonus_pages=20,
        )
        recipients, _ = preview_game_reward(game=game, actor=self.host.user, target_type="team", target_team=self.a)
        self.assertIn(self.host, recipients)
        for options in ({"target_type": "team", "target_team": self.b}, {"target_type": "challenge"}):
            with self.assertRaisesMessage(ValidationError, "outside your Host team authority"):
                preview_game_reward(game=game, actor=self.host.user, **options)

    def test_stale_removal_request_cannot_use_prior_team_scope(self):
        enrollment = self.month.enrollments.get(participant=self.other_host)
        assign_participant_to_team(month=self.month, participant=self.host, team=self.b, actor=self.admin.user)
        self.client.force_login(self.host.user)
        url = self.url("month-participant-remove", enrollment.pk)
        self.assertEqual(self.client.get(url).status_code, 200)
        assign_participant_to_team(month=self.month, participant=self.host, team=self.a, actor=self.admin.user)
        with self.assertRaises(PermissionDenied):
            self.remove(enrollment, self.host.user)
        self.assertEqual(self.client.post(url, {"reason": "Stale confirmation"}).status_code, 403)
        enrollment.refresh_from_db()
        self.assertTrue(enrollment.is_active)
        self.assertFalse(enrollment.host_scope_restored)

    def test_floater_cannot_reenroll_or_submit_and_historical_pages_are_preserved(self):
        from .score_aggregation import challenge_score_totals

        pending = self.book(self.reader)
        self.other_book.status = BookSubmission.Status.APPROVED
        self.other_book.approved_pages = 123
        self.other_book.save()
        enrollment = self.month.enrollments.get(participant=self.reader)
        self.remove(enrollment, self.admin.user)
        ChallengeStaffAssignment.objects.create(
            month=self.month, membership=self.reader, role=ChallengeStaffAssignment.Role.FLOATER,
        )
        self.assertEqual(challenge_score_totals(month=self.month)[self.reader.pk]["total_pages"], 123)
        self.assertTrue(can_review_submission(self.reader.user, self.own_book))
        with self.assertRaises(ValidationError):
            activate_participation(month=self.month, participant=self.reader, actor=self.admin.user,
                                   origin=MonthEnrollment.Origin.STAFF)
        with self.assertRaises(ValidationError):
            assign_participant_to_team(month=self.month, participant=self.reader, team=self.b,
                                       actor=self.admin.user)
        # Simulate a conflicting restored record to exercise the submission gate.
        MonthEnrollment.objects.filter(pk=enrollment.pk).update(is_active=True)
        self.client.force_login(self.reader.user)
        self.assertEqual(self.client.post(self.url("submission-create"), {}).status_code, 403)
        self.assertEqual(BookSubmission.objects.filter(month=self.month, participant=self.reader).count(), 2)
        self.client.force_login(self.admin.user)
        payload = self.approve_payload(pending)
        response = self.client.post(self.url("submission-review", pending.pk), payload)
        self.assertContains(response, "Nothing was saved")
        pending.refresh_from_db()
        self.assertEqual(pending.status, BookSubmission.Status.PENDING)
        self.assertEqual(challenge_score_totals(month=self.month)[self.reader.pk]["total_pages"], 123)


    def test_group_directory_does_not_leak_out_of_team_fields(self):
        past = ChallengeMonth.objects.create(
            group=self.group, name="Past", status=ChallengeMonth.Status.COMPLETED,
            starts_on=date(2026, 7, 1), ends_on=date(2026, 7, 31),
        )
        MonthEnrollment.objects.create(month=past, participant=self.reader)
        BookSubmission.objects.create(
            month=past, participant=self.reader, title="Past reading", author="Writer",
            book_format=BookSubmission.Format.PAPERBACK, completed_on=date(2026, 7, 10),
            submitted_pages=9876, approved_pages=9876, status=BookSubmission.Status.APPROVED,
        )
        self.other_book.status = BookSubmission.Status.APPROVED
        self.other_book.approved_pages = 123
        self.other_book.save()
        profile_url = reverse("participant-detail", args=[self.group.slug, self.reader.pk])
        directory_url = reverse("participant-list", args=[self.group.slug])
        permissions_url = reverse("participant-permissions-edit", args=[self.group.slug, self.reader.pk])
        for role in (Membership.Role.OWNER, Membership.Role.MODERATOR):
            with self.subTest(role=role):
                self.host.role = role
                self.host.permission_overrides = {"manage_permissions": True}
                self.host.save(update_fields=["role", "permission_overrides"])
                self.client.force_login(self.host.user)
                directory = self.client.get(directory_url, {"show_average": "1"})
                self.assertContains(directory, self.reader.user.username)
                self.assertContains(directory, permissions_url)
                self.assertNotContains(directory, "9876")
                row = next(item for item in directory.context["participants"] if item.pk == self.reader.pk)
                self.assertIsNone(row.planning)
                profile = self.client.get(profile_url, {"show_discord": "1"})
                self.assertContains(profile, self.reader.user.username)
                self.assertNotContains(profile, "private-team-b-reader")
                self.assertNotContains(profile, "Verified Pages")
                self.assertNotContains(profile, "Approved Books")
                with self.assertRaises(KeyError):
                    profile.context["approved_pages"]
                self.assertEqual(self.client.get(permissions_url).status_code, 200)

        # The existing Group public Discord choice and public Challenge audience still work.
        UserProfile.objects.filter(user=self.reader.user).update(discord_username_is_public=True)
        self.assertContains(self.client.get(profile_url), "private-team-b-reader")
        ChallengeMonth.objects.filter(pk=self.month.pk).update(
            reader_scores_visibility=ChallengeMonth.CompetitionVisibility.EVERYBODY,
            team_standings_visibility=ChallengeMonth.CompetitionVisibility.EVERYBODY,
        )
        roster = self.client.get(self.url("team-detail", self.b.pk))
        self.assertTrue(roster.context["can_view_reader_scores"])
        self.assertEqual(roster.context["team_total"], 123)
        self.assertNotContains(self.client.get(profile_url), "Verified Pages")

    def test_restoration_service_checks_reason_and_locked_confirmation(self):
        MonthEnrollment.objects.filter(pk=self.enrollment.pk).update(is_active=False, inactive_reason="removed")
        label = f"{self.host.user.username} — {self.month.name}"
        operation = RecoveryRequest(
            actor=self.root, tier=2, action="enrollment.host_scope_restored", target_type="MonthEnrollment",
            target_id=str(self.enrollment.pk), target_label=label, reason="Restore operational scope",
            required_confirmation="", supplied_confirmation=label,
        )
        with self.assertRaises(PermissionDenied):
            restore_inactive_host_scope(enrollment=self.enrollment, recovery_request=replace(operation, actor=self.admin.user))
        for changes in (
            {"reason": "  "}, {"supplied_confirmation": ""},
            {"required_confirmation": "forged label", "supplied_confirmation": "forged label"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                restore_inactive_host_scope(enrollment=self.enrollment, recovery_request=replace(operation, operation_id=uuid4(), **changes))
            self.enrollment.refresh_from_db()
            self.assertFalse(self.enrollment.host_scope_restored)
        restore_inactive_host_scope(enrollment=self.enrollment, recovery_request=operation)
        self.enrollment.refresh_from_db()
        self.assertTrue(self.enrollment.host_scope_restored)
        self.assertTrue(RecoveryOperation.objects.filter(
            actor=self.root, action="enrollment.host_scope_restored",
            target_id=str(self.enrollment.pk), result=RecoveryOperation.Result.SUCCEEDED,
        ).exists())
        self.assertEqual(AuditEvent.objects.filter(
            actor=self.root, action="enrollment.host_scope_restored",
            object_id=str(self.enrollment.pk),
        ).count(), 1)


class HostScopeConcurrencyTests(TransactionTestCase):
    setUp = ParticipatingHostScopeTests.setUp
    member = ParticipatingHostScopeTests.member
    book = ParticipatingHostScopeTests.book
    remove = ParticipatingHostScopeTests.remove

    @skipUnlessDBFeature("has_select_for_update")
    def test_removal_waiting_for_roster_change_rechecks_scope(self):
        from . import participation

        enrollment = self.month.enrollments.get(participant=self.other_host)
        actor = self.host.user
        administrator = self.admin.user
        assign_participant_to_team(month=self.month, participant=self.host, team=self.b, actor=administrator)
        attempting_lock = Event()
        lock_month = participation._lock_month

        def observe_lock(month_id):
            attempting_lock.set()
            return lock_month(month_id)

        def stale_removal():
            try:
                self.remove(enrollment, actor)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as workers:
            with transaction.atomic():
                assign_participant_to_team(month=self.month, participant=self.host, team=self.a, actor=administrator)
                with patch.object(participation, "_lock_month", side_effect=observe_lock):
                    future = workers.submit(stale_removal)
                    self.assertTrue(attempting_lock.wait(timeout=5))
                    self.assertFalse(future.done())
            with self.assertRaises(PermissionDenied):
                future.result(timeout=5)
        enrollment.refresh_from_db()
        self.assertTrue(enrollment.is_active)
        self.assertFalse(enrollment.host_scope_restored)
