"""Artwork-only edits against locked synthetic selections and temporary media."""
import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction, OperationalError
from django.test import TransactionTestCase, override_settings, Client
from django.urls import reverse
from . import test_submission_hotfix as fixture
from . import personal_tbr_covers as covers
from .models import (PersonalTBRBook, AuditEvent, BookSubmission, PersonalTBRMatch, PersonalTBRCompletionAward,
    ModifierProvenance, MonthEnrollment, Membership, ReadingGroup, ChallengeMonth, DiscordEventDelivery)
from .templatetags.book_covers import safe_cover_url


class PersonalTBRCoverTests(TransactionTestCase):
    def setUp(self):
        fixture.SubmissionHotfixTests.setUp(self)
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        setting=override_settings(MEDIA_ROOT=self.temp.name)
        setting.enable();self.addCleanup(setting.disable)
        self.entry=self.tbr.books.first()
        self.url=reverse('personal-tbr-cover',args=['hotfix',self.month.pk,self.entry.pk])

    def image(self,name='../../unsafe.jpg',fmt='PNG',size=(30,50)):
        data=io.BytesIO();Image.new('RGB',size,'blue').save(data,format=fmt)
        return SimpleUploadedFile(name,data.getvalue(),content_type='image/'+fmt.lower())

    def update(self,**kwargs):
        book=covers.load(self.entry.pk)
        return covers.update_cover(actor=self.reader.user,book=book,version=covers.cover_version(book,self.reader.user),**kwargs)

    def files(self):
        return sorted(str(p.relative_to(self.temp.name)) for p in Path(self.temp.name).rglob('*') if p.is_file())

    def evidence(self):
        books=list(self.tbr.books.order_by('pk').values())
        for book in books:book.pop('custom_cover')
        return {'books':books,'tbr':list(type(self.tbr).objects.values()),
            **{model.__name__:list(model.objects.order_by('pk').values()) for model in (
                BookSubmission,PersonalTBRMatch,PersonalTBRCompletionAward,ModifierProvenance,DiscordEventDelivery)}}

    def test_locked_upload_replace_remove_preserve_all_nine_selections_and_evidence(self):
        before=self.evidence()
        first=self.update(upload=self.image());old=first.custom_cover.name
        second=self.update(upload=self.image('replacement.webp','WEBP'));new=second.custom_cover.name
        self.assertNotEqual(old,new)
        self.assertTrue(Path(self.temp.name,old).is_file()) # Existing media convention retains old assets.
        self.assertEqual(self.evidence(),before)
        result=self.update(remove=True)
        self.assertFalse(result.custom_cover)
        self.assertEqual(self.evidence(),before)
        self.assertEqual(self.tbr.books.count(),9)
        self.assertTrue(Path(self.temp.name,new).is_file())
        self.assertEqual(list(AuditEvent.objects.filter(object_type='PersonalTBRBook').order_by('pk').values_list('action',flat=True)),
            ['personal_tbr.cover_uploaded','personal_tbr.cover_replaced','personal_tbr.cover_removed'])
        self.assertFalse(AuditEvent.objects.filter(action__startswith='submission.').exists())

    def test_own_unlocked_upload_and_visible_card_controls(self):
        type(self.tbr).objects.filter(pk=self.tbr.pk).update(confirmed_at=None)
        self.update(upload=self.image())
        response=self.client.get(reverse('personal-tbr-detail',args=['hotfix',self.month.pk]))
        self.assertContains(response,'Change Cover')
        self.assertContains(response,'has not been locked yet')
        self.assertContains(response,self.url)

    def test_locked_cards_and_upload_page_show_separate_artwork_controls(self):
        response=self.client.get(reverse('personal-tbr-detail',args=['hotfix',self.month.pk]))
        self.assertContains(response,'Upload Cover',count=18) # Visible label and accessible label, nine cards.
        self.assertContains(response,'selections are permanently locked')
        response=self.client.get(self.url)
        self.assertContains(response,'multipart/form-data')
        self.assertNotContains(response,'name="title_snapshot"')
        self.assertNotContains(response,'name="catalog_book"')
        self.update(upload=self.image())
        self.assertContains(self.client.get(self.url),'Remove Custom Cover')

    def test_other_reader_and_owner_staff_cannot_edit_foreign_artwork(self):
        for role in ('member','owner'):
            user=get_user_model().objects.create_user('other-'+role)
            Membership.objects.create(group=self.month.group,user=user,role=role,display_name=role)
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.url).status_code,403)
            self.assertEqual(self.client.post(self.url,{'version':covers.cover_version(self.entry,user),'artwork':self.image()}).status_code,403)
            with self.assertRaises(PermissionDenied):
                covers.update_cover(actor=user,book=self.entry,version=covers.cover_version(self.entry,user),upload=self.image())
        self.assertFalse(self.files())

    def test_cross_group_cross_challenge_and_invalid_entry_denied(self):
        foreign=ReadingGroup.objects.create(name='Foreign',slug='foreign')
        month=ChallengeMonth.objects.create(group=foreign,name='Foreign',status='active',starts_on=self.month.starts_on,ends_on=self.month.ends_on)
        for args in [('foreign',self.month.pk,self.entry.pk),('hotfix',month.pk,self.entry.pk),('hotfix',self.month.pk,99999)]:
            self.assertEqual(self.client.post(reverse('personal-tbr-cover',args=args),{'artwork':self.image()}).status_code,404)

    def test_historical_lifecycle_and_departure_are_read_only(self):
        for status in ('finalizing','completed','archived'):
            type(self.month).objects.filter(pk=self.month.pk).update(status=status)
            self.assertFalse(covers.can_manage(self.reader.user,covers.load(self.entry.pk)))
            self.assertEqual(self.client.post(self.url,{'artwork':self.image()}).status_code,403)
        type(self.month).objects.filter(pk=self.month.pk).update(status='active')
        MonthEnrollment.objects.filter(month=self.month,participant=self.reader).update(is_active=False)
        self.assertEqual(self.client.get(self.url).status_code,403)

    def test_formats_are_normalized_and_filename_never_used(self):
        for fmt in ('JPEG','PNG','WEBP'):
            book=self.update(upload=self.image('../../escape.svg',fmt,size=(1600,2400)))
            self.assertRegex(book.custom_cover.name,r'^tbr-covers/[0-9]+/[0-9a-f]{32}\.png$')
            with Image.open(book.custom_cover.path) as image:
                self.assertEqual(image.format,'PNG');self.assertEqual(image.size,(800,1200))
                self.assertFalse(image.getexif())

    def test_unsupported_malformed_and_oversized_preserve_current_cover(self):
        original=self.update(upload=self.image()).custom_cover.name
        uploads=[self.image('animation.gif','GIF'),SimpleUploadedFile('malformed.png',b'not an image'),
            SimpleUploadedFile('remote.url',b'https://example.com/picture.jpg'),
            SimpleUploadedFile('huge.png',b'x'*(5*1024*1024+1)),self.image('wide.png',size=(6001,1))]
        before=self.files()
        for upload in uploads:
            with self.subTest(name=upload.name),self.assertRaises(ValidationError):self.update(upload=upload)
            self.assertEqual(covers.load(self.entry.pk).custom_cover.name,original)
        self.assertEqual(self.files(),before)

    def test_database_failure_rolls_back_reference_audit_and_new_file(self):
        original=self.update(upload=self.image()).custom_cover.name
        before=self.files();audits=AuditEvent.objects.count()
        with patch('core.personal_tbr_covers.AuditEvent.objects.create',side_effect=OperationalError('synthetic')):
            with self.assertRaises(OperationalError):self.update(upload=self.image())
        self.assertEqual(covers.load(self.entry.pk).custom_cover.name,original)
        self.assertEqual(AuditEvent.objects.count(),audits)
        self.assertEqual(self.files(),before)

    def test_storage_failure_preserves_existing_reference(self):
        original=self.update(upload=self.image()).custom_cover.name
        field=PersonalTBRBook._meta.get_field('custom_cover')
        with patch.object(field.storage,'save',side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):self.update(upload=self.image())
        self.assertEqual(covers.load(self.entry.pk).custom_cover.name,original)

    def test_custom_cover_priority_and_removal_restores_fallback(self):
        PersonalTBRBook.objects.filter(pk=self.entry.pk).update(display_cover_url='https://example.com/hardcover.jpg')
        result=self.update(upload=self.image())
        self.assertEqual(result.effective_display_cover_url,result.custom_cover.url)
        self.assertEqual(safe_cover_url(result.effective_display_cover_url),result.custom_cover.url)
        result=self.update(remove=True)
        self.assertEqual(result.effective_display_cover_url,'https://example.com/hardcover.jpg')
        response=self.client.get(reverse('personal-tbr-detail',args=['hotfix',self.month.pk]))
        self.assertContains(response,'https://example.com/hardcover.jpg')

    def test_missing_custom_file_and_empty_fallback_use_normal_placeholder(self):
        result=self.update(upload=self.image())
        Path(result.custom_cover.path).unlink()
        self.assertEqual(result.effective_display_cover_url,'')
        response=self.client.get(reverse('personal-tbr-detail',args=['hotfix',self.month.pk]))
        self.assertContains(response,'data-book-cover-placeholder')
        self.assertNotContains(response,'src="'+result.custom_cover.url+'"')

    def test_shared_files_and_hardcover_art_are_never_deleted(self):
        result=self.update(upload=self.image());shared=result.custom_cover.name
        other=self.tbr.books.last()
        PersonalTBRBook.objects.filter(pk=other.pk).update(custom_cover=shared)
        self.update(remove=True)
        self.assertTrue(Path(self.temp.name,shared).exists())
        self.assertTrue(covers.load(other.pk).custom_cover.storage.exists(shared))
        self.assertTrue(covers.load(other.pk).effective_display_cover_url)

    def test_stale_tampered_and_duplicate_forms_fail_closed(self):
        token=covers.cover_version(self.entry,self.reader.user)
        self.update(upload=self.image())
        for version in (token,token+'tampered'):
            with self.assertRaises(ValidationError):
                covers.update_cover(actor=self.reader.user,book=self.entry,version=version,upload=self.image())
        with patch('django.core.signing.time.time',return_value=1):expired=covers.cover_version(covers.load(self.entry.pk),self.reader.user)
        with self.assertRaises(ValidationError):covers.update_cover(actor=self.reader.user,book=self.entry,version=expired,remove=True)

    def test_general_locked_identity_editing_remains_prohibited(self):
        self.update(upload=self.image())
        book=covers.load(self.entry.pk);book.title_snapshot='Cannot change this'
        with self.assertRaises(ValidationError):book.save()
        with self.assertRaises(ValidationError):book.delete()

    def test_matching_scoring_reconciliation_and_discord_are_never_called(self):
        paths=['core.personal_tbr_matching.synchronize_submission','core.botm_matching.synchronize_submission',
            'core.scoring.refresh_submission_score','core.tbr_reconciliation._repair','core.discord_events.enqueue']
        from contextlib import ExitStack
        with ExitStack() as stack:
            mocks=[stack.enter_context(patch(path)) for path in paths]
            self.update(upload=self.image());self.update(remove=True)
            for mock in mocks:mock.assert_not_called()
        self.assertFalse(DiscordEventDelivery.objects.exists())

    def test_post_ignores_identity_fields_and_enforces_csrf_and_authentication(self):
        before=self.evidence()
        response=self.client.post(self.url,{'version':covers.cover_version(self.entry,self.reader.user),'action':'upload',
            'artwork':self.image(),'title_snapshot':'Forged','position':9,'confirmed_at':'','catalog_book':999})
        self.assertEqual(response.status_code,302);self.assertEqual(self.evidence(),before)
        client=Client(enforce_csrf_checks=True)
        self.assertEqual(client.get(self.url).status_code,302)
        client.force_login(self.reader.user)
        self.assertEqual(client.post(self.url,{'action':'remove'}).status_code,403)

    def test_enclosing_transaction_is_rejected_before_any_storage_write(self):
        with transaction.atomic(),self.assertRaises(RuntimeError):self.update(upload=self.image())
        self.assertFalse(self.files())

    def test_only_generated_media_namespace_is_accepted_for_local_covers(self):
        valid='/media/tbr-covers/1/'+'a'*32+'.png'
        self.assertEqual(safe_cover_url(valid),valid)
        for value in ('/media/private.png','/media/tbr-covers/1/../secret.png','//evil.example/cover.png','/media/tbr-covers/1/%2e%2e/file.png'):
            self.assertEqual(safe_cover_url(value),'')

    def test_existing_qualifications_progress_and_frozen_award_remain_identical(self):
        from .personal_tbr_matching import synchronize_submission
        from .personal_tbr_presentation import build_personal_tbr_reader_presentation
        from datetime import date
        for book in self.tbr.books.all():
            s=BookSubmission.objects.create(month=self.month,participant=self.reader,title=book.title_snapshot,author=book.author_snapshot,
                book_format='ebook',completed_on=date(2026,11,3),submitted_pages=100,approved_pages=100,status='approved',catalog_book=book.catalog_book)
            synchronize_submission(s)
        self.assertEqual(build_personal_tbr_reader_presentation(personal_tbr=self.tbr)['completed_count'],9)
        self.assertTrue(PersonalTBRCompletionAward.objects.get().is_qualifying)
        before=self.evidence()
        self.update(upload=self.image());self.update(upload=self.image());self.update(remove=True)
        self.assertEqual(self.evidence(),before)
        self.assertEqual(build_personal_tbr_reader_presentation(personal_tbr=self.tbr)['completed_count'],9)

    def test_existing_provider_refresh_columns_do_not_override_custom_cover(self):
        book=self.update(upload=self.image());custom=book.custom_cover.name
        PersonalTBRBook.objects.filter(pk=book.pk).update(display_cover_url='https://example.com/refreshed.jpg')
        book=covers.load(book.pk)
        self.assertEqual(book.custom_cover.name,custom)
        self.assertEqual(book.effective_display_cover_url,book.custom_cover.url)
        self.assertEqual(self.update(remove=True).effective_display_cover_url,'https://example.com/refreshed.jpg')

    def test_unlocked_draft_replacement_with_same_books_retains_override(self):
        from .personal_tbr import replace_draft_personal_tbr, _book_values
        type(self.tbr).objects.filter(pk=self.tbr.pk).update(confirmed_at=None)
        custom=self.update(upload=self.image()).custom_cover.name
        values=[_book_values({field:getattr(book,field) for field in (
            'position','catalog_book','catalog_edition','title_snapshot','author_snapshot','page_count_snapshot','cover_url_snapshot','source_url_snapshot')}) for book in self.tbr.books.all()]
        replace_draft_personal_tbr(enrollment=self.tbr.enrollment,books=values)
        self.assertEqual(covers.load(self.entry.pk).custom_cover.name,custom)

    def test_upcoming_artwork_allowed_but_draft_challenge_visibility_preserved(self):
        type(self.month).objects.filter(pk=self.month.pk).update(status='upcoming')
        self.update(upload=self.image())
        type(self.month).objects.filter(pk=self.month.pk).update(status='draft')
        self.assertFalse(covers.can_manage(self.reader.user,covers.load(self.entry.pk)))

    def test_authority_is_rechecked_after_image_processing(self):
        original=covers.image_asset
        def departed(*args,**kwargs):
            result=original(*args,**kwargs)
            MonthEnrollment.objects.filter(pk=self.tbr.enrollment_id).update(is_active=False)
            return result
        with patch.object(covers,'image_asset',side_effect=departed):
            with self.assertRaises(PermissionDenied):self.update(upload=self.image())
        self.assertFalse(self.files())
        self.assertFalse(covers.load(self.entry.pk).custom_cover)

    def test_malformed_upload_renders_error_without_changing_artwork(self):
        original=self.update(upload=self.image()).custom_cover.name
        book=covers.load(self.entry.pk)
        response=self.client.post(self.url,{'version':covers.cover_version(book,self.reader.user),
            'artwork':SimpleUploadedFile('bad.png',b'invalid'),'action':'upload'})
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'Choose a valid PNG, JPEG, or WebP image')
        self.assertEqual(covers.load(self.entry.pk).custom_cover.name,original)
