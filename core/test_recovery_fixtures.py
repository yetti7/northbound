"""Test-only protected graph; independent of the excluded development demo seeder."""
from datetime import date
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.utils import timezone

from .botm_matching import synchronize_submission as match_botm
from .game_rewards import apply_game_reward
from .models import (
    BookSubmission, BotmBook, CatalogBook, ChallengeMonth, ChallengeSignupAnswer,
    ChallengeSignupQuestion, ChallengeStaffAssignment, Game, Membership, MonthEnrollment,
    MonthTheme, PersonalTBR, PersonalTBRBook, ProgressCheckpoint, ProgressCheckpointResult,
    ReadingGroup, Team, TeamAssignment, ThemeClaim,
)
from .personal_tbr_matching import synchronize_submission as match_tbr
from .scoring import apply_submission_review


def create_protected_recovery_graph(owner):
    group = ReadingGroup.objects.create(name="Recovery fixture", slug="recovery-fixture")
    user = get_user_model().objects.create_user("purge-reader")
    member = Membership.objects.create(group=group, user=user, display_name="Reader")
    month = ChallengeMonth.objects.create(
        group=group, name="Stories Under the Stars", status="active",
        starts_on=date(2026, 8, 1), ends_on=date(2026, 8, 31),
        botm_enabled=True, botm_completion_bonus_pages=100,
        tbr_enabled=True, tbr_book_bonus_pages=10, tbr_completion_bonus_pages=100,
        games_enabled=True,
    )
    ChallengeMonth.objects.create(group=group, name="Completed fixture", status="completed",
        starts_on=date(2026, 7, 1), ends_on=date(2026, 7, 31))
    question = ChallengeSignupQuestion.objects.create(month=month, wording="Plan?",
        question_type="short_text", position=1)
    enrollment = MonthEnrollment.objects.create(month=month, participant=member)
    ChallengeSignupAnswer.objects.create(enrollment=enrollment, question=question, value="Read")
    team = Team.objects.create(month=month, name="Fixture Team")
    TeamAssignment.objects.create(month=month, participant=member, team=team)
    ChallengeStaffAssignment.objects.create(month=month, membership=member, role="host")
    tbr = PersonalTBR.objects.create(enrollment=enrollment)
    books = []
    for position in range(1, 10):
        book = CatalogBook.objects.create(provider="hardcover", provider_book_id=f"fixture-{position}",
            title=f"Fixture Book {position}", author="Fixture Author")
        books.append(book)
        PersonalTBRBook.objects.create(personal_tbr=tbr, position=position, catalog_book=book,
            title_snapshot=book.title, author_snapshot=book.author, page_count_snapshot=100)
    tbr.confirmed_at = timezone.now()
    tbr.save()
    BotmBook.objects.create(month=month, position=1, catalog_book=books[0],
        title_snapshot=books[0].title, author_snapshot=books[0].author,
        page_count_snapshot=100, bonus_pages=10)
    theme = MonthTheme.objects.create(month=month, name="Fixture Theme", bonus_percentage=25,
        starts_on=month.starts_on, ends_on=month.ends_on)
    for book in books:
        submission = BookSubmission.objects.create(month=month, participant=member, catalog_book=book,
            title=book.title, author=book.author, book_format="ebook",
            completed_on=date(2026, 8, 10), submitted_pages=100)
        claim = ThemeClaim.objects.create(submission=submission, theme=theme, status="approved")
        submission.status = "approved"
        submission.approved_pages = 100
        apply_submission_review(submission, [claim], owner, timezone.now())
        match_botm(submission)
        match_tbr(submission)
    now = timezone.now()
    game = Game.objects.create(month=month, name="Fixture Game", starts_at=now,
        ends_at=now + timezone.timedelta(days=1), advertised_bonus_pages=50)
    apply_game_reward(game=game, actor=owner, target_type="reader", target_participant=member,
        amount=20, reason="Fixture reward", idempotency_key=uuid4())
    checkpoint = ProgressCheckpoint.objects.create(month=month, scheduled_at=now,
        target_basis="fixed", fixed_target_pages=100)
    ProgressCheckpointResult.objects.create(checkpoint=checkpoint, participant=member, evaluated_at=now,
        threshold_percentage=25, progress_basis="total", target_basis="fixed",
        target_pages=100, required_pages=25, progress_pages=100, outcome="met")
