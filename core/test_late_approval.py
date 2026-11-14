from datetime import timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .immersive import review_immersive
from .models import (
    AuditEvent, BookSubmission, BotmBook, BotmCompletionAward, BotmMatch, ChallengeMonth,
    ChallengeStaffAssignment, Membership, ModifierProvenance, MonthEnrollment, MonthTheme,
    PersonalTBR, PersonalTBRBook, PersonalTBRCompletionAward, PersonalTBRMatch, ReadingGroup, Team,
)
from .participation import activate_participation, assign_participant_to_team, deactivate_participation
from .score_aggregation import challenge_score_totals, challenge_team_totals
from .submission_review_matching import apply_unified_review, review_token


class LateApprovalTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_superuser("late-owner")
        self.user = get_user_model().objects.create_user("late-reader")
        self.group = ReadingGroup.objects.create(name="Group", slug="late", timezone="America/New_York")
        self.reader = Membership.objects.create(group=self.group, user=self.user, display_name="Reader")
        self.today = timezone.localdate(timezone=ZoneInfo(self.group.timezone))
        self.month = ChallengeMonth.objects.create(
            group=self.group, name="Challenge", status="active",
            starts_on=self.today - timedelta(days=5), ends_on=self.today + timedelta(days=5),
        )
        self.team = Team.objects.create(month=self.month, name="Team A")
        self.other_team = Team.objects.create(month=self.month, name="Team B")
        self.enrollment = self.activate()
        self.assign(self.team)
        self.client.force_login(self.user)

    def activate(self):
        return activate_participation(
            month=self.month, participant=self.reader, actor=self.user, origin=MonthEnrollment.Origin.SELF,
        )[0]

    def assign(self, team):
        return assign_participant_to_team(month=self.month, participant=self.reader, team=team, actor=self.owner)[0]

    def depart(self, *, removed=False):
        self.enrollment = deactivate_participation(
            enrollment=self.enrollment, actor=self.owner if removed else self.user,
            reason="removed" if removed else "withdrawn", ordinary_staff_removal=removed,
        )[0]

    def submit(self, **fields):
        response = self.client.post(reverse("submission-create", args=[self.group.slug, self.month.pk]), {
            "title": "Book", "author": "Author", "book_format": "ebook",
            "completed_on": self.today.isoformat(), "submitted_pages": 400, **fields,
        })
        self.assertEqual(response.status_code, 302)
        return BookSubmission.objects.latest("pk")

    def approve(self, book, *, actor=None, token=None, themes=False):
        actor = actor or self.owner
        book.refresh_from_db()
        token = token or review_token(book, actor)
        claims = list(book.theme_claims.all()) if themes else []
        for claim in claims:
            claim.status = "approved"
        book.status = "approved"
        book.approved_pages = 400
        return apply_unified_review(book, claims, actor, timezone.now(), version=token, confirmations=[])

    def test_withdrawal_credit_and_repeat_preserve_participation(self):
        book = self.submit()
        self.depart()
        old_periods = list(self.enrollment.participation_periods.values())
        approved = self.approve(book)
        self.assertEqual(approved.final_scored_pages, 400)
        self.assertEqual(challenge_score_totals(month=self.month)[self.reader.pk]["total_pages"], 400)
        self.assertEqual(challenge_team_totals(month=self.month, team_ids=[self.team.pk]), {self.team.pk: 400})
        self.enrollment.refresh_from_db()
        self.assertFalse(self.enrollment.is_active)
        self.assertEqual(self.enrollment.credited_team_id, self.team.pk)
        self.assertEqual(list(self.enrollment.participation_periods.values()), old_periods)
        with self.assertRaisesMessage(ValidationError, "Only a pending submission"):
            self.approve(book)
        self.assertEqual(AuditEvent.objects.filter(action="submission.late_approved", object_id=str(book.pk)).count(), 1)

    def test_staff_removal_after_transfer_uses_credited_team(self):
        book = self.submit()
        self.assign(self.other_team)
        self.depart(removed=True)
        self.approve(book)
        self.assertEqual(challenge_team_totals(month=self.month, team_ids=[self.team.pk, self.other_team.pk]),
                         {self.team.pk: 0, self.other_team.pk: 400})

    def test_reactivation_requires_assignment_and_uses_ordinary_review_when_eligible(self):
        book = self.submit()
        self.depart()
        self.enrollment = self.activate()
        with self.assertRaisesMessage(ValidationError, "scoring team"):
            self.approve(book)
        self.assign(self.other_team)
        self.approve(book)
        self.assertFalse(AuditEvent.objects.filter(action="submission.late_approved").exists())
        self.assertEqual(challenge_team_totals(month=self.month, team_ids=[self.other_team.pk]), {self.other_team.pk: 400})

    def test_later_departure_cannot_supply_a_team_for_a_previous_period(self):
        book = self.submit()
        self.depart()
        self.enrollment = self.activate()
        self.assign(self.other_team)
        self.depart()
        with self.assertRaisesMessage(ValidationError, "cannot be verified"):
            self.approve(book)

    def test_stale_signed_review_from_before_departure_is_rejected(self):
        book = self.submit()
        token = review_token(book, self.owner)
        self.depart()
        with self.assertRaisesMessage(ValidationError, "Reload"):
            self.approve(book, token=token)
        self.assertEqual(self.approve(book).status, "approved")

    def test_missing_team_and_ineligible_evidence_fail_closed(self):
        book = self.submit()
        self.depart()
        self.enrollment.credited_team = None
        self.enrollment.save(update_fields=["credited_team"])
        with self.assertRaisesMessage(ValidationError, "cannot be verified"):
            self.approve(book)
        self.enrollment.credited_team = self.team
        self.enrollment.save(update_fields=["credited_team"])
        # Model-level fixture simulates a legacy import performed after departure.
        legacy = BookSubmission.objects.create(month=self.month, participant=self.reader, title="Legacy",
            author="Author", book_format="ebook", completed_on=self.today, submitted_pages=400)
        with self.assertRaisesMessage(ValidationError, "cannot be verified"):
            self.approve(legacy)

    def test_backdated_completion_and_post_departure_edits_do_not_supply_evidence(self):
        prior_day = self.today - timedelta(days=1)
        backdated = self.submit(completed_on=prior_day.isoformat())
        original = self.submit(title="Original")
        self.depart()
        for book, field, value in ((backdated, "completed_on", self.today), (original, "title", "Edited")):
            setattr(book, field, value)
            book.save(update_fields=[field])
            with self.assertRaisesMessage(ValidationError, "cannot be verified"):
                self.approve(book)

    def test_team_scoped_reviewer_and_finalized_challenge_remain_restricted(self):
        leader = get_user_model().objects.create_user("late-leader")
        member = Membership.objects.create(group=self.group, user=leader, display_name="Leader")
        activate_participation(month=self.month, participant=member, actor=leader, origin="self")
        assign_participant_to_team(month=self.month, participant=member, team=self.team, actor=self.owner)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=member, team=self.team, role="team_leader")
        book = self.submit()
        self.depart()
        with self.assertRaises(PermissionDenied):
            self.approve(book, actor=leader)
        for state in ("completed", "archived"):
            ChallengeMonth.objects.filter(pk=self.month.pk).update(status=state)
            with self.assertRaises(PermissionDenied):
                self.approve(book)

    def configure_awards(self):
        ChallengeMonth.objects.filter(pk=self.month.pk).update(botm_enabled=True, botm_completion_bonus_pages=35,
            tbr_enabled=True, tbr_book_bonus_pages=15, tbr_completion_bonus_pages=90)
        botm = BotmBook.objects.create(month=self.month, position=1, title_snapshot="Book", author_snapshot="Author",
                                      page_count_snapshot=400, bonus_pages=25)
        tbr = PersonalTBR.objects.create(enrollment=self.enrollment)
        for position in range(1, 10):
            PersonalTBRBook.objects.create(personal_tbr=tbr, position=position,
                title_snapshot="Book" if position == 9 else f"Earlier {position}", author_snapshot="Author")
        tbr.confirmed_at = timezone.now()
        tbr.save(update_fields=["confirmed_at"])
        return botm, tbr

    def test_themes_book_rewards_completion_and_historical_rates(self):
        botm, tbr = self.configure_awards()
        from .personal_tbr_matching import synchronize_submission
        for position in range(1, 9):
            earlier = BookSubmission.objects.create(month=self.month, participant=self.reader,
                title=f"Earlier {position}", author="Author", book_format="ebook",
                completed_on=self.today, submitted_pages=10, approved_pages=10, status="approved")
            synchronize_submission(earlier)
        theme = MonthTheme.objects.create(month=self.month, name="Theme", bonus_percentage=10,
                                          starts_on=self.today, ends_on=self.today)
        book = self.submit(themes=[theme.pk])
        self.depart()
        old_scores = dict(BookSubmission.objects.exclude(pk=book.pk).values_list("pk", "final_scored_pages"))
        ChallengeMonth.objects.filter(pk=self.month.pk).update(
            botm_completion_bonus_pages=200, tbr_book_bonus_pages=300, tbr_completion_bonus_pages=500,
        )
        BotmBook.objects.filter(pk=botm.pk).update(bonus_pages=999)
        approved = self.approve(book, themes=True)
        self.assertEqual(approved.final_scored_pages, 480)
        self.assertTrue(BotmMatch.objects.get(submission=book).is_qualifying)
        self.assertTrue(PersonalTBRMatch.objects.get(submission=book).is_qualifying)
        self.assertEqual(BotmCompletionAward.objects.get().bonus_amount_snapshot, 35)
        self.assertEqual(PersonalTBRCompletionAward.objects.get(personal_tbr=tbr).bonus_amount_snapshot, 90)
        self.assertEqual(dict(BookSubmission.objects.exclude(pk=book.pk).values_list("pk", "final_scored_pages")), old_scores)
        self.assertEqual(dict(ModifierProvenance.objects.filter(submission=book).values_list("source_type", "amount")),
                         {"theme_bonus": 40, "tbr_book": 15, "botm_book": 25})

    def test_late_immersive_requires_separate_review_and_uses_captured_rate(self):
        ChallengeMonth.objects.filter(pk=self.month.pk).update(immersive_enabled=True, immersive_bonus_percent=50)
        book = self.submit(immersive_request="on")
        immersive = book.immersive_requests.get()
        self.depart()
        ChallengeMonth.objects.filter(pk=self.month.pk).update(immersive_bonus_percent=100)
        self.assertEqual(self.approve(book).final_scored_pages, 400)
        immersive.refresh_from_db()
        self.assertEqual(immersive.status, "pending")
        award = review_immersive(request=immersive, actor=self.owner, decision="approved")
        self.assertEqual(award.awarded_pages, 200)
        book.refresh_from_db()
        self.assertEqual(book.final_scored_pages, 600)

    def test_earlier_local_day_holds_unproved_bonus_through_reactivation(self):
        botm, _ = self.configure_awards()
        self.enrollment.participation_periods.update(started_at=timezone.now() - timedelta(days=2))
        yesterday = self.today - timedelta(days=1)
        theme = MonthTheme.objects.create(month=self.month, name="Old Theme", bonus_percentage=10,
                                          starts_on=yesterday, ends_on=self.today)
        book = self.submit(themes=[theme.pk], completed_on=yesterday.isoformat())
        self.depart()
        approved = self.approve(book, themes=True)
        self.assertEqual(approved.final_scored_pages, 400)
        self.assertTrue(BotmMatch.objects.get(submission=book, botm_book=botm).is_qualifying)
        self.assertTrue(PersonalTBRMatch.objects.get(submission=book).is_qualifying)
        self.assertIn("Theme Old Theme needs historical configuration review.",
                      approved.late_approval_evidence["held_for_review"])
        self.assertFalse(BotmCompletionAward.objects.exists())
        self.assertFalse(ModifierProvenance.objects.filter(submission=book).exists())
        self.enrollment = self.activate()
        self.assign(self.other_team)
        from .botm_matching import synchronize_submission
        synchronize_submission(book)
        self.assertFalse(BotmCompletionAward.objects.exists())
        self.assertFalse(ModifierProvenance.objects.filter(submission=book).exists())
        with self.assertRaisesMessage(ValidationError, "held historical Theme"):
            self.approve(book, themes=True)

    def test_rate_change_invalidates_review_token_for_departed_reader(self):
        self.configure_awards()
        book = self.submit()
        self.depart()
        token = review_token(book, self.owner)
        ChallengeMonth.objects.filter(pk=self.month.pk).update(tbr_book_bonus_pages=111)
        with self.assertRaisesMessage(ValidationError, "Reload"):
            self.approve(book, token=token)

    def test_submitting_after_departure_is_rejected_by_normal_entry_point(self):
        self.depart()
        response = self.client.post(reverse("submission-create", args=[self.group.slug, self.month.pk]), {
            "title": "After departure", "author": "Author", "book_format": "ebook",
            "completed_on": self.today.isoformat(), "submitted_pages": 400,
        })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(BookSubmission.objects.exists())

    def test_challenge_wide_host_can_approve_through_signed_review_page(self):
        host_user = get_user_model().objects.create_user("late-host")
        host = Membership.objects.create(group=self.group, user=host_user, display_name="Host")
        ChallengeStaffAssignment.objects.create(month=self.month, membership=host, role="host")
        book = self.submit()
        self.depart(removed=True)
        self.client.force_login(host_user)
        url = reverse("submission-review", args=[self.group.slug, self.month.pk, book.pk])
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        result = self.client.post(url, {"status": "approved", "approved_pages": 400,
                                       "matching_version": page.context["matching_form"]["matching_version"].value()})
        self.assertEqual(result.status_code, 302)
        book.refresh_from_db()
        self.assertEqual(book.reviewed_by_id, host_user.pk)
        self.assertEqual(book.final_scored_pages, 400)

    def test_completion_on_a_day_shared_by_two_periods_is_ambiguous(self):
        self.depart()
        self.enrollment = self.activate()
        self.assign(self.other_team)
        book = self.submit()
        self.depart()
        with self.assertRaisesMessage(ValidationError, "cannot be verified"):
            self.approve(book)

    def test_changed_theme_window_does_not_block_base_approval_from_review_page(self):
        theme = MonthTheme.objects.create(month=self.month, name="Moved Theme", bonus_percentage=10,
                                          starts_on=self.today, ends_on=self.today)
        book = self.submit(themes=[theme.pk])
        self.depart()
        yesterday = self.today - timedelta(days=1)
        MonthTheme.objects.filter(pk=theme.pk).update(starts_on=yesterday, ends_on=yesterday)
        self.client.force_login(self.owner)
        url = reverse("submission-review", args=[self.group.slug, self.month.pk, book.pk])
        page = self.client.get(url)
        self.assertEqual(page.context["claim_formset"].initial_form_count(), 0)
        result = self.client.post(url, {"status": "approved", "approved_pages": 400,
                                       "matching_version": page.context["matching_form"]["matching_version"].value(),
                                       "claims-TOTAL_FORMS": 0, "claims-INITIAL_FORMS": 0})
        self.assertEqual(result.status_code, 302)
        book.refresh_from_db()
        self.assertEqual(book.final_scored_pages, 400)
        self.assertEqual(book.theme_claims.get().status, "pending")
        self.assertIn("Theme Moved Theme needs historical configuration review.", book.late_approval_evidence["held_for_review"])
