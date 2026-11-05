"""Focused Hardcover association/preview safeguards; providers are always mocked."""
from unittest.mock import patch
from django.core.exceptions import ValidationError, PermissionDenied
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from . import test_submission_hotfix as fixture
from . import test_submission_editing as editing
from .models import (CatalogBook, CatalogEdition, BookSubmission, AuditEvent, ImmersiveRequest,
                     ModifierProvenance, ProgressCheckpoint, ProgressCheckpointResult)
from .submission_corrections import correction_version, preview_correction, apply_previewed_correction
from .immersive import configure_immersive, request_immersive, review_immersive


class SubmissionAssociationTests(TransactionTestCase):
    setUp = fixture.SubmissionHotfixTests.setUp
    actor = editing.SubmissionEditingTests.actor
    submission = editing.SubmissionEditingTests.submission
    payload = editing.SubmissionEditingTests.payload

    def selection(self, audio=False, same_work=False):
        book = self.book if same_work else CatalogBook.objects.create(provider='hardcover',
            provider_book_id='101', title='Selected book', author='Writer')
        selected = CatalogEdition.objects.create(provider='hardcover', provider_edition_id='202',
            book=book, page_count=None if audio else 673, format_name='Audiobook' if audio else 'Ebook',
            audio_seconds=36000 if audio else None)
        scoring = CatalogEdition.objects.create(provider='hardcover', provider_edition_id='203',
            book=book, page_count=673, format_name='Paperback') if audio else selected
        return selected, scoring

    def metadata(self, *args, **kwargs):
        return {'title':'Selected book', 'author':'Writer',
                'page_count': None if kwargs['edition_id']=='202' and self.audio else 673}

    def preview(self, item, actor, changes):
        return preview_correction(submission=item,actor=actor,changes=changes,
            version=correction_version(item,actor),hardcover_token='synthetic')

    def apply(self, item, actor, changes, plan, **kwargs):
        return apply_previewed_correction(submission=item,actor=actor,changes=changes,
            version=plan['version'],preview_token=plan['token'],hardcover_token='synthetic',**kwargs)

    def make_plan(self, item, actor, changes):
        version=correction_version(item,actor)
        plan=preview_correction(submission=item,actor=actor,changes=changes,version=version,hardcover_token='synthetic')
        plan['version']=version
        return plan

    def test_global_dropdown_removed_and_only_posted_or_current_edition_loaded(self):
        item=self.submission();actor=self.actor();selected,_=self.selection()
        self.client.force_login(actor)
        response=self.client.get(self.edit_url)
        self.assertContains(response,'Find or change Hardcover association')
        self.assertNotContains(response,'<select name="edition"')
        self.assertEqual(list(response.context['form'].fields['edition'].queryset),[])
        self.assertContains(response,'value="Not in Love"')

    def test_search_and_selection_authorization_and_work_identity(self):
        item=self.submission();actor=self.actor();selected,_=self.selection()
        url=reverse('submission-edit-catalog',args=['hotfix',self.month.pk,item.pk])
        with patch('core.views._staff_tbr_search_token',return_value='synthetic') as credential,patch(
                'core.views.search_books',return_value=([{'book_id':'101','title':'Selected'}],False)):
            self.client.force_login(self.reader.user)
            self.assertEqual(self.client.post(url,{'action':'search','query':'Title'}).status_code,403)
            credential.assert_not_called()
            self.client.force_login(actor)
            self.assertEqual(self.client.post(url,{'action':'search','query':'Title'}).status_code,200)
            self.assertEqual(self.client.get(url).status_code,405)
            with patch('core.views.lookup_edition',return_value=({'book_id':'101','edition_id':'202'},False)):
                self.assertEqual(self.client.post(url,{'action':'edition','book_id':'999','edition_id':'202'}).status_code,400)
        self.assertEqual(self.client.post(reverse('submission-edit-catalog',args=['wrong',self.month.pk,item.pk]),{}).status_code,404)

    def test_manual_to_hardcover_update_requires_confirmation_and_preserves_history(self):
        item=self.submission();actor=self.actor();selected,_=self.selection();self.audio=False
        changes={'catalog_edition_id':selected.pk,'association_mode':'update'}
        before=list(AuditEvent.objects.values())
        with patch('core.submission_corrections.fetch_edition_metadata',side_effect=self.metadata):
            plan=self.make_plan(item,actor,changes)
            item.refresh_from_db();self.assertEqual(item.approved_pages,350);self.assertIsNone(item.catalog_book_id)
            self.assertEqual(list(AuditEvent.objects.values()),before)
            with self.assertRaises(ValidationError):self.apply(item,actor,changes,plan)
            with self.assertRaises(ValidationError):self.apply(item,actor,changes,plan,confirm_approved=True)
            corrected=self.apply(item,actor,changes,plan,confirm_approved=True,confirm_work=True)
        self.assertEqual((corrected.pk,corrected.approved_pages,corrected.title),(item.pk,673,'Selected book'))
        self.assertEqual(corrected.verification_method,'hardcover')
        self.assertTrue(AuditEvent.objects.filter(action='submission.corrected',object_id=str(item.pk)).exists())
        self.assertEqual(BookSubmission.objects.count(),1)

    def test_associate_only_preserves_scores_awards_approval_and_matches(self):
        item=self.submission(catalog=True);actor=self.actor();selected,_=self.selection();self.audio=False
        configure_immersive(month=self.month,actor=actor,enabled=True,percent=100)
        claim=review_immersive(request=request_immersive(submission=item,actor=self.reader.user),actor=actor,decision='approved')
        item.refresh_from_db()
        old=(item.title,item.approved_pages,item.bonus_pages,item.final_scored_pages,item.status,item.verification_method)
        sources=list(ModifierProvenance.objects.values());matches=list(item.personal_tbr_matches.values());requests=list(ImmersiveRequest.objects.values())
        changes={'catalog_edition_id':selected.pk,'association_mode':'only'}
        with patch('core.submission_corrections.fetch_edition_metadata',side_effect=self.metadata):
            plan=self.make_plan(item,actor,changes)
            corrected=self.apply(item,actor,changes,plan,confirm_approved=True,confirm_work=True)
        self.assertEqual((corrected.title,corrected.approved_pages,corrected.bonus_pages,corrected.final_scored_pages,corrected.status),old[:5])
        self.assertEqual(corrected.catalog_edition_id,selected.pk)
        self.assertEqual(list(ModifierProvenance.objects.values()),sources)
        self.assertEqual(list(item.personal_tbr_matches.values()),matches)
        self.assertEqual(list(ImmersiveRequest.objects.values()),requests)
        self.assertEqual(corrected.verification_method,'manual')

    def test_audio_equivalent_recalculates_captured_immersive_once_and_preserves_checkpoints(self):
        item=self.submission();actor=self.actor();selected,scoring=self.selection(audio=True);self.audio=True
        configure_immersive(month=self.month,actor=actor,enabled=True,percent=100)
        claim=review_immersive(request=request_immersive(submission=item,actor=self.reader.user),actor=actor,decision='approved')
        checkpoint=ProgressCheckpoint.objects.create(month=self.month,scheduled_at=timezone.now(),fixed_target_pages=100,target_basis='fixed')
        ProgressCheckpointResult.objects.create(checkpoint=checkpoint,participant=self.reader,evaluated_at=timezone.now(),threshold_percentage=25,
            progress_basis='base',target_basis='fixed',target_pages=100,required_pages=25,progress_pages=350,outcome='met')
        history=list(ProgressCheckpointResult.objects.values())
        changes={'catalog_edition_id':selected.pk,'association_mode':'update'}
        with patch('core.submission_corrections.fetch_edition_metadata',side_effect=self.metadata),patch(
                'core.submission_corrections.resolve_scoring_edition',return_value=({'edition_id':'203'},'hardcover_audio')) as resolver:
            plan=self.make_plan(item,actor,changes)
            self.assertEqual(plan['impact']['approved_pages'],673)
            corrected=self.apply(item,actor,changes,plan,confirm_approved=True,confirm_work=True)
            resolver.assert_called()
        self.assertEqual((corrected.approved_pages,corrected.bonus_pages,corrected.final_scored_pages),(673,673,1346))
        self.assertEqual((corrected.catalog_edition_id,corrected.scoring_catalog_edition_id),(selected.pk,scoring.pk))
        self.assertEqual(corrected.verification_method,'hardcover_audio')
        claim.refresh_from_db();self.assertEqual((claim.captured_percent,claim.status), (100,'approved'))
        self.assertEqual(ModifierProvenance.objects.filter(submission=item,source_type='immersive').count(),1)
        self.assertEqual(list(ProgressCheckpointResult.objects.values()),history)

    def test_missing_equivalent_never_zeroes_approved_pages(self):
        item=self.submission();actor=self.actor();selected,_=self.selection(audio=True);self.audio=True
        changes={'catalog_edition_id':selected.pk,'association_mode':'update'}
        with patch('core.submission_corrections.fetch_edition_metadata',side_effect=self.metadata),patch(
                'core.submission_corrections.resolve_scoring_edition',return_value=(None,None)):
            with self.assertRaises(ValidationError):self.make_plan(item,actor,changes)
            item.refresh_from_db();self.assertEqual(item.approved_pages,350)
            changes['page_count']=350
            plan=self.make_plan(item,actor,changes)
            corrected=self.apply(item,actor,changes,plan,confirm_approved=True,confirm_work=True)
            self.assertEqual((corrected.approved_pages,corrected.verification_method),(350,'manual'))

    def test_stale_preview_inputs_authority_and_provider_pages_rejected(self):
        item=self.submission();actor=self.actor();selected,_=self.selection();self.audio=False
        changes={'catalog_edition_id':selected.pk,'association_mode':'update'}
        with patch('core.submission_corrections.fetch_edition_metadata',side_effect=self.metadata):
            plan=self.make_plan(item,actor,changes)
            with self.assertRaises(ValidationError):self.apply(item,actor,{**changes,'page_count':800},plan,confirm_approved=True,confirm_work=True)
            with self.assertRaises((ValidationError,PermissionDenied)):self.apply(item,self.reader.user,changes,plan,confirm_approved=True,confirm_work=True)
            with patch('core.submission_corrections.fetch_edition_metadata',return_value={'title':'Selected book','author':'Writer','page_count':900}):
                with self.assertRaises(ValidationError):self.apply(item,actor,changes,plan,confirm_approved=True,confirm_work=True)
            BookSubmission.objects.filter(pk=item.pk).update(approved_pages=351)
            with self.assertRaises(ValidationError):self.apply(item,actor,changes,plan,confirm_approved=True,confirm_work=True)
        self.assertFalse(AuditEvent.objects.filter(action='submission.corrected',object_id=str(item.pk)).exists())

    def test_web_preview_then_confirm_and_stale_immersive_state(self):
        item=self.submission();actor=self.actor();selected,_=self.selection();self.audio=False
        self.client.force_login(actor);data=self.payload(item,actor,edition=selected.pk,association_mode='update')
        with patch('core.views._staff_tbr_search_token',return_value='synthetic'),patch(
                'core.submission_corrections.fetch_edition_metadata',side_effect=self.metadata):
            response=self.client.post(self.edit_url,data)
            self.assertEqual(response.status_code,200);self.assertContains(response,'Preview before confirmation')
            data['preview_token']=response.context['preview']['token'];data['confirm_approved']='on';data['confirm_work']='on'
            self.assertEqual(self.client.post(self.edit_url,data).status_code,302)
        item.refresh_from_db();self.assertEqual(item.approved_pages,673)

    def test_preview_invalidated_by_immersive_review_and_revoked_editor(self):
        item=self.submission();actor=self.actor();selected,_=self.selection();self.audio=False
        configure_immersive(month=self.month,actor=actor,enabled=True,percent=100)
        request=request_immersive(submission=item,actor=self.reader.user)
        changes={'catalog_edition_id':selected.pk,'association_mode':'update'}
        with patch('core.submission_corrections.fetch_edition_metadata',side_effect=self.metadata):
            plan=self.make_plan(item,actor,changes)
            review_immersive(request=request,actor=actor,decision='approved')
            with self.assertRaises(ValidationError):self.apply(item,actor,changes,plan,confirm_approved=True,confirm_work=True)
            plan=self.make_plan(item,actor,changes)
            actor.is_active=False;actor.save(update_fields=['is_active'])
            with self.assertRaises(PermissionDenied):self.apply(item,actor,changes,plan,confirm_approved=True,confirm_work=True)

    def test_pending_link_does_not_autoapprove_book_or_immersive_request(self):
        item=self.submission(status='pending');actor=self.actor();selected,_=self.selection();self.audio=False
        configure_immersive(month=self.month,actor=actor,enabled=True,percent=100)
        request=request_immersive(submission=item,actor=self.reader.user)
        changes={'catalog_edition_id':selected.pk,'association_mode':'update'}
        with patch('core.submission_corrections.fetch_edition_metadata',side_effect=self.metadata):
            plan=self.make_plan(item,actor,changes)
            corrected=self.apply(item,actor,changes,plan,confirm_work=True)
        self.assertEqual((corrected.status,corrected.approved_pages,corrected.bonus_pages),('pending',None,0))
        request.refresh_from_db();self.assertEqual(request.status,'pending');self.assertFalse(request.is_active)

    def test_wrong_scoring_work_is_rejected(self):
        item=self.submission();actor=self.actor();selected,_=self.selection(audio=True);self.audio=True
        changes={'catalog_edition_id':selected.pk,'association_mode':'update'}
        with patch('core.submission_corrections.fetch_edition_metadata',side_effect=self.metadata),patch(
                'core.submission_corrections.resolve_scoring_edition',return_value=({'edition_id':self.edition.provider_edition_id},'hardcover_audio')):
            with self.assertRaises(ValidationError):self.make_plan(item,actor,changes)
        item.refresh_from_db();self.assertEqual(item.approved_pages,350)

    def test_associate_only_rejects_other_edits_and_preserves_manual_verification(self):
        item=self.submission();actor=self.actor();selected,_=self.selection();self.audio=False
        changes={'catalog_edition_id':selected.pk,'association_mode':'only'}
        with patch('core.submission_corrections.fetch_edition_metadata',side_effect=self.metadata):
            with self.assertRaises(ValidationError):self.make_plan(item,actor,{**changes,'page_count':999})
            plan=self.make_plan(item,actor,changes)
            corrected=self.apply(item,actor,changes,plan,confirm_approved=True,confirm_work=True)
        self.assertEqual((corrected.approved_pages,corrected.verification_method,corrected.title),(350,'manual',item.title))

    def test_creation_catalog_includes_both_edition_details_and_review_fallback(self):
        from .models import HardcoverConnection
        from .integrations.secrets import encrypt_token
        selected,scoring=self.selection(audio=True)
        HardcoverConnection.objects.create(group=self.month.group,is_valid=True,encrypted_token=encrypt_token('synthetic'))
        self.client.force_login(self.reader.user)
        url=reverse('submission-catalog',args=['hotfix',self.month.pk])
        audio={'book_id':'101','edition_id':'202','format':'Audiobook','audio_seconds':36000,'pages':None,'title':'Selected book'}
        print_edition={'book_id':'101','edition_id':'203','format':'Paperback','pages':673,'source_url':'https://hardcover.app/books/synthetic'}
        with patch('core.views.lookup_edition',return_value=(audio,False)),patch(
                'core.views.resolve_scoring_edition',return_value=(print_edition,'hardcover_audio')):
            data=self.client.post(url,{'action':'edition','edition_id':'202'}).json()['result']
            self.assertEqual((data['pages'],data['audio_seconds'],data['scoring_edition']['edition_id']),(673,36000,'203'))
        with patch('core.views.lookup_edition',return_value=(audio,False)),patch('core.views.resolve_scoring_edition',return_value=(None,None)):
            data=self.client.post(url,{'action':'edition','edition_id':'202'}).json()
            self.assertTrue(data['manual_required']);self.assertIn('staff review',data['message'])
        response=self.client.get(reverse('submission-create',args=['hotfix',self.month.pk]))
        self.assertContains(response,'data-audio-scoring')

    def test_search_scope_reuses_correction_authority_for_staff_and_platform(self):
        from .models import TeamAssignment
        item=self.submission()
        url=reverse('submission-edit-catalog',args=['hotfix',self.month.pk,item.pk])
        with patch('core.views._staff_tbr_search_token',return_value='synthetic'),patch('core.views.search_books',return_value=([],False)):
            for role,expected in (('owner',200),('moderator',403),('floater',200),('team_leader',200),('platform',200)):
                actor=self.actor(role);self.client.force_login(actor)
                self.assertEqual(self.client.post(url,{'action':'search','query':'Title'}).status_code,expected)
                if role=='team_leader':
                    TeamAssignment.objects.filter(participant=self.reader,ended_at__isnull=True).update(ended_at=timezone.now())
                    self.assertEqual(self.client.post(url,{'action':'search','query':'Title'}).status_code,403)

    def test_associate_only_same_edition_cannot_change_rejected_status(self):
        item=self.submission(status='rejected',catalog=True);actor=self.actor()
        with self.assertRaises(ValidationError):
            self.make_plan(item,actor,{'catalog_edition_id':item.catalog_edition_id,'association_mode':'only'})
        item.refresh_from_db();self.assertEqual(item.status,'rejected')

    def test_preview_provider_work_is_outside_transaction_and_all_score_writes_roll_back(self):
        from django.db import connection
        from .models import (ThemeClaim, PersonalTBRMatch, BotmMatch, PersonalTBRCompletionAward, BotmCompletionAward)
        item=self.submission(catalog=True);actor=self.actor();selected,scoring=self.selection(audio=True);self.audio=True
        configure_immersive(month=self.month,actor=actor,enabled=True,percent=100)
        review_immersive(request=request_immersive(submission=item,actor=self.reader.user),actor=actor,decision='approved')
        models=(BookSubmission,ThemeClaim,PersonalTBRMatch,BotmMatch,PersonalTBRCompletionAward,
                BotmCompletionAward,ModifierProvenance,ImmersiveRequest,AuditEvent,ProgressCheckpointResult)
        def state():return {model.__name__:list(model.objects.order_by('pk').values()) for model in models}
        before=state()
        def fetch(*args,**kwargs):
            self.assertFalse(connection.in_atomic_block)
            return self.metadata(*args,**kwargs)
        def resolve(*args,**kwargs):
            self.assertFalse(connection.in_atomic_block)
            # Simulate persistent provider cache refreshes to records already referenced by a submission.
            CatalogBook.objects.filter(pk=self.book.pk).update(title='Refreshed catalog title')
            CatalogEdition.objects.filter(pk=self.edition.pk).update(page_count=999)
            self.assertEqual(state(),before)
            return {'edition_id':'203'},'hardcover_audio'
        with patch('core.submission_corrections.fetch_edition_metadata',side_effect=fetch),patch(
                'core.submission_corrections.resolve_scoring_edition',side_effect=resolve):
            plan=self.make_plan(item,actor,{'catalog_edition_id':selected.pk,'association_mode':'update'})
        self.assertEqual(plan['impact']['approved_pages'],673)
        self.assertEqual(state(),before)
        self.edition.refresh_from_db();self.assertEqual(self.edition.page_count,999)
        item.refresh_from_db();self.assertEqual(item.approved_pages,350)

    def test_database_busy_after_preview_save_rolls_back_and_returns_form_error(self):
        from django.db import OperationalError
        item=self.submission();actor=self.actor();self.client.force_login(actor)
        before=list(BookSubmission.objects.values())
        data=self.payload(item,actor,submitted_pages=400)
        with patch('core.submission_corrections.refresh_submission_score',side_effect=OperationalError('PRIVATE DATABASE ERROR')):
            response=self.client.post(self.edit_url,data)
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'Another update prevented saving. Nothing was saved.')
        self.assertNotContains(response,'PRIVATE DATABASE ERROR')
        self.assertEqual(list(BookSubmission.objects.values()),before)
        self.assertFalse(AuditEvent.objects.filter(action='submission.corrected',object_id=str(item.pk)).exists())

    def test_catalog_cache_database_busy_is_retryable_without_internal_details(self):
        from django.db import OperationalError
        item=self.submission();actor=self.actor();self.client.force_login(actor)
        url=reverse('submission-edit-catalog',args=['hotfix',self.month.pk,item.pk])
        before=list(BookSubmission.objects.values())
        with patch('core.views._staff_tbr_search_token',return_value='synthetic'),patch(
                'core.views.lookup_edition',side_effect=OperationalError('PRIVATE DATABASE ERROR')):
            response=self.client.post(url,{'action':'edition','book_id':'101','edition_id':'202'})
        self.assertEqual(response.status_code,503)
        self.assertIn('Please try again',response.json()['message'])
        self.assertNotContains(response,'PRIVATE DATABASE ERROR',status_code=503)
        self.assertEqual(list(BookSubmission.objects.values()),before)
