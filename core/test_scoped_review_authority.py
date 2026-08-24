from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import AuditEvent, BookSubmission, ChallengeMonth, ChallengeStaffAssignment, Membership, MonthEnrollment, MonthTheme, ReadingGroup, Team, TeamAssignment, ThemeClaim
from .permissions import can_review_challenge, can_view_team_stats


class ScopedReviewAuthorityTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner_user = User.objects.create_user("scope-owner", password="test-password")
        self.moderator_user = User.objects.create_user("scope-moderator", password="test-password")
        self.host_user = User.objects.create_user("scope-host", password="test-password")
        self.leader_one_user = User.objects.create_user("scope-leader-one", password="test-password")
        self.leader_two_user = User.objects.create_user("scope-leader-two", password="test-password")
        self.floater_user = User.objects.create_user("scope-floater", password="test-password")
        self.reader_one_user = User.objects.create_user("scope-reader-one", password="test-password")
        self.reader_two_user = User.objects.create_user("scope-reader-two", password="test-password")
        self.other_host_user = User.objects.create_user("scope-other-host", password="test-password")
        self.ended_user = User.objects.create_user("scope-ended", password="test-password")
        self.platform_owner = User.objects.create_superuser("scope-platform", password="test-password")
        self.group = ReadingGroup.objects.create(name="Scope Group", slug="scope-group")

        def member(user, name, role=Membership.Role.MEMBER, overrides=None):
            return Membership.objects.create(group=self.group, user=user, display_name=name, role=role, permission_overrides=overrides or {})

        self.owner = member(self.owner_user, "Owner", Membership.Role.OWNER)
        self.moderator = member(self.moderator_user, "Moderator", Membership.Role.MODERATOR, {"review_submissions": True})
        self.host = member(self.host_user, "Host")
        self.leader_one = member(self.leader_one_user, "Leader One")
        self.leader_two = member(self.leader_two_user, "Leader Two")
        self.floater = member(self.floater_user, "Floater")
        self.reader_one = member(self.reader_one_user, "Reader One")
        self.reader_two = member(self.reader_two_user, "Reader Two")
        self.other_host = member(self.other_host_user, "Other Host")
        self.ended = member(self.ended_user, "Ended Staff")
        self.month = ChallengeMonth.objects.create(group=self.group, name="Scope Month", starts_on=date(2026, 8, 1), ends_on=date(2026, 8, 31), status=ChallengeMonth.Status.OPEN)
        self.other_month = ChallengeMonth.objects.create(group=self.group, name="Other Scope Month", starts_on=date(2026, 9, 1), ends_on=date(2026, 9, 30), status=ChallengeMonth.Status.OPEN)
        self.team_one = Team.objects.create(month=self.month, name="North Team")
        self.team_two = Team.objects.create(month=self.month, name="South Team")
        for participant, team in ((self.leader_one, self.team_one), (self.leader_two, self.team_one), (self.reader_one, self.team_one), (self.reader_two, self.team_two)):
            TeamAssignment.objects.create(month=self.month, participant=participant, team=team)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.host, role=ChallengeStaffAssignment.Role.HOST, assigned_by=self.owner_user)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.leader_one, team=self.team_one, role=ChallengeStaffAssignment.Role.TEAM_LEADER, assigned_by=self.host_user)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.leader_two, team=self.team_one, role=ChallengeStaffAssignment.Role.TEAM_LEADER, assigned_by=self.host_user)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.floater, role=ChallengeStaffAssignment.Role.FLOATER, assigned_by=self.host_user)
        ChallengeStaffAssignment.objects.create(month=self.other_month, membership=self.other_host, role=ChallengeStaffAssignment.Role.HOST, assigned_by=self.owner_user)
        for role in (ChallengeStaffAssignment.Role.HOST, ChallengeStaffAssignment.Role.FLOATER):
            assignment = ChallengeStaffAssignment.objects.create(month=self.month, membership=self.ended, role=role, assigned_by=self.owner_user)
            assignment.ended_at = timezone.now()
            assignment.ended_by = self.owner_user
            assignment.save()
        self.theme = MonthTheme.objects.create(month=self.month, name="Scope Theme", starts_on=date(2026, 8, 1), ends_on=date(2026, 8, 31), bonus_pages=40)
        self.submission_one = self.make_submission(self.reader_one, "North Pending")
        self.submission_two = self.make_submission(self.reader_two, "South Pending")
        self.claim_one = ThemeClaim.objects.create(submission=self.submission_one, theme=self.theme)
        self.claim_two = ThemeClaim.objects.create(submission=self.submission_two, theme=self.theme)
        self.queue_url = reverse("review-queue", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})

    def make_submission(self, participant, title):
        return BookSubmission.objects.create(
            month=self.month,
            participant=participant,
            title=title,
            author="Author",
            book_format=BookSubmission.Format.EBOOK,
            completed_on=date(2026, 8, 10),
            submitted_pages=300,
        )

    def review_url(self, submission):
        return reverse("submission-review", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": submission.pk})

    def review_payload(self, submission, status=BookSubmission.Status.APPROVED, approved_pages=275, claim=None, claim_status=ThemeClaim.Status.APPROVED):
        data = {"approved_pages": approved_pages, "status": status, "verification_url": "", "review_notes": "Scoped review"}
        if claim:
            data.update({
                "claims-TOTAL_FORMS": "1", "claims-INITIAL_FORMS": "1", "claims-MIN_NUM_FORMS": "0", "claims-MAX_NUM_FORMS": "1000",
                "claims-0-id": str(claim.pk), "claims-0-submission": str(submission.pk), "claims-0-status": claim_status,
            })
        return data

    def assert_queue_scope(self, user, visible, hidden=()):
        self.client.force_login(user)
        response = self.client.get(self.queue_url)
        self.assertEqual(response.status_code, 200)
        for title in visible:
            self.assertContains(response, title)
        for title in hidden:
            self.assertNotContains(response, title)
        return response

    def test_host_sees_and_reviews_manual_submissions_and_claims_challenge_wide(self):
        response = self.assert_queue_scope(self.host_user, ("North Pending", "South Pending"))
        self.assertContains(response, "Review scope: Entire Challenge")
        submitted_at = self.submission_two.submitted_at
        result = self.client.post(self.review_url(self.submission_two), self.review_payload(self.submission_two, claim=self.claim_two))
        self.assertRedirects(result, self.queue_url)
        self.submission_two.refresh_from_db()
        self.claim_two.refresh_from_db()
        self.assertEqual(self.submission_two.reviewed_by, self.host_user)
        self.assertEqual(self.claim_two.reviewed_by, self.host_user)
        self.assertEqual(self.submission_two.submitted_at, submitted_at)
        self.assertEqual(self.submission_two.approved_pages, 275)
        self.assertEqual(self.submission_two.bonus_pages, 40)
        self.assertEqual(self.submission_two.final_scored_pages, 315)
        self.assertTrue(AuditEvent.objects.filter(actor=self.host_user, action="submission.approved", object_id=str(self.submission_two.pk)).exists())

    def test_team_leaders_share_own_team_scope_for_manual_and_theme_review(self):
        for user in (self.leader_one_user, self.leader_two_user):
            response = self.assert_queue_scope(user, ("North Pending",), ("South Pending",))
            self.assertContains(response, "Review scope: North Team")
            self.assertEqual(self.client.get(self.review_url(self.submission_two)).status_code, 403)
            self.assertEqual(self.client.post(self.review_url(self.submission_two), self.review_payload(self.submission_two, claim=self.claim_two)).status_code, 403)
        self.client.force_login(self.leader_one_user)
        self.client.post(self.review_url(self.submission_one), self.review_payload(self.submission_one, claim=self.claim_one))
        self.claim_one.refresh_from_db()
        self.assertEqual(self.claim_one.reviewed_by, self.leader_one_user)

    def test_floater_reviews_challenge_wide_without_other_authority(self):
        self.assert_queue_scope(self.floater_user, ("North Pending", "South Pending"))
        result = self.client.post(self.review_url(self.submission_two), self.review_payload(self.submission_two, status=BookSubmission.Status.REJECTED, approved_pages="", claim=self.claim_two, claim_status=ThemeClaim.Status.REJECTED))
        self.assertRedirects(result, self.queue_url)
        self.submission_two.refresh_from_db()
        self.assertEqual(self.submission_two.reviewed_by, self.floater_user)
        self.assertFalse(MonthEnrollment.objects.filter(month=self.month, participant=self.floater).exists())
        self.assertFalse(TeamAssignment.objects.filter(month=self.month, participant=self.floater).exists())
        self.assertFalse(can_view_team_stats(self.floater_user, self.month))
        self.assertEqual(self.client.post(reverse("challenge-floater-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}), {"membership": self.owner.pk}).status_code, 403)
        self.assertEqual(self.client.get(reverse("submission-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})).status_code, 302)

    def test_host_plus_team_leader_receives_host_wide_scope(self):
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.leader_one, role=ChallengeStaffAssignment.Role.HOST, assigned_by=self.owner_user)
        self.assert_queue_scope(self.leader_one_user, ("North Pending", "South Pending"))
        self.assertEqual(self.client.get(self.review_url(self.submission_two)).status_code, 200)

    def test_group_authority_without_staffing_cannot_review(self):
        for user in (self.reader_one_user, self.owner_user, self.moderator_user, self.other_host_user, self.ended_user):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.queue_url).status_code, 403)
            self.assertEqual(self.client.get(self.review_url(self.submission_one)).status_code, 403)
            self.assertFalse(can_review_challenge(user, self.month))

    def test_platform_owner_has_challenge_wide_review_override(self):
        self.assertTrue(can_review_challenge(self.platform_owner, self.month))
        self.assert_queue_scope(self.platform_owner, ("North Pending", "South Pending"))
        response = self.client.post(
            self.review_url(self.submission_two),
            self.review_payload(self.submission_two, claim=self.claim_two),
        )
        self.assertRedirects(response, self.queue_url)
        self.submission_two.refresh_from_db()
        self.claim_two.refresh_from_db()
        self.assertEqual(self.submission_two.reviewed_by, self.platform_owner)
        self.assertEqual(self.claim_two.reviewed_by, self.platform_owner)

    def test_ended_team_leader_staffing_ends_review_scope(self):
        leader = ChallengeStaffAssignment.objects.get(month=self.month, membership=self.leader_one, role=ChallengeStaffAssignment.Role.TEAM_LEADER)
        leader.ended_at = timezone.now()
        leader.ended_by = self.host_user
        leader.save()
        self.client.force_login(self.leader_one_user)
        self.assertEqual(self.client.get(self.queue_url).status_code, 403)
