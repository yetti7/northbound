from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from django.db import close_old_connections,connection
from django.test import TransactionTestCase,override_settings
from django.core.exceptions import ValidationError, PermissionDenied
from . import test_scavenger_photo_review as fixtures
from .models import ModifierProvenance,PromptCompletion,ScavengerPhotoSubmission
from .photo_submissions import create_photo_submission
from .photo_review import review_photo_submission,photo_review_version


@override_settings(NORTHBOUND_PROMPT_RESERVATIONS_READY=True)
class PhotoSQLiteConcurrencyTests(TransactionTestCase):
    token=fixtures.PhotoReviewTests.token
    prompt=fixtures.PhotoReviewTests.prompt
    settings=fixtures.PhotoReviewTests.settings
    create=fixtures.PhotoReviewTests.create
    protected=fixtures.PhotoReviewTests.protected
    setUp=fixtures.PhotoReviewTests.setUp

    def race(self,photos,decisions):
        barrier=Barrier(2)
        requests=[(photo.pk,photo_review_version(photo,self.actor),decision) for photo,decision in zip(photos,decisions)]
        actor=self.actor
        def run(request):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                pk,version,decision=request
                result=review_photo_submission(photo_id=pk,actor=actor,version=version,decision=decision,reviewer_verified=True)
                return ('ok',result.status)
            except (ValidationError,PermissionDenied) as exc:
                return ('conflict',type(exc).__name__)
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:return list(pool.map(run,requests))

    def test_two_reviewers_one_receipt(self):
        if connection.vendor!='sqlite':self.skipTest('SQLite backend concurrency check only')
        photo=self.create();results=self.race([photo,photo],['approved','approved'])
        self.assertEqual(sum(r[0]=='ok' for r in results),1,results)
        self.assertEqual(ModifierProvenance.objects.filter(source_type='scavenger_prompt').count(),1)
        self.assertEqual(ScavengerPhotoSubmission.objects.get(pk=photo.pk).status,'approved')

    def test_approval_rejection_race_has_one_consistent_result(self):
        if connection.vendor!='sqlite':self.skipTest('SQLite backend concurrency check only')
        photo=self.create();results=self.race([photo,photo],['approved','rejected'])
        self.assertEqual(sum(r[0]=='ok' for r in results),1,results)
        photo.refresh_from_db()
        self.assertEqual(ModifierProvenance.objects.count(),1 if photo.status=='approved' else 0)
        self.assertEqual(photo.prompt_claim.state,'qualified' if photo.status=='approved' else 'rejected')

    def test_concurrent_final_prompts_cannot_duplicate_completion(self):
        if connection.vendor!='sqlite':self.skipTest('SQLite backend concurrency check only')
        self.book_prompt.submission_type='photo';self.book_prompt.save()
        first=self.create();second=create_photo_submission(month_id=self.month.pk,actor=self.reader.user,prompt_id=self.book_prompt.pk,reader_confirmed=True)
        results=self.race([first,second],['approved','approved'])
        self.assertEqual(sum(r[0]=='ok' for r in results),1,results)
        loser=ScavengerPhotoSubmission.objects.get(status='pending')
        review_photo_submission(photo_id=loser.pk,actor=self.actor,version=photo_review_version(loser,self.actor),decision='approved',reviewer_verified=True)
        self.assertEqual(PromptCompletion.objects.count(),1)
        self.assertEqual(ModifierProvenance.objects.filter(source_type='scavenger_completion').count(),1)

    def test_approval_against_reader_withdrawal_and_edit(self):
        if connection.vendor!='sqlite':self.skipTest('SQLite backend concurrency check only')
        from .photo_submissions import transition_photo_reservation,edit_pending_photo
        self.book_prompt.submission_type='photo';self.book_prompt.save()
        for kind,prompt in [('withdraw',self.photo_prompt),('edit',self.book_prompt)]:
            photo=create_photo_submission(month_id=self.month.pk,actor=self.reader.user,prompt_id=prompt.pk,reader_confirmed=True)
            token=photo_review_version(photo,self.actor);barrier=Barrier(2)
            def run(action):
                close_old_connections()
                try:
                    barrier.wait(timeout=10)
                    if action=='approve':review_photo_submission(photo_id=photo.pk,actor=self.actor,version=token,decision='approved',reviewer_verified=True)
                    elif kind=='withdraw':transition_photo_reservation(photo_id=photo.pk,actor=self.reader.user,version=photo.version,status='withdrawn')
                    else:edit_pending_photo(photo_id=photo.pk,actor=self.reader.user,version=photo.version,prompt_id=prompt.pk,discord_message_url='https://discord.com/channels/1/2/3')
                    return 'ok'
                except (ValidationError,PermissionDenied):return 'conflict'
                finally:close_old_connections()
            with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,['approve','reader']))
            self.assertEqual(results.count('ok'),1,results)
            photo.refresh_from_db();receipt=ModifierProvenance.objects.filter(source_reference=f'prompt_claim:{photo.prompt_claim.pk}')
            self.assertEqual(receipt.count(),1 if photo.status=='approved' else 0)

    def test_concurrent_approved_corrections_one_current_receipt(self):
        if connection.vendor!='sqlite':self.skipTest('SQLite backend concurrency check only')
        from .photo_actions import correction_preview,correct_photo,management_version
        third=self.prompt(3,kind='scavenger',submission_type='photo',points=99)
        photo=self.create();photo=review_photo_submission(photo_id=photo.pk,actor=self.actor,version=photo_review_version(photo,self.actor),decision='approved',reviewer_verified=True)
        token=correction_preview(photo,self.actor,prompt_id=third.pk)['token'];version=management_version(photo,self.actor,'correct')
        actor=self.actor;barrier=Barrier(2)
        def run(_):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                correct_photo(photo_id=photo.pk,actor=actor,version=version,prompt_id=third.pk,preview=token)
                return 'ok'
            except (ValidationError,PermissionDenied):return 'conflict'
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,[1,2]))
        self.assertEqual(results.count('ok'),1,results)
        rows=ModifierProvenance.objects.filter(source_type='scavenger_prompt')
        self.assertEqual(rows.count(),2);self.assertEqual(rows.filter(is_active=True).get().amount,99)
        self.assertFalse(PromptCompletion.objects.exists())
