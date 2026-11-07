"""Work equivalence without edition constraints or changes to page calculation."""
from django.test import TestCase
from django.core.exceptions import ValidationError
from . import test_matching_phase2a as foundation
from .models import CatalogEdition, CatalogBook, BotmMatch, PersonalTBRMatch, ModifierProvenance, HardcoverSyncOutbox
from .matching_resolution import identity_evidence, identity_stamp
from .matching_intent import capture_intent, selection_token
from .botm_matching import synchronize_submission as sync_botm
from .personal_tbr_matching import synchronize_submission as sync_tbr


class WorkEquivalenceTests(TestCase):
    setUp = foundation.MatchingPhase2ATests.setUp
    actor = foundation.MatchingPhase2ATests.actor
    submission = foundation.MatchingPhase2ATests.submission
    botm = foundation.MatchingPhase2ATests.botm
    confirm = foundation.MatchingPhase2ATests.confirm

    def edition(self, label, **kwargs):
        return CatalogEdition.objects.create(book=self.book, provider='hardcover',
            provider_edition_id=label, format_name=label, **kwargs)

    def target(self):
        return self.botm(title=self.book.title,author=self.book.author,
            catalog_book=self.book,catalog_edition=self.edition)

    def test_formats_and_different_editions_qualify_in_both_domains(self):
        target=self.target()
        for label, book_format in [('Hardcover','hardcover'),('Paperback','paperback'),
                                   ('Ebook','ebook'),('Audiobook','audiobook')]:
            with self.subTest(format=label):
                edition=WorkEquivalenceTests.edition(self,label,page_count=None if label=='Audiobook' else 384,
                    audio_seconds=36000 if label=='Audiobook' else None)
                s=self.submission(title='A different edition title',author='Edition contributor',pages=384,catalog=self.book)
                s.catalog_edition=edition;s.book_format=book_format;s.save()
                sync_botm(s);sync_tbr(s);s.refresh_from_db()
                self.assertTrue(BotmMatch.objects.get(submission=s).is_qualifying)
                self.assertTrue(PersonalTBRMatch.objects.get(submission=s).is_qualifying)
                self.assertEqual(s.approved_pages,384)
                # Withdrawal frees the qualification; it does not delete award history.
                s.is_removed=True;s.save();sync_botm(s);sync_tbr(s)

    def test_german_title_shared_work_needs_no_reviewer(self):
        self.target()
        s=self.submission(title='Nicht verliebt',author='Ali Hazelwood und Übersetzer',catalog=self.book,pages=384)
        s.catalog_edition=WorkEquivalenceTests.edition(self,'German paperback',page_count=420);s.save()
        sync_botm(s);sync_tbr(s)
        for model in [BotmMatch,PersonalTBRMatch]:
            m=model.objects.get(submission=s)
            self.assertTrue(m.is_qualifying);self.assertEqual(m.method,'catalog_work')
            self.assertIsNone(m.reviewer_id)
        s.refresh_from_db();self.assertEqual(s.approved_pages,384)

    def test_translation_without_shared_work_preserves_intent_and_can_be_reviewed(self):
        target=self.botm(title='Not in Love',author='Ali Hazelwood')
        s=self.submission(title='Nicht verliebt',author='Ali Hazelwood und Übersetzer',save=False,status='pending')
        capture_intent(s,actor=self.reader.user,botm_id=target.pk,
            tokens={'botm':selection_token(target,self.reader.user)})
        s.status='approved';s.approved_pages=158;s.save();sync_botm(s)
        m=BotmMatch.objects.get(submission=s)
        self.assertEqual(m.status,'pending_review');self.assertFalse(m.is_qualifying)
        self.confirm(s,self.actor('floater'),'botm',m)
        s.refresh_from_db();self.assertEqual(s.selected_botm_book_id,target.pk)
        self.assertEqual((s.approved_pages,s.final_scored_pages),(158,408))

    def test_different_translation_work_has_no_unverified_equivalence(self):
        target=self.target()
        other=CatalogBook.objects.create(provider='hardcover',provider_book_id='translation',title='Nicht verliebt',author=self.book.author)
        s=self.submission(title=other.title,author=other.author,catalog=other,save=False)
        capture_intent(s,actor=self.reader.user,botm_id=target.pk,tokens={'botm':selection_token(target,self.reader.user)})
        s.save();sync_botm(s);m=BotmMatch.objects.get(submission=s)
        self.assertFalse(m.is_qualifying);self.assertTrue(m.conflict_reason)
        with self.assertRaises(ValidationError):self.confirm(s,self.actor('floater'),'botm',m)

    def test_volume_and_adaptation_contradictions_stay_protected_even_with_shared_work(self):
        for left,right in [('Series Volume 14','Series Volume 15'),
                           ('Series Manga','Series Light Novel'),
                           ('Series Graphic Novel','Series Novel')]:
            target=self.target();target.title_snapshot=right;target.save()
            s=self.submission(title=left,catalog=self.book)
            self.assertEqual(identity_evidence(s,target)[0],'conflict')
            target.delete();s.delete()

    def test_abridgement_uncertainty_is_reviewable_not_automatic(self):
        self.target()
        s=self.submission(title=self.book.title,author=self.book.author,catalog=self.book)
        s.catalog_edition=WorkEquivalenceTests.edition(self,'Audiobook abridged',audio_seconds=12000);s.save()
        sync_botm(s);m=BotmMatch.objects.get(submission=s)
        self.assertFalse(m.is_qualifying);self.assertEqual(m.status,'pending_review')
        self.assertIn('Abridged',m.evidence_summary)
        target=BotmMatch.objects.get(pk=m.pk).botm_book
        target.title_snapshot += ' Unabridged'
        self.assertEqual(identity_evidence(s,target)[0],'pending')
        self.confirm(s,self.actor('floater'),'botm',m)
        s.refresh_from_db();self.assertEqual(s.approved_pages,158)

    def test_selected_alternate_edition_preserves_intent_awards_and_duplicates(self):
        target=self.target();s=self.submission(title=self.book.title,author=self.book.author,catalog=self.book,save=False)
        capture_intent(s,actor=self.reader.user,botm_id=target.pk,tokens={'botm':selection_token(target,self.reader.user)})
        s.catalog_edition=WorkEquivalenceTests.edition(self,'Ebook',page_count=399);s.save();sync_botm(s)
        before=list(ModifierProvenance.objects.filter(submission=s).values());stamp=identity_stamp(s,target)
        s.catalog_edition=WorkEquivalenceTests.edition(self,'Paperback',page_count=402);s.save();sync_botm(s)
        self.assertEqual(identity_stamp(s,target),stamp)
        self.assertEqual(list(ModifierProvenance.objects.filter(submission=s).values()),before)
        self.assertEqual(s.selected_botm_book_id,target.pk)
        duplicate=self.submission(title=self.book.title,author=self.book.author,catalog=self.book)
        sync_botm(duplicate);self.assertFalse(BotmMatch.objects.get(submission=duplicate).is_qualifying)
        self.assertFalse(HardcoverSyncOutbox.objects.exists())

    def test_edition_parent_is_evidence_but_mismatched_parent_is_conflict(self):
        target=self.target();s=self.submission(title='Nicht verliebt',author='Translator')
        s.catalog_edition=self.edition
        self.assertEqual(identity_evidence(s,target)[0],'work')
        s.catalog_book=CatalogBook.objects.create(provider='hardcover',provider_book_id='wrong',title='Wrong',author='Other')
        self.assertEqual(identity_evidence(s,target)[0],'conflict')
