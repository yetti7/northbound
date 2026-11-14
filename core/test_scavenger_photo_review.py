import os
from pathlib import Path
from datetime import datetime, timezone as dt_timezone
from unittest.mock import patch
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase,override_settings
from django.urls import reverse
from django.utils import timezone
from . import test_scavenger_photo_foundation as fixtures
from .models import (AuditEvent,BookSubmission,ChallengeMonth,Membership,MonthEnrollment,PromptClaim,
    PromptCompletion,ModifierProvenance,ScavengerPhotoSubmission,TeamAssignment)
from .photo_submissions import create_photo_submission,edit_pending_photo,transition_photo_reservation
from .photo_review import review_photo_submission,photo_review_version,pending_review_photos
from .prompt_reservations import replace_selections
from .prompt_scoring import review_prompts
from .score_aggregation import challenge_score_totals,challenge_team_totals
from .review_attention import challenge_attention_count


@override_settings(NORTHBOUND_PROMPT_RESERVATIONS_READY=True)
class PhotoReviewTests(TestCase):
    token=fixtures.PhotoFoundationTests.token
    prompt=fixtures.PhotoFoundationTests.prompt
    settings=fixtures.PhotoFoundationTests.settings
    create=fixtures.PhotoFoundationTests.create
    protected=fixtures.PhotoFoundationTests.protected

    def setUp(self):
        fixtures.PhotoFoundationTests.setUp(self)
        self.settings(kind='scavenger',enabled=True,completion_bonus=100)
        self.client.force_login(self.actor)

    def review(self,photo,actor=None,decision='approved',verified=True,version=None):
        actor=actor or self.actor
        return review_photo_submission(photo_id=photo.pk,actor=actor,decision=decision,
            reviewer_verified=verified,version=version or photo_review_version(photo,actor))

    def url(self,photo):return reverse('photo-review',args=[self.group.slug,self.month.pk,photo.pk])
    def score(self):return challenge_score_totals(month=self.month,participant_ids=[self.reader.pk])[self.reader.pk]

    def test_verified_approval_without_link_atomic_receipt_and_date(self):
        photo=self.create();before=list(BookSubmission.objects.values())
        photo=self.review(photo)
        self.assertEqual(photo.status,'approved');self.assertTrue(photo.reviewer_verified)
        self.assertEqual(photo.reviewed_by,self.actor);self.assertIsNotNone(photo.reviewed_at)
        claim=photo.prompt_claim;self.assertEqual(claim.state,'qualified');self.assertTrue(claim.earning_evidence)
        row=ModifierProvenance.objects.get(source_reference=f'prompt_claim:{claim.pk}')
        self.assertEqual(row.source_type,'scavenger_prompt');self.assertIsNone(row.submission_id)
        self.assertEqual(row.amount,25);self.assertEqual(str(row.effective_date),claim.earning_evidence['effective_date'])
        self.assertEqual(row.applied_by,self.actor);self.assertEqual(self.score()['modifier_pages'],25)
        self.assertEqual(list(BookSubmission.objects.values()),before)
        event=AuditEvent.objects.get(action='scavenger_photo.reviewed');self.assertEqual(event.actor,self.actor)
        self.assertIn('"reviewer_verified": true',event.summary)

    def test_reader_confirmation_and_discord_link_do_not_verify(self):
        photo=self.create(discord_message_url='https://discord.com/channels/1/2/3')
        for flag in [False,None,'on',1]:
            with self.assertRaises(ValidationError):self.review(photo,verified=flag)
        photo.refresh_from_db();self.assertEqual(photo.status,'pending');self.assertFalse(ModifierProvenance.objects.exists())
        self.assertNotIn('discord.com',''.join(AuditEvent.objects.values_list('summary',flat=True)))

    def test_rejection_attributes_reviewer_and_releases_for_new_attempt(self):
        photo=self.create();photo=self.review(photo,decision='rejected',verified=False)
        self.assertEqual(photo.status,'rejected');self.assertFalse(photo.reviewer_verified)
        self.assertEqual(photo.reviewed_by,self.actor);self.assertEqual(photo.prompt_claim.state,'rejected')
        self.assertEqual(ModifierProvenance.objects.count(),0)
        retry=self.create();self.assertNotEqual(retry.pk,photo.pk)
        self.assertEqual(ScavengerPhotoSubmission.objects.count(),2)

    def test_authorization_matrix_and_self_approval_parity(self):
        for role in ['owner','moderator','reader','outsider']:
            photo=self.create()
            with self.assertRaises(PermissionDenied):self.review(photo,actor=self.users[role])
            transition_photo_reservation(photo_id=photo.pk,actor=self.reader.user,version=photo.version,status='withdrawn')
        for role in ['root','host','floater','leader','participating']:
            photo=self.create();self.assertEqual(self.review(photo,actor=self.users[role]).status,'approved',role)
            # Fresh Reader per role is unnecessary: use the other Photo prompt after clearing fixture rows only.
            ScavengerPhotoSubmission.objects.filter(pk=photo.pk).update(status='voided',removed_at=timezone.now())
            PromptClaim.objects.filter(photo_submission=photo).update(state='released')
            ModifierProvenance.objects.all().delete()
        for role in ['leader','participating']:
            own=create_photo_submission(month_id=self.month.pk,actor=self.users[role],prompt_id=self.photo_prompt.pk,reader_confirmed=True)
            self.assertEqual(self.review(own,actor=self.users[role]).reviewed_by,self.users[role])

    def test_team_scope_queue_and_direct_review(self):
        photo=self.create(discord_message_url='https://discord.com/channels/111/222/333')
        from .participation import assign_participant_to_team
        assign_participant_to_team(month=self.month,participant=self.reader,team=self.other_team,actor=self.actor)
        for role in ['leader','participating']:
            self.assertFalse(pending_review_photos(self.users[role],self.month).exists())
            self.client.force_login(self.users[role]);response=self.client.get(self.url(photo))
            self.assertEqual(response.status_code,404);self.assertNotIn(photo.discord_message_url.encode(),response.content)
            with self.assertRaises(PermissionDenied):self.review(photo,actor=self.users[role])
        Membership.objects.filter(pk=self.members['participating'].pk).update(role='owner')
        with self.assertRaises(PermissionDenied):self.review(photo,actor=self.users['participating'])

    def test_transfer_preserves_original_acceptance_and_existing_team_rule(self):
        photo=self.create();evidence=photo.acceptance_evidence.copy()
        from .participation import assign_participant_to_team
        assign_participant_to_team(month=self.month,participant=self.reader,team=self.other_team,actor=self.actor)
        photo.refresh_from_db();photo=self.review(photo)
        self.assertEqual(photo.acceptance_evidence,evidence)
        self.assertEqual(photo.prompt_claim.earning_evidence['acceptance']['team_id'],self.team.pk)
        totals=challenge_team_totals(month=self.month,team_ids=[self.team.pk,self.other_team.pk])
        self.assertEqual(totals,{self.team.pk:0,self.other_team.pk:393})

    def test_missing_or_contradictory_acceptance_team_denied(self):
        photo=self.create()
        ScavengerPhotoSubmission.objects.filter(pk=photo.pk).update(accepted_team_assignment=None)
        photo.refresh_from_db()
        with self.assertRaises(ValidationError):self.review(photo)
        self.assertEqual(ModifierProvenance.objects.count(),0)

    def test_timezone_submission_date_not_approval_date(self):
        from .models import ParticipationPeriod
        self.group.timezone='America/Los_Angeles';self.group.save()
        accepted=datetime(2026,11,13,1,30,tzinfo=dt_timezone.utc)
        TeamAssignment.objects.filter(month=self.month,participant=self.reader).update(assigned_at=accepted-timezone.timedelta(days=1))
        with patch('core.photo_submissions.timezone.now',return_value=accepted):photo=self.create()
        self.review(photo)
        row=ModifierProvenance.objects.get(source_type='scavenger_prompt')
        self.assertEqual(str(row.effective_date),'2026-11-12')

    def test_photo_only_completion_once_and_frozen_points(self):
        # Configure all required prompts before the first activity freezes the set.
        self.book_prompt.submission_type='photo';self.book_prompt.save()
        first=self.create();second=create_photo_submission(month_id=self.month.pk,actor=self.reader.user,prompt_id=self.book_prompt.pk,reader_confirmed=True)
        self.review(first);second.refresh_from_db();self.review(second)
        award=PromptCompletion.objects.get();self.assertTrue(award.is_active);self.assertEqual(award.bonus_amount_snapshot,100)
        self.assertEqual(ModifierProvenance.objects.filter(source_type='scavenger_completion').count(),1)
        self.assertEqual(self.score()['modifier_pages'],150)
        first.refresh_from_db();count=ModifierProvenance.objects.count()
        self.review(first);self.assertEqual(ModifierProvenance.objects.count(),count)
        self.photo_prompt.points=500;self.photo_prompt.save()
        first.refresh_from_db();self.review(first)
        self.assertEqual(self.score()['modifier_pages'],150)

    def test_mixed_completion_photo_last_and_book_last(self):
        replace_selections(submission=self.book,actor=self.actor,selections={'scavenger':self.book_prompt.pk})
        photo=self.create()
        review_prompts(self.book,self.actor,{'scavenger':'qualified'})
        self.assertFalse(PromptCompletion.objects.exists())
        self.review(photo)
        self.assertTrue(PromptCompletion.objects.get().is_active)
        self.assertEqual(self.score()['modifier_pages'],150)

    def test_mixed_completion_book_last_uses_same_award(self):
        photo=self.create();self.review(photo)
        replace_selections(submission=self.book,actor=self.actor,selections={'scavenger':self.book_prompt.pk})
        review_prompts(self.book,self.actor,{'scavenger':'qualified'})
        self.assertEqual(PromptCompletion.objects.count(),1);self.assertEqual(self.score()['modifier_pages'],150)
        review_prompts(self.book,self.actor,{'scavenger':'qualified'})
        self.assertEqual(PromptCompletion.objects.count(),1)

    def test_pending_and_rejected_do_not_complete(self):
        replace_selections(submission=self.book,actor=self.actor,selections={'scavenger':self.book_prompt.pk})
        review_prompts(self.book,self.actor,{'scavenger':'qualified'})
        photo=self.create();self.assertFalse(PromptCompletion.objects.exists())
        self.review(photo,decision='rejected',verified=False)
        self.assertFalse(PromptCompletion.objects.exists())

    def test_zero_points_qualification_and_zero_completion(self):
        self.photo_prompt.points=0;self.photo_prompt.save()
        self.book_prompt.points=0;self.book_prompt.submission_type='photo';self.book_prompt.save()
        self.settings(kind='scavenger',enabled=True,completion_bonus=0)
        first=self.create();second=create_photo_submission(month_id=self.month.pk,actor=self.reader.user,prompt_id=self.book_prompt.pk,reader_confirmed=True)
        self.review(first);self.review(second)
        self.assertEqual(PromptClaim.objects.filter(state='qualified').count(),2)
        self.assertTrue(PromptCompletion.objects.get().is_active)
        self.assertFalse(ModifierProvenance.objects.exists())

    def test_old_version_repeated_approval_and_opposing_decision_fail_safely(self):
        photo=self.create();version=photo_review_version(photo,self.actor)
        self.review(photo,version=version)
        for decision in ['approved','rejected']:
            with self.assertRaises(ValidationError):self.review(photo,decision=decision,version=version)
        photo.refresh_from_db();self.assertEqual(photo.status,'approved')
        self.assertEqual(ModifierProvenance.objects.count(),1)
        with self.assertRaises(ValidationError):self.review(photo,decision='rejected')

    def test_pending_edit_and_withdrawal_invalidate_review(self):
        photo=self.create();version=photo_review_version(photo,self.actor)
        edit_pending_photo(photo_id=photo.pk,actor=self.reader.user,version=photo.version,prompt_id=self.photo_prompt.pk,discord_message_url='https://discord.com/channels/1/2/3')
        with self.assertRaises(ValidationError):self.review(photo,version=version)
        photo.refresh_from_db();version=photo_review_version(photo,self.actor)
        transition_photo_reservation(photo_id=photo.pk,actor=self.reader.user,version=photo.version,status='withdrawn')
        with self.assertRaises(ValidationError):self.review(photo,version=version)
        self.assertFalse(ModifierProvenance.objects.exists())

    def test_approval_blocks_later_reader_edit_or_withdrawal(self):
        photo=self.create();self.review(photo)
        with self.assertRaises(PermissionDenied):edit_pending_photo(photo_id=photo.pk,actor=self.reader.user,version=photo.version,prompt_id=self.photo_prompt.pk)
        photo.refresh_from_db()
        with self.assertRaises(ValidationError):transition_photo_reservation(photo_id=photo.pk,actor=self.reader.user,version=photo.version,status='withdrawn')

    def test_rollback_after_source_or_completion_failure(self):
        photo=self.create();before=(AuditEvent.objects.count(),self.protected())
        with patch('core.prompt_scoring._completion',side_effect=ValidationError('synthetic')):
            with self.assertRaises(ValidationError):self.review(photo)
        photo.refresh_from_db();self.assertEqual(photo.status,'pending');self.assertEqual(photo.version,1)
        self.assertEqual(photo.prompt_claim.state,'reserved');self.assertFalse(ModifierProvenance.objects.exists())
        self.assertEqual((AuditEvent.objects.count(),self.protected()),before)

    def test_state_config_and_actor_bound_signed_review(self):
        photo=self.create();version=photo_review_version(photo,self.actor)
        for bad in [version+'tampered',photo_review_version(photo,self.users['root'])]:
            with self.assertRaises(ValidationError):self.review(photo,version=bad)
        self.photo_prompt.points=35;self.photo_prompt.save()
        with self.assertRaises(ValidationError):self.review(photo,version=version)
        self.assertFalse(ModifierProvenance.objects.exists())

    def test_closed_gate_category_exclusion_and_lifecycle(self):
        photo=self.create()
        with override_settings(NORTHBOUND_PROMPT_RESERVATIONS_READY=False):
            with self.assertRaises(ValidationError):self.review(photo)
            self.assertFalse(pending_review_photos(self.actor,self.month).exists())
        for stage in ['draft','upcoming','completed','archived']:
            ChallengeMonth.objects.filter(pk=self.month.pk).update(status=stage);photo.month.refresh_from_db()
            with self.assertRaises(PermissionDenied):self.review(photo)
        ChallengeMonth.objects.filter(pk=self.month.pk).update(status='finalizing');photo.month.refresh_from_db()
        self.assertEqual(self.review(photo).status,'approved')

    def test_disabled_category_and_november_no_award(self):
        photo=self.create();category=self.photo_prompt.category;category.refresh_from_db();category.enabled=False;category.save()
        with self.assertRaises(ValidationError):self.review(photo)
        category.enabled=True;category.save()
        ChallengeMonth.objects.filter(pk=self.month.pk).update(prompt_automation_excluded=True);photo.month.refresh_from_db()
        with self.assertRaises(ValidationError):self.review(photo)
        self.assertFalse(ModifierProvenance.objects.exists())

    def test_queue_attention_and_form_verification(self):
        photo=self.create(discord_message_url='https://discord.com/channels/111/222/333')
        baseline=challenge_attention_count(self.actor,self.month)
        response=self.client.get(reverse('review-queue',args=[self.group.slug,self.month.pk]))
        self.assertContains(response,'Review Photo');self.assertContains(response,'SH')
        self.assertContains(response,photo.discord_message_url)
        response=self.client.get(self.url(photo));self.assertContains(response,'Required verification')
        self.assertContains(response,'I have verified this photo in the designated Discord channel.')
        token=response.context['form'].initial['version']
        response=self.client.post(self.url(photo),{'version':token,'decision':'approved'})
        self.assertEqual(response.status_code,200);self.assertTrue(response.context['form'].errors)
        response=self.client.post(self.url(photo),{'version':token,'decision':'approved','reviewer_verified':'on'})
        self.assertRedirects(response,reverse('review-queue',args=[self.group.slug,self.month.pk]))
        self.assertEqual(challenge_attention_count(self.actor,self.month),baseline-1)

    def test_reading_statistics_and_book_covers_remain_book_only(self):
        from .recaps import build_recap
        before=build_recap(viewer=self.reader.user,perspective='reader',group_id=self.group.pk,challenge_id=self.month.pk)
        photo=self.create();self.review(photo)
        after=build_recap(viewer=self.reader.user,perspective='reader',group_id=self.group.pk,challenge_id=self.month.pk)
        for result in [before,after]:
            cards={c['key']:c['value'] for section in result['sections'] for c in section['cards']}
            if result is before:original=cards
            else:
                for key in ['entries','pages','format_ebook']:
                    self.assertIn(key,original);self.assertEqual(cards[key],original[key],key)
        self.assertEqual(BookSubmission.objects.count(),1)
        from .recaps import _book_highlights
        self.assertEqual(_book_highlights(self.reader.user,self.month,[self.reader.pk],(None,None)),[{'title':self.book.title,'author':self.book.author,'cover_url':''}])

    def test_mixed_completion_existing_book_void_and_restore_preserve_photo_credit(self):
        photo=self.create();self.review(photo)
        replace_selections(submission=self.book,actor=self.actor,selections={'scavenger':self.book_prompt.pk})
        review_prompts(self.book,self.actor,{'scavenger':'qualified'})
        photo_receipt=ModifierProvenance.objects.filter(source_reference=f'prompt_claim:{photo.prompt_claim.pk}').values().get()
        self.book.refresh_from_db();self.book.is_removed=True;self.book.save()
        self.assertFalse(PromptCompletion.objects.get().is_active)
        self.assertEqual(ModifierProvenance.objects.filter(pk=photo_receipt['id']).values().get(),photo_receipt)
        self.book.is_removed=False;self.book.save()
        self.assertTrue(PromptCompletion.objects.get().is_active)
        self.assertEqual(PromptCompletion.objects.count(),1)

    def test_scoring_helpers_reject_missing_actor_and_modified_receipt(self):
        photo=self.create();photo=self.review(photo);claim=photo.prompt_claim
        from .prompt_scoring import _source
        with self.assertRaises(PermissionDenied):_source(claim,True,None)
        claim.earning_evidence={**claim.earning_evidence,'amount':999}
        with self.assertRaises(ValidationError):_source(claim,True,self.actor)
        self.assertEqual(ModifierProvenance.objects.get().amount,25)

    def test_departed_reader_valid_evidence_uses_captured_points_and_credited_team(self):
        photo=self.create()
        from .participation import deactivate_participation
        enrollment=MonthEnrollment.objects.get(month=self.month,participant=self.reader)
        deactivate_participation(enrollment=enrollment,actor=self.actor,reason='removed')
        self.photo_prompt.points=500;self.photo_prompt.save()
        photo.refresh_from_db();self.review(photo)
        self.assertEqual(ModifierProvenance.objects.get().amount,25)
        self.assertEqual(challenge_team_totals(month=self.month,team_ids=[self.team.pk])[self.team.pk],393)

    def test_direct_model_decisions_cannot_skip_review_service(self):
        photo=self.create()
        for status in ['approved','rejected']:
            photo.refresh_from_db();photo.status=status
            photo.reviewer_verified=status=='approved';photo.reviewed_by=self.actor;photo.reviewed_at=timezone.now()
            with self.assertRaises(ValidationError):photo.save()
        photo.refresh_from_db();self.assertEqual(photo.status,'pending')
        self.assertEqual(photo.prompt_claim.state,'reserved');self.assertFalse(ModifierProvenance.objects.exists())

    def test_optional_browser_fixtures(self):
        directory=os.environ.get('NB_PHOTO_C_FIXTURES')
        if not directory:return
        root=Path(directory);root.mkdir(parents=True,exist_ok=True)
        self.photo_prompt.description='Photograph your favorite seasonal reading space with an exceptionally long prompt description '*4
        self.photo_prompt.save();photo=self.create(discord_message_url='https://discord.com/channels/111/222/333')
        for name,url in [('review',self.url(photo)),('queue',reverse('review-queue',args=[self.group.slug,self.month.pk]))]:
            response=self.client.get(url);self.assertEqual(response.status_code,200)
            (root/(name+'.html')).write_bytes(response.content)
