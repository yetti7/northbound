from datetime import date
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from . import test_submission_workspace as fixture
from .models import (BookSubmission, MonthTheme, ThemeClaim, ImmersiveRequest, PromptCategory,
                     ChallengePrompt, PromptClaim, TeamAssignment, ModifierProvenance)


class BookManagementOverviewTests(TransactionTestCase):
    setUp=fixture.SubmissionWorkspaceTests.setUp
    actor=fixture.SubmissionWorkspaceTests.actor
    submission=fixture.SubmissionWorkspaceTests.submission

    def edit(self, book):
        return self.client.get(reverse('submission-editor',args=[self.month.group.slug,self.month.pk,book.pk]))

    def test_existing_actions_and_pages_are_preserved_for_each_staff_role(self):
        book=self.submission()
        book.reference_url='https://books.example.com/reference';book.save(update_fields=['reference_url'])
        before=list(BookSubmission.objects.values()),list(ModifierProvenance.objects.values())
        for role in ('host','floater','team_leader'):
            with self.subTest(role=role):
                self.client.force_login(self.actor(role))
                response=self.edit(book)
                self.assertEqual(response.status_code,200)
                self.assertContains(response,'aria-label="Challenge Qualifications"')
                self.assertContains(response,'Personal TBR')
                self.assertContains(response,'Open Review and Scoring')
                self.assertContains(response,'Exceptional credit corrections')
                self.assertContains(response,'href="https://books.example.com/reference"')
                self.assertNotContains(response,'name="reference_url"')
                self.assertEqual(response.context['workspace']['edit_post_url'],reverse('submission-editor',args=['hotfix',self.month.pk,book.pk]))
        self.assertEqual((list(BookSubmission.objects.values()),list(ModifierProvenance.objects.values())),before)
        TeamAssignment.objects.filter(participant=self.reader,month=self.month).update(ended_at=timezone.now())
        self.assertEqual(self.edit(book).status_code,403)

    def test_pending_books_omit_exceptional_credit_and_disabled_features(self):
        book=self.submission(status='pending');self.client.force_login(self.actor())
        response=self.edit(book)
        self.assertContains(response,'Save Pending Changes')
        self.assertNotContains(response,'Exceptional credit corrections')
        self.assertNotContains(response,'<h3>Bingo</h3>',html=True)
        self.assertNotContains(response,'<h3>Scavenger Hunt</h3>',html=True)
        self.assertNotContains(response,'<h3>Immersive Reading</h3>',html=True)
        self.assertNotContains(response,'<h3>Book of the Month</h3>',html=True)

    def test_theme_and_immersive_relationships_use_existing_details(self):
        book=self.submission();self.client.force_login(self.actor())
        theme=MonthTheme.objects.create(month=self.month,name='Existing Theme',starts_on=self.month.starts_on,ends_on=self.month.ends_on)
        ThemeClaim.objects.create(submission=book,theme=theme)
        ImmersiveRequest.objects.create(submission=book,requester=self.reader.user,captured_percent=100)
        response=self.edit(book)
        self.assertContains(response,'Existing Theme')
        self.assertContains(response,'View existing Immersive Reading actions')
        self.assertContains(response,'section=qualifications#immersive')
        self.assertNotContains(response,'name="approved_pages"')

    def test_prompt_history_is_visible_without_activating_automation(self):
        self.client.force_login(self.actor())
        book=BookSubmission.objects.create(month=self.month,participant=self.reader,title='Prompt-only book',author='Writer',
            book_format='ebook',completed_on=date(2026,11,3),submitted_pages=100)
        for kind in ('bingo','scavenger'):
            category=PromptCategory.objects.create(month=self.month,kind=kind,enabled=True)
            prompt=ChallengePrompt.objects.create(category=category,number=1,description='Existing prompt')
            with override_settings(NORTHBOUND_PROMPT_RESERVATIONS_READY=True):
                PromptClaim.objects.create(category=category,prompt=prompt,submission=book,participant=self.reader)
        with override_settings(NORTHBOUND_PROMPT_RESERVATIONS_READY=False):
            response=self.edit(book)
            self.assertContains(response,'Recorded history only',count=2)
            self.assertNotContains(response,'name="bingo_prompt"')
            self.assertNotContains(response,'name="scavenger_prompt"')
        with override_settings(NORTHBOUND_PROMPT_RESERVATIONS_READY=True):
            response=self.edit(book)
            self.assertContains(response,'name="bingo_prompt"')
            self.assertContains(response,'name="scavenger_prompt"')
            self.assertNotContains(response,'Recorded history only')

    def test_lifecycle_restrictions_remain_enforced(self):
        book=self.submission();self.client.force_login(self.actor())
        type(self.month).objects.filter(pk=self.month.pk).update(status='completed')
        self.assertEqual(self.edit(book).status_code,403)
