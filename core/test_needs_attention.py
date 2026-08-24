from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import BookSubmission, ChallengeMonth, ChallengeStaffAssignment, Membership, MonthTheme, ReadingGroup, Team, TeamAssignment, ThemeClaim
from .review_attention import needs_attention_summary


class NeedsAttentionTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner_user = User.objects.create_user("attention-owner", password="test-password")
        self.moderator_user = User.objects.create_user("attention-moderator", password="test-password")
        self.host_user = User.objects.create_user("attention-host", password="test-password")
        self.leader_user = User.objects.create_user("attention-leader", password="test-password")
        self.floater_user = User.objects.create_user("attention-floater", password="test-password")
        self.reader_one_user = User.objects.create_user("attention-reader-one", password="test-password")
        self.reader_two_user = User.objects.create_user("attention-reader-two", password="test-password")
        self.member_user = User.objects.create_user("attention-member", password="test-password")
        self.platform_owner = User.objects.create_superuser("attention-platform", password="test-password")
        self.group = ReadingGroup.objects.create(name="Attention Group", slug="attention-group")

        def member(user, name, role=Membership.Role.MEMBER, overrides=None):
            return Membership.objects.create(group=self.group, user=user, display_name=name, role=role, permission_overrides=overrides or {})

        self.owner = member(self.owner_user, "Owner", Membership.Role.OWNER)
        self.moderator = member(self.moderator_user, "Moderator", Membership.Role.MODERATOR, {"review_submissions": True})
        self.host = member(self.host_user, "Host")
        self.leader = member(self.leader_user, "Leader")
        self.floater = member(self.floater_user, "Floater")
        self.reader_one = member(self.reader_one_user, "Reader One")
        self.reader_two = member(self.reader_two_user, "Reader Two")
        self.member = member(self.member_user, "Member")
        self.month = ChallengeMonth.objects.create(group=self.group, name="Attention Month", starts_on=date(2026, 8, 1), ends_on=date(2026, 8, 31), status=ChallengeMonth.Status.OPEN)
        self.other_month = ChallengeMonth.objects.create(group=self.group, name="Second Attention Month", starts_on=date(2026, 9, 1), ends_on=date(2026, 9, 30), status=ChallengeMonth.Status.CLOSED)
        self.unauthorized_month = ChallengeMonth.objects.create(group=self.group, name="Unauthorized Month", starts_on=date(2026, 10, 1), ends_on=date(2026, 10, 31), status=ChallengeMonth.Status.OPEN)
        self.team_one = Team.objects.create(month=self.month, name="North Team")
        self.team_two = Team.objects.create(month=self.month, name="South Team")
        for participant, team in ((self.leader, self.team_one), (self.reader_one, self.team_one), (self.reader_two, self.team_two)):
            TeamAssignment.objects.create(month=self.month, participant=participant, team=team)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.host, role=ChallengeStaffAssignment.Role.HOST, assigned_by=self.owner_user)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.leader, team=self.team_one, role=ChallengeStaffAssignment.Role.TEAM_LEADER, assigned_by=self.host_user)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.floater, role=ChallengeStaffAssignment.Role.FLOATER, assigned_by=self.host_user)
        self.theme = MonthTheme.objects.create(month=self.month, name="Attention Theme", starts_on=date(2026, 8, 1), ends_on=date(2026, 8, 31), bonus_pages=25)
        self.north_submission = self.make_submission(self.month, self.reader_one, "North Work")
        self.south_submission = self.make_submission(self.month, self.reader_two, "South Work")
        self.north_claim = ThemeClaim.objects.create(submission=self.north_submission, theme=self.theme)
        self.south_claim = ThemeClaim.objects.create(submission=self.south_submission, theme=self.theme)
        self.unauthorized_submission = self.make_submission(self.unauthorized_month, self.member, "Unauthorized Work")

    def make_submission(self, month, participant, title):
        return BookSubmission.objects.create(month=month, participant=participant, title=title, author="Author", book_format=BookSubmission.Format.EBOOK, completed_on=month.starts_on, submitted_pages=200)

    def test_host_team_leader_and_floater_receive_correct_aggregate_counts(self):
        self.assertEqual(needs_attention_summary(self.host_user)["total"], 4)
        self.assertEqual(needs_attention_summary(self.leader_user)["total"], 2)
        self.assertEqual(needs_attention_summary(self.floater_user)["total"], 4)

    def test_host_plus_team_leader_does_not_double_count(self):
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.leader, role=ChallengeStaffAssignment.Role.HOST, assigned_by=self.owner_user)
        summary = needs_attention_summary(self.leader_user)
        self.assertEqual(summary["total"], 4)
        self.assertEqual(len(summary["challenges"]), 1)
        self.assertEqual(summary["challenges"][0]["scope_label"], "Entire Challenge")

    def test_multiple_challenges_aggregate_and_unauthorized_challenge_is_excluded(self):
        ChallengeStaffAssignment.objects.create(month=self.other_month, membership=self.host, role=ChallengeStaffAssignment.Role.HOST, assigned_by=self.owner_user)
        other_submission = self.make_submission(self.other_month, self.member, "Second Challenge Work")
        summary = needs_attention_summary(self.host_user)
        self.assertEqual(summary["total"], 5)
        self.assertEqual({item["month"] for item in summary["challenges"]}, {self.month, self.other_month})
        self.assertNotIn(self.unauthorized_month, {item["month"] for item in summary["challenges"]})
        self.assertIsNotNone(other_submission.pk)

    def test_unstaffed_group_authority_receives_no_count(self):
        for user in (self.owner_user, self.moderator_user, self.member_user, self.reader_one_user):
            self.assertEqual(needs_attention_summary(user)["total"], 0)

    def test_platform_owner_receives_installation_wide_actionable_count(self):
        summary = needs_attention_summary(self.platform_owner)
        self.assertEqual(summary["total"], 5)
        self.assertEqual(
            {item["month"] for item in summary["challenges"]},
            {self.month, self.unauthorized_month},
        )
        self.assertTrue(all(item["scope_label"] == "Entire Challenge" for item in summary["challenges"]))

    def test_ended_staffing_immediately_removes_scope(self):
        assignment = ChallengeStaffAssignment.objects.get(month=self.month, membership=self.floater, role=ChallengeStaffAssignment.Role.FLOATER)
        assignment.ended_at = timezone.now()
        assignment.ended_by = self.host_user
        assignment.save()
        self.assertEqual(needs_attention_summary(self.floater_user)["total"], 0)

    def test_navigation_indicator_appears_updates_after_review_and_disappears_at_zero(self):
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Needs Attention")
        self.assertContains(response, '<span class="needs-attention-count">2</span>', html=True)
        review_url = reverse("submission-review", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.north_submission.pk})
        result = self.client.post(review_url, {
            "approved_pages": 190, "status": BookSubmission.Status.APPROVED, "verification_url": "", "review_notes": "Done",
            "claims-TOTAL_FORMS": "1", "claims-INITIAL_FORMS": "1", "claims-MIN_NUM_FORMS": "0", "claims-MAX_NUM_FORMS": "1000",
            "claims-0-id": str(self.north_claim.pk), "claims-0-submission": str(self.north_submission.pk), "claims-0-status": ThemeClaim.Status.APPROVED,
        })
        self.assertRedirects(result, reverse("review-queue", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}))
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, "Needs Attention")
        new_submission = self.make_submission(self.month, self.reader_one, "New North Work")
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Needs Attention")
        self.assertContains(response, '<span class="needs-attention-count">1</span>', html=True)
        self.assertIsNotNone(new_submission.pk)

    def test_intermediary_page_lists_only_authorized_challenges_and_links_to_scoped_queue(self):
        ChallengeStaffAssignment.objects.create(month=self.other_month, membership=self.leader, role=ChallengeStaffAssignment.Role.HOST, assigned_by=self.owner_user)
        self.make_submission(self.other_month, self.member, "Second Challenge Work")
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("needs-attention"))
        self.assertContains(response, self.month.name)
        self.assertContains(response, self.other_month.name)
        self.assertNotContains(response, self.unauthorized_month.name)
        self.assertContains(response, "Review scope: North Team")
        self.assertNotContains(response, "South Work")
        queue = self.client.get(reverse("review-queue", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}))
        self.assertContains(queue, "North Work")
        self.assertNotContains(queue, "South Work")
