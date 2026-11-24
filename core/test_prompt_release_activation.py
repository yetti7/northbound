"""Released defaults against an isolated copy of December's prompt structure."""
from datetime import date
from django.conf import settings as django_settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from . import test_prompt_foundations as fixtures
from .models import (BookSubmission, ChallengeMonth, PromptClaim, PromptCompletion,
                     ModifierProvenance, TeamAssignment)
from .prompt_reservations import reservations_ready, category_available, replace_selections
from .prompt_scoring import review_prompts
from .photo_submissions import create_photo_submission
from .photo_review import review_photo_submission, photo_review_version
from .submission_workspace import qualification_overview


class ReleasedPromptTests(TestCase):
    token = fixtures.PromptFoundationTests.token
    prompt = fixtures.PromptFoundationTests.prompt
    settings = fixtures.PromptFoundationTests.settings

    def setUp(self):
        fixtures.PromptFoundationTests.setUp(self)
        ChallengeMonth.objects.filter(pk=self.month.pk).update(
            name='December reference', starts_on=date(2026,12,1), ends_on=date(2026,12,31),
            immersive_enabled=True, immersive_bonus_percent=100)
        self.month.refresh_from_db()
        self.reader=self.members['reader']
        self.bingo=[self.prompt(number=n,description='Free Space' if n==7 else 'Bingo '+str(n),points=0) for n in range(1,10)]
        photos={6,9,12,16,19,24,25}
        self.scavenger=[self.prompt(number=n,kind='scavenger',points=50,
            submission_type='photo' if n in photos else 'book') for n in range(1,26)]
        self.settings(enabled=True,completion_bonus=1000)
        self.settings(kind='scavenger',enabled=True,completion_bonus=1000)

    def make_book(self,approved=True):
        return BookSubmission.objects.create(month=self.month,participant=self.reader,
            title='December book',author='Author',book_format='ebook',completed_on=date(2026,12,3),
            submitted_pages=100,approved_pages=100 if approved else None,
            status='approved' if approved else 'pending')

    def test_shipped_default_exposes_reader_selectors_without_override(self):
        self.assertTrue(django_settings.NORTHBOUND_PROMPT_RESERVATIONS_READY)
        self.assertTrue(reservations_ready())
        self.client.force_login(self.reader.user)
        response=self.client.get(reverse('submission-create',args=[self.group.slug,self.month.pk]))
        self.assertContains(response,'name="bingo_prompt"')
        self.assertContains(response,'name="scavenger_prompt"')
        response=self.client.post(reverse('submission-create',args=[self.group.slug,self.month.pk]),{
            'title':'Reader selected','author':'Author','book_format':'ebook','completed_on':'2026-12-03',
            'submitted_pages':100,'bingo_prompt':self.bingo[0].pk,'scavenger_prompt':self.scavenger[0].pk})
        self.assertEqual(response.status_code,302)
        self.assertEqual(PromptClaim.objects.filter(state='reserved').count(),2)
        self.assertFalse(ModifierProvenance.objects.exists())

    def test_bingo_full_card_requires_free_space_qualification_and_preserves_base_pages(self):
        for prompt in self.bingo:
            if prompt.number==7:continue
            book=self.make_book();replace_selections(submission=book,actor=self.actor,selections={'bingo':prompt.pk})
            review_prompts(book,self.actor,{'bingo':'qualified'})
            book.refresh_from_db();self.assertEqual(book.approved_pages,100)
        self.assertFalse(PromptCompletion.objects.exists())
        book=self.make_book();replace_selections(submission=book,actor=self.actor,selections={'bingo':self.bingo[6].pk})
        self.assertFalse(PromptCompletion.objects.exists())
        review_prompts(book,self.actor,{'bingo':'qualified'})
        self.assertEqual(PromptCompletion.objects.get().bonus_amount_snapshot,1000)
        self.assertEqual(list(ModifierProvenance.objects.values_list('source_type','amount')),[('bingo_completion',1000)])

    def test_mixed_scavenger_completion_requires_all_books_and_verified_photos(self):
        for prompt in self.scavenger:
            if prompt.submission_type=='book':
                book=self.make_book();replace_selections(submission=book,actor=self.actor,selections={'scavenger':prompt.pk})
                review_prompts(book,self.actor,{'scavenger':'qualified'})
            else:
                photo=create_photo_submission(month_id=self.month.pk,actor=self.reader.user,
                    prompt_id=prompt.pk,reader_confirmed=True,discord_message_url='')
                if prompt==self.scavenger[-1]:self.assertFalse(PromptCompletion.objects.exists())
                review_photo_submission(photo_id=photo.pk,actor=self.actor,decision='approved',
                    reviewer_verified=True,version=photo_review_version(photo,self.actor))
        rows=ModifierProvenance.objects.filter(source_type='scavenger_prompt')
        self.assertEqual(rows.count(),25);self.assertEqual(set(rows.values_list('amount',flat=True)),{50})
        self.assertEqual(PromptCompletion.objects.get().bonus_amount_snapshot,1000)

    def test_lifecycle_category_and_missing_team_boundaries_under_released_default(self):
        for stage in ['upcoming','active','finalizing','completed']:
            ChallengeMonth.objects.filter(pk=self.month.pk).update(status=stage);self.month.refresh_from_db()
            self.assertEqual(category_available(self.month,'bingo'),stage in {'active','finalizing'})
            if stage!='active':
                with self.assertRaises((ValidationError,PermissionDenied)):
                    create_photo_submission(month_id=self.month.pk,actor=self.reader.user,
                        prompt_id=self.scavenger[5].pk,reader_confirmed=True)
        ChallengeMonth.objects.filter(pk=self.month.pk).update(status='active');self.month.refresh_from_db()
        TeamAssignment.objects.filter(month=self.month,participant=self.reader).delete()
        photo=create_photo_submission(month_id=self.month.pk,actor=self.reader.user,
            prompt_id=self.scavenger[5].pk,reader_confirmed=True)
        with self.assertRaises(ValidationError):
            review_photo_submission(photo_id=photo.pk,actor=self.actor,decision='approved',
                reviewer_verified=True,version=photo_review_version(photo,self.actor))
        self.assertFalse(ModifierProvenance.objects.exists())
        self.settings(enabled=False);self.assertFalse(category_available(self.month,'bingo'))
        self.assertTrue(category_available(self.month,'scavenger'))

    def test_management_shows_enabled_empty_categories_with_lifecycle_and_permissions(self):
        book=self.make_book(False);self.client.force_login(self.actor)
        url=reverse('submission-editor',args=[self.group.slug,self.month.pk,book.pk])
        response=self.client.get(url)
        for label in ['Bingo','Scavenger Hunt','Immersive Reading']:
            self.assertContains(response,'<h3>'+label+'</h3>',html=True)
        self.assertContains(response,'name="bingo_prompt"');self.assertContains(response,'separate Photo workflow')
        self.assertNotContains(response,'Review Bingo qualification')
        ChallengeMonth.objects.filter(pk=self.month.pk).update(status='upcoming');self.month.refresh_from_db();book.month=self.month
        overview=qualification_overview(self.actor,book)
        self.assertEqual(len(overview['prompts']),2)
        self.assertTrue(all(p['availability']=='Available when the Challenge is Active.' for p in overview['prompts']))
        self.assertTrue(all(not p['operational'] for p in overview['prompts']))
        self.client.force_login(self.users['moderator']);self.assertEqual(self.client.get(url).status_code,403)

    def test_november_exclusion_survives_released_default(self):
        self.group.slug='guildwars';self.group.save(update_fields=['slug'])
        ChallengeMonth.objects.filter(pk=self.month.pk).update(starts_on=date(2026,11,1),ends_on=date(2026,11,30))
        self.month.refresh_from_db();self.assertFalse(category_available(self.month,'bingo'))
        self.assertFalse(category_available(self.month,'scavenger'))
