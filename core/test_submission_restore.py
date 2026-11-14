"""Ordinary staff restoration shares recovery reconciliation without recovery authority."""
import json
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from . import test_submission_hotfix as fixture
from . import test_submission_editing as editing
from .models import (
    AuditEvent, BookSubmission, ChallengeMonth, ChallengeStaffAssignment,
    HardcoverSyncOutbox, Membership, ModifierProvenance, MonthEnrollment, RecoveryOperation,
    Team, TeamAssignment, ThemeClaim, MonthTheme,
)
from .permissions import can_restore_submission
from .recovery_scoring import restore_submission_as_staff, staff_restore_confirmation
from .score_aggregation import challenge_score_totals


class SubmissionRestoreTests(TestCase):
    setUp = fixture.SubmissionHotfixTests.setUp
    actor = editing.SubmissionEditingTests.actor
    submission = editing.SubmissionEditingTests.submission

    def void(self, item, actor):
        self.client.force_login(actor)
        url = reverse("submission-remove", args=["hotfix", self.month.pk, item.pk])
        self.assertEqual(self.client.post(url, {"reason": "Duplicate entry"}).status_code, 302)
        item.refresh_from_db()
        return item

    def restore_url(self, item):
        return reverse("submission-restore", args=["hotfix", self.month.pk, item.pk])

    def restore(self, item, actor, **kwargs):
        return restore_submission_as_staff(
            submission=item, actor=actor,
            confirmation=kwargs.pop("confirmation", staff_restore_confirmation(item, actor)), **kwargs,
        )

    def test_void_roles_restore_and_preserve_status_history_and_catalog(self):
        for role in ("platform", "owner", "moderator", "host", "floater", "team_leader"):
            with self.subTest(role=role):
                actor = self.actor(role)
                item = self.void(self.submission(catalog=True), actor)
                old = list(AuditEvent.objects.filter(action="submission.removed").values())
                outbox = list(HardcoverSyncOutbox.objects.values())
                self.assertTrue(can_restore_submission(actor, item))
                page = self.client.get(self.restore_url(item))
                self.assertContains(page, "Restoration reason (optional)")
                self.assertEqual(self.client.post(self.restore_url(item), {
                    "confirmation": page.context["confirmation"],
                }).status_code, 302)
                item.refresh_from_db()
                self.assertFalse(item.is_removed)
                self.assertEqual(item.status, "approved")
                self.assertEqual(item.approved_pages, 350)
                self.assertEqual(item.catalog_edition_id, self.edition.pk)
                self.assertEqual(list(AuditEvent.objects.filter(action="submission.removed").values()), old)
                self.assertEqual(list(HardcoverSyncOutbox.objects.values()), outbox)
                self.assertFalse(RecoveryOperation.objects.exists())
                payload = json.loads(AuditEvent.objects.get(action="submission.restored", object_id=str(item.pk)).summary)
                self.assertEqual(payload["previous_removal"]["reason"], "Duplicate entry")
                history = reverse("submission-history", args=["hotfix", self.month.pk, item.pk])
                self.client.force_login(self.reader.user)
                history_page = self.client.get(history)
                self.assertContains(history_page, "Removal reason: Duplicate entry")
                self.assertContains(history_page, "Restored")
                self.assertNotContains(history_page, "reader_score_before")

    def test_lifecycle_and_preserved_statuses(self):
        actor = self.actor("platform")
        for state in ("draft", "upcoming", "active", "finalizing", "completed", "archived"):
            for status in ("approved", "pending", "rejected"):
                with self.subTest(state=state, status=status):
                    ChallengeMonth.objects.filter(pk=self.month.pk).update(status=state)
                    self.month.refresh_from_db()
                    item = self.submission(status=status)
                    BookSubmission.objects.filter(pk=item.pk).update(is_removed=True, removed_at=timezone.now())
                    item.refresh_from_db()
                    if state in {"draft", "upcoming", "active"}:
                        self.restore(item, actor)
                        item.refresh_from_db()
                        self.assertEqual(item.status, status)
                        self.assertFalse(item.is_removed)
                        if status != "approved":
                            self.assertIsNone(item.final_scored_pages)
                    else:
                        with self.assertRaises(PermissionDenied):
                            self.restore(item, actor)
                        item.refresh_from_db()
                        self.assertTrue(item.is_removed)

    def test_denials_and_stale_team_authority(self):
        leader = self.actor("team_leader")
        item = self.void(self.submission(), leader)
        token = staff_restore_confirmation(item, leader)
        for actor in (self.reader.user, self.actor("unassigned")):
            with self.assertRaises(PermissionDenied):
                self.restore(item, actor)
        # Legacy capability flags do not grant submission authority.
        member = Membership.objects.get(user=actor)
        member.permission_overrides = {"manage_months": True, "remove_content": True}
        member.save(update_fields=["permission_overrides"])
        with self.assertRaises(PermissionDenied):
            self.restore(item, actor)
        TeamAssignment.objects.filter(participant=self.reader).update(ended_at=timezone.now())
        with self.assertRaises(PermissionDenied):
            self.restore(item, leader, confirmation=token)
        ChallengeStaffAssignment.objects.filter(membership__user=leader).update(ended_at=timezone.now())
        with self.assertRaises(PermissionDenied):
            self.restore(item, leader)
        item.refresh_from_db()
        self.assertTrue(item.is_removed)

    def test_duplicate_stale_tampered_and_rollback(self):
        actor = self.actor("host")
        item = self.void(self.submission(catalog=True), actor)
        old_token = staff_restore_confirmation(item, actor)
        with self.assertRaises(ValidationError):
            self.restore(item, actor, confirmation="tampered")
        with patch("core.recovery_scoring._synchronize_submission_sources", side_effect=ValidationError("failed")):
            with self.assertRaises(ValidationError):
                self.restore(item, actor)
        item.refresh_from_db()
        self.assertTrue(item.is_removed)
        self.assertFalse(AuditEvent.objects.filter(action="submission.restored").exists())
        self.restore(item, actor)
        with self.assertRaises(ValidationError):
            self.restore(item, actor, confirmation=old_token)
        item.refresh_from_db()
        self.void(item, actor)
        with self.assertRaises(ValidationError):
            self.restore(item, actor, confirmation=old_token)
        self.assertTrue(BookSubmission.objects.get(pk=item.pk).is_removed)

    def test_withdrawn_and_reassigned_readers_are_blocked_before_scoring(self):
        actor = self.actor("owner")
        item = self.void(self.submission(catalog=True), actor)
        enrollment = MonthEnrollment.objects.get(month=self.month, participant=self.reader)
        enrollment.is_active = False
        enrollment.save(update_fields=["is_active"])
        before = list(ModifierProvenance.objects.values())
        with self.assertRaises(ValidationError):
            self.restore(item, actor)
        enrollment.is_active = True
        enrollment.save(update_fields=["is_active"])
        team = Team.objects.create(month=self.month, name="Historical team")
        TeamAssignment.objects.create(month=self.month, participant=self.reader, team=team, ended_at=timezone.now())
        with self.assertRaises(ValidationError):
            self.restore(item, actor)
        self.assertEqual(list(ModifierProvenance.objects.values()), before)
        self.assertTrue(BookSubmission.objects.get(pk=item.pk).is_removed)

    def test_csrf_and_endpoint_denial(self):
        actor = self.actor("host")
        item = self.void(self.submission(), actor)
        client = Client(enforce_csrf_checks=True)
        client.force_login(actor)
        self.assertEqual(client.post(self.restore_url(item), {
            "confirmation": staff_restore_confirmation(item, actor),
        }).status_code, 403)
        self.client.force_login(self.reader.user)
        self.assertEqual(self.client.get(self.restore_url(item)).status_code, 403)
        self.assertEqual(self.client.post(self.restore_url(item), {}).status_code, 403)

    def test_participating_host_overlap_and_cross_group_remain_restricted(self):
        from django.contrib.auth import get_user_model
        from .models import ReadingGroup
        host = self.actor("host")
        member = Membership.objects.get(user=host)
        member.role = "owner"
        member.save(update_fields=["role"])
        team = Team.objects.create(month=self.month, name="Host team")
        MonthEnrollment.objects.create(month=self.month, participant=member)
        TeamAssignment.objects.create(month=self.month, team=team, participant=member)
        item = self.submission()
        BookSubmission.objects.filter(pk=item.pk).update(is_removed=True, removed_at=timezone.now())
        item.refresh_from_db()
        with self.assertRaises(PermissionDenied):
            self.restore(item, host)
        TeamAssignment.objects.create(month=self.month, team=team, participant=self.reader)
        self.restore(item, host)
        item.refresh_from_db()
        self.assertFalse(item.is_removed)

        outsider = get_user_model().objects.create_user("outside-restore-group")
        group = ReadingGroup.objects.create(name="Other", slug="other-restore-group")
        Membership.objects.create(group=group, user=outsider, role="owner", display_name="Owner")
        BookSubmission.objects.filter(pk=item.pk).update(is_removed=True, removed_at=timezone.now())
        item.refresh_from_db()
        with self.assertRaises(PermissionDenied):
            self.restore(item, outsider)
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(self.restore_url(item)).status_code, 403)
        mismatch = reverse("submission-restore", args=[group.slug, self.month.pk, item.pk])
        self.assertEqual(self.client.get(mismatch).status_code, 404)

    def test_existing_bonus_evidence_and_qualifications_are_reconciled(self):
        from .models import BotmBook, ImmersiveRequest
        from .botm_matching import synchronize_submission
        from .scoring import refresh_submission_score
        actor = self.actor("host")
        item = self.submission(catalog=True)
        self.month.botm_enabled = True
        self.month.save(update_fields=["botm_enabled"])
        BotmBook.objects.create(
            month=self.month, position=1, catalog_book=self.book,
            title_snapshot=self.book.title, author_snapshot=self.book.author,
            page_count_snapshot=350, bonus_pages=25,
        )
        theme = MonthTheme.objects.create(
            month=self.month, name="Original Theme", starts_on=self.month.starts_on,
            ends_on=self.month.ends_on, bonus_rule="legacy_fixed", bonus_pages=30,
        )
        claim = ThemeClaim.objects.create(
            submission=item, theme=theme, status="approved", approved_bonus_pages=30,
        )
        request = ImmersiveRequest.objects.create(
            submission=item, requester=self.reader.user, captured_percent=10,
            status="approved", reviewed_by=actor, reviewed_at=timezone.now(),
        )
        synchronize_submission(item)
        refresh_submission_score(item)
        before_score = challenge_score_totals(month=self.month, participant_ids=[self.reader.pk])
        evidence = list(ThemeClaim.objects.filter(pk=claim.pk).values())
        self.void(item, actor)
        self.assertEqual(challenge_score_totals(month=self.month, participant_ids=[self.reader.pk])[self.reader.pk]["total_pages"], 0)
        self.restore(item, actor, reason="Restore duplicate void")
        item.refresh_from_db()
        request.refresh_from_db()
        self.assertEqual(challenge_score_totals(month=self.month, participant_ids=[self.reader.pk]), before_score)
        self.assertEqual(list(ThemeClaim.objects.filter(pk=claim.pk).values()), evidence)
        self.assertTrue(request.is_active)
        self.assertTrue(item.botm_matches.filter(is_qualifying=True).exists())
        self.assertTrue(item.personal_tbr_matches.filter(is_qualifying=True).exists())
        self.assertFalse(HardcoverSyncOutbox.objects.exists())

    def test_competing_tbr_qualification_does_not_duplicate_credit(self):
        actor = self.actor("host")
        item = self.void(self.submission(catalog=True), actor)
        competing = self.submission(catalog=True)
        self.assertTrue(competing.personal_tbr_matches.filter(is_qualifying=True).exists())
        self.restore(item, actor)
        self.assertEqual(self.month.personal_tbr_matches.filter(is_qualifying=True).count(), 1)
        competing.refresh_from_db()
        self.assertFalse(competing.is_removed)
        self.assertEqual(competing.approved_pages, 350)
