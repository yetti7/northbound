from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import AuditEvent, BookSubmission, ChallengeMonth, ChallengeStaffAssignment, Membership, MonthEnrollment, MonthTheme, ReadingGroup, Team, TeamAssignment, ThemeClaim
from .permissions import can_manage_challenge_hosts, can_operate_challenge, is_challenge_host
from .review_attention import needs_attention_summary


class PlatformOwnerChallengeOverrideTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.platform_owner = User.objects.create_superuser("override-platform", password="test-password")
        self.owner_user = User.objects.create_user("override-owner", password="test-password")
        self.moderator_user = User.objects.create_user("override-moderator", password="test-password")
        self.reader_user = User.objects.create_user("override-reader", password="test-password")
        self.second_reader_user = User.objects.create_user("override-reader-two", password="test-password")
        self.leader_user = User.objects.create_user("override-leader", password="test-password")
        self.floater_user = User.objects.create_user("override-floater", password="test-password")
        self.host_candidate_user = User.objects.create_user("override-host-candidate", password="test-password")
        self.group = ReadingGroup.objects.create(name="Override Group", slug="override-group")

        def member(user, name, role=Membership.Role.MEMBER, overrides=None):
            return Membership.objects.create(
                group=self.group,
                user=user,
                display_name=name,
                role=role,
                permission_overrides=overrides or {},
            )

        self.owner = member(self.owner_user, "Owner", Membership.Role.OWNER)
        self.moderator = member(
            self.moderator_user,
            "Moderator",
            Membership.Role.MODERATOR,
            {"manage_months": True, "manage_participants": True, "remove_content": True},
        )
        self.reader = member(self.reader_user, "Reader")
        self.second_reader = member(self.second_reader_user, "Second Reader")
        self.leader = member(self.leader_user, "Leader")
        self.floater = member(self.floater_user, "Floater")
        self.host_candidate = member(self.host_candidate_user, "Host Candidate")
        self.draft = ChallengeMonth.objects.create(
            group=self.group,
            name="Override Draft",
            starts_on=date(2026, 11, 1),
            ends_on=date(2026, 11, 30),
            status=ChallengeMonth.Status.DRAFT,
            announcement_mode=ChallengeMonth.AnnouncementMode.CUSTOM,
            announcement="Old challenge announcement",
        )
        self.open_month = ChallengeMonth.objects.create(
            group=self.group,
            name="Override Open",
            starts_on=date(2026, 12, 1),
            ends_on=date(2026, 12, 31),
            status=ChallengeMonth.Status.ACTIVE,
        )
        self.team_one = Team.objects.create(month=self.draft, name="Team One", color="#112233")
        self.team_two = Team.objects.create(month=self.draft, name="Team Two", color="#445566")
        MonthEnrollment.objects.create(month=self.draft, participant=self.leader)
        TeamAssignment.objects.create(month=self.draft, participant=self.leader, team=self.team_one)
        self.hidden_theme = MonthTheme.objects.create(
            month=self.draft,
            name="Hidden Theme",
            starts_on=self.draft.starts_on,
            ends_on=self.draft.ends_on,
            is_active=False,
            is_visible=False,
        )
        self.removal_submission = BookSubmission.objects.create(
            month=self.draft,
            participant=self.leader,
            title="Removal Candidate",
            author="Author",
            book_format=BookSubmission.Format.EBOOK,
            completed_on=date(2026, 11, 10),
            submitted_pages=200,
        )
        self.review_theme = MonthTheme.objects.create(
            month=self.open_month,
            name="Review Theme",
            starts_on=self.open_month.starts_on,
            ends_on=self.open_month.ends_on,
            bonus_pages=25,
        )
        self.review_submission = BookSubmission.objects.create(
            month=self.open_month,
            participant=self.reader,
            title="Review Candidate",
            author="Author",
            book_format=BookSubmission.Format.EBOOK,
            completed_on=date(2026, 12, 10),
            submitted_pages=240,
        )
        self.review_claim = ThemeClaim.objects.create(
            submission=self.review_submission,
            theme=self.review_theme,
        )
        self.client.force_login(self.platform_owner)

    def assert_platform_owner_has_no_identity_records(self):
        self.assertFalse(Membership.objects.filter(user=self.platform_owner).exists())
        self.assertFalse(MonthEnrollment.objects.filter(participant__user=self.platform_owner).exists())
        self.assertFalse(TeamAssignment.objects.filter(participant__user=self.platform_owner).exists())
        self.assertFalse(ChallengeStaffAssignment.objects.filter(membership__user=self.platform_owner).exists())
        self.assertFalse(BookSubmission.objects.filter(participant__user=self.platform_owner).exists())

    def test_override_is_operational_authority_not_host_identity(self):
        self.assertTrue(can_operate_challenge(self.platform_owner, self.draft))
        self.assertFalse(is_challenge_host(self.platform_owner, self.draft))
        self.assertTrue(can_manage_challenge_hosts(self.platform_owner, self.group))
        self.assert_platform_owner_has_no_identity_records()

    def test_platform_owner_can_correct_challenge_configuration_and_lifecycle_with_audit(self):
        edit_url = reverse("month-edit", kwargs={"group_slug": self.group.slug, "pk": self.draft.pk})
        response = self.client.post(edit_url, {
            "name": "Corrected Override Draft",
            "description": "Administrative correction",
            "registration_opens_at": "2026-10-01T09:00",
            "registration_closes_at": "2026-10-20T17:00",
            "starts_at": "2026-11-01T08:00",
            "ends_at": "2026-11-30T20:00",
            "final_announcement_at": "2026-12-01T10:00",
            "auto_open_registration": "on",
            "auto_close_registration": "on",
            "auto_start_challenge": "on",
            "auto_end_challenge": "on",
            "announcement_mode": ChallengeMonth.AnnouncementMode.CUSTOM,
            "announcement": "Old challenge announcement",
        })
        self.assertRedirects(response, self.draft.get_absolute_url())
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.name, "Corrected Override Draft")
        transition_url = reverse("challenge-lifecycle-transition", kwargs={
            "group_slug": self.group.slug,
            "pk": self.draft.pk,
            "target_status": ChallengeMonth.Status.UPCOMING,
        })
        self.client.post(transition_url)
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.status, ChallengeMonth.Status.UPCOMING)
        self.assertTrue(AuditEvent.objects.filter(
            actor=self.platform_owner,
            action="challenge.lifecycle_changed",
            object_id=str(self.draft.pk),
        ).exists())
        self.assert_platform_owner_has_no_identity_records()

    def test_platform_owner_can_create_edit_archive_restore_and_delete_teams(self):
        create_url = reverse("team-create", kwargs={"group_slug": self.group.slug, "month_pk": self.draft.pk})
        self.client.post(create_url, {"name": "Temporary Team", "color": "#778899"})
        team = Team.objects.get(month=self.draft, name="Temporary Team")
        edit_url = reverse("team-edit", kwargs={"group_slug": self.group.slug, "month_pk": self.draft.pk, "pk": team.pk})
        self.client.post(edit_url, {"name": "Edited Team", "color": "#abcdef"})
        team.refresh_from_db()
        self.assertEqual((team.name, team.color), ("Edited Team", "#abcdef"))
        toggle_url = reverse("team-archive-toggle", kwargs={"group_slug": self.group.slug, "month_pk": self.draft.pk, "pk": team.pk})
        self.client.post(toggle_url)
        team.refresh_from_db()
        self.assertTrue(team.is_archived)
        self.client.post(toggle_url)
        team.refresh_from_db()
        self.assertFalse(team.is_archived)
        self.client.post(reverse("team-delete", kwargs={"group_slug": self.group.slug, "month_pk": self.draft.pk, "pk": team.pk}))
        self.assertFalse(Team.objects.filter(pk=team.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(actor=self.platform_owner, action="team.deleted").exists())
        self.assert_platform_owner_has_no_identity_records()

    def test_platform_owner_can_administer_enrollment_and_team_roster(self):
        add_url = reverse("month-participant-add", kwargs={"group_slug": self.group.slug, "month_pk": self.draft.pk})
        self.client.post(add_url, {"participant": self.reader.pk, "team": self.team_one.pk})
        enrollment = MonthEnrollment.objects.get(month=self.draft, participant=self.reader)
        assignment = TeamAssignment.objects.get(month=self.draft, participant=self.reader)
        edit_url = reverse("month-participant-edit", kwargs={"group_slug": self.group.slug, "month_pk": self.draft.pk, "pk": enrollment.pk})
        self.client.post(edit_url, {"team": self.team_two.pk})
        assignment.refresh_from_db()
        self.assertIsNotNone(assignment.ended_at)
        assignment = TeamAssignment.objects.get(month=self.draft, participant=self.reader, ended_at__isnull=True)
        self.assertEqual(assignment.team, self.team_two)
        self.client.post(edit_url, {"team": ""})
        assignment.refresh_from_db()
        self.assertIsNotNone(assignment.ended_at)
        self.client.post(edit_url, {"team": self.team_one.pk})
        assignment = TeamAssignment.objects.get(month=self.draft, participant=self.reader, ended_at__isnull=True)
        self.client.post(reverse("team-assignment-remove", kwargs={"group_slug": self.group.slug, "month_pk": self.draft.pk, "pk": assignment.pk}), {"reason": "Administrative change"})
        assignment.refresh_from_db()
        self.assertIsNotNone(assignment.ended_at)
        self.client.post(reverse("month-participant-remove", kwargs={"group_slug": self.group.slug, "month_pk": self.draft.pk, "pk": enrollment.pk}), {"reason": "Administrative removal"})
        enrollment.refresh_from_db()
        self.assertFalse(enrollment.is_active)
        self.assertTrue(AuditEvent.objects.filter(actor=self.platform_owner, action="participation.staff_removed").exists())
        self.assert_platform_owner_has_no_identity_records()

    def test_platform_owner_can_manage_themes_announcement_and_submission_removal(self):
        theme_page = self.client.get(reverse("theme-list", kwargs={"group_slug": self.group.slug, "month_pk": self.draft.pk}))
        self.assertContains(theme_page, self.hidden_theme.name)
        create_url = reverse("theme-create", kwargs={"group_slug": self.group.slug, "month_pk": self.draft.pk})
        self.client.post(create_url, {
            "name": "Created Theme", "description": "", "starts_on": "2026-11-01", "ends_on": "2026-11-30",
            "bonus_pages": 20, "prompt": "", "is_active": "on", "is_visible": "on",
        })
        theme = MonthTheme.objects.get(month=self.draft, name="Created Theme")
        edit_url = reverse("theme-edit", kwargs={"group_slug": self.group.slug, "month_pk": self.draft.pk, "pk": theme.pk})
        self.client.post(edit_url, {
            "name": "Edited Theme", "description": "Edited", "starts_on": "2026-11-01", "ends_on": "2026-11-30",
            "bonus_pages": 30, "prompt": "", "is_active": "on", "is_visible": "on",
        })
        theme.refresh_from_db()
        self.assertEqual(theme.name, "Edited Theme")
        self.client.post(reverse("month-announcement-update", kwargs={"group_slug": self.group.slug, "month_pk": self.draft.pk}), {"announcement": "Platform announcement"})
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.announcement, "Platform announcement")
        self.client.post(reverse("submission-remove", kwargs={"group_slug": self.group.slug, "month_pk": self.draft.pk, "pk": self.removal_submission.pk}), {"reason": "Administrative correction"})
        self.removal_submission.refresh_from_db()
        self.assertTrue(self.removal_submission.is_removed)
        self.assertEqual(self.removal_submission.removed_by, self.platform_owner)
        self.assertTrue(AuditEvent.objects.filter(actor=self.platform_owner, action="submission.removed", object_id=str(self.removal_submission.pk)).exists())
        self.assert_platform_owner_has_no_identity_records()

    def test_platform_owner_can_review_challenge_wide_and_receives_needs_attention(self):
        summary = needs_attention_summary(self.platform_owner)
        self.assertEqual(summary["total"], 2)
        self.assertEqual({item["month"] for item in summary["challenges"]}, {self.open_month})
        attention_page = self.client.get(reverse("needs-attention"))
        self.assertContains(attention_page, self.open_month.name)
        queue_url = reverse("review-queue", kwargs={"group_slug": self.group.slug, "month_pk": self.open_month.pk})
        queue = self.client.get(queue_url)
        self.assertContains(queue, self.review_submission.title)
        review_url = reverse("submission-review", kwargs={"group_slug": self.group.slug, "month_pk": self.open_month.pk, "pk": self.review_submission.pk})
        response = self.client.post(review_url, {
            "approved_pages": 230,
            "status": BookSubmission.Status.APPROVED,
            "verification_url": "",
            "review_notes": "Platform review",
            "claims-TOTAL_FORMS": "1",
            "claims-INITIAL_FORMS": "1",
            "claims-MIN_NUM_FORMS": "0",
            "claims-MAX_NUM_FORMS": "1000",
            "claims-0-id": str(self.review_claim.pk),
            "claims-0-submission": str(self.review_submission.pk),
            "claims-0-status": ThemeClaim.Status.APPROVED,
        })
        self.assertRedirects(response, queue_url)
        self.review_submission.refresh_from_db()
        self.review_claim.refresh_from_db()
        self.assertEqual(self.review_submission.reviewed_by, self.platform_owner)
        self.assertEqual(self.review_claim.reviewed_by, self.platform_owner)
        self.assertTrue(AuditEvent.objects.filter(actor=self.platform_owner, action="submission.approved").exists())
        self.assert_platform_owner_has_no_identity_records()

    def test_platform_owner_can_manage_hosts_team_leaders_and_floaters(self):
        host_url = reverse("challenge-host-list", kwargs={"group_slug": self.group.slug, "month_pk": self.draft.pk})
        self.client.post(host_url, {"membership": self.host_candidate.pk})
        host_assignment = ChallengeStaffAssignment.objects.get(month=self.draft, membership=self.host_candidate, role=ChallengeStaffAssignment.Role.HOST)
        self.assertEqual(host_assignment.assigned_by, self.platform_owner)
        leader_url = reverse("team-leader-list", kwargs={"group_slug": self.group.slug, "month_pk": self.draft.pk, "team_pk": self.team_one.pk})
        self.client.post(leader_url, {"membership": self.leader.pk})
        leader_assignment = ChallengeStaffAssignment.objects.get(month=self.draft, membership=self.leader, role=ChallengeStaffAssignment.Role.TEAM_LEADER)
        self.assertEqual(leader_assignment.assigned_by, self.platform_owner)
        floater_url = reverse("challenge-floater-list", kwargs={"group_slug": self.group.slug, "month_pk": self.draft.pk})
        self.client.post(floater_url, {"membership": self.floater.pk})
        floater_assignment = ChallengeStaffAssignment.objects.get(month=self.draft, membership=self.floater, role=ChallengeStaffAssignment.Role.FLOATER)
        self.assertEqual(floater_assignment.assigned_by, self.platform_owner)
        self.client.post(reverse("team-leader-end", kwargs={"group_slug": self.group.slug, "month_pk": self.draft.pk, "team_pk": self.team_one.pk, "pk": leader_assignment.pk}))
        self.client.post(reverse("challenge-floater-end", kwargs={"group_slug": self.group.slug, "month_pk": self.draft.pk, "pk": floater_assignment.pk}))
        self.client.post(reverse("challenge-host-end", kwargs={"group_slug": self.group.slug, "month_pk": self.draft.pk, "pk": host_assignment.pk}))
        for assignment in (host_assignment, leader_assignment, floater_assignment):
            assignment.refresh_from_db()
            self.assertIsNotNone(assignment.ended_at)
            self.assertEqual(assignment.ended_by, self.platform_owner)
        self.assert_platform_owner_has_no_identity_records()

    def test_normal_unstaffed_group_authority_remains_denied(self):
        operation_urls = (
            reverse("team-create", kwargs={"group_slug": self.group.slug, "month_pk": self.draft.pk}),
            reverse("theme-create", kwargs={"group_slug": self.group.slug, "month_pk": self.draft.pk}),
            reverse("month-participant-add", kwargs={"group_slug": self.group.slug, "month_pk": self.draft.pk}),
            reverse("submission-remove", kwargs={"group_slug": self.group.slug, "month_pk": self.draft.pk, "pk": self.removal_submission.pk}),
            reverse("challenge-floater-list", kwargs={"group_slug": self.group.slug, "month_pk": self.draft.pk}),
        )
        for user in (self.owner_user, self.moderator_user):
            self.client.force_login(user)
            for url in operation_urls:
                with self.subTest(user=user.username, url=url):
                    self.assertEqual(self.client.post(url, {}).status_code, 403)
