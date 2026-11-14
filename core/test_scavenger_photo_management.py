import os
from pathlib import Path
from unittest.mock import patch
from django.core.exceptions import PermissionDenied,ValidationError
from django.test import TestCase,override_settings,RequestFactory
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.urls import reverse
from . import test_scavenger_photo_review as fixtures
from .models import (BookSubmission,ScavengerPhotoSubmission,PromptClaim,PromptCompletion,ModifierProvenance,
    AuditEvent,ChallengeMonth,Membership,MonthEnrollment)
from .photo_submissions import create_photo_submission
from .photo_actions import correct_photo,correction_preview,management_version,change_photo_credit
from .photo_submission_table import submission_page
from .score_aggregation import challenge_team_totals


@override_settings(NORTHBOUND_PROMPT_RESERVATIONS_READY=True)
class PhotoManagementTests(TestCase):
    token=fixtures.PhotoReviewTests.token
    prompt=fixtures.PhotoReviewTests.prompt
    settings=fixtures.PhotoReviewTests.settings
    create=fixtures.PhotoReviewTests.create
    protected=fixtures.PhotoReviewTests.protected
    review=fixtures.PhotoReviewTests.review
    score=fixtures.PhotoReviewTests.score
    setUp=fixtures.PhotoReviewTests.setUp

    def correct(self,photo,actor=None,prompt_id=None,url='',version=None):
        actor=actor or self.actor
        return correct_photo(photo_id=photo.pk,actor=actor,version=version or management_version(photo,actor,'correct'),
            prompt_id=prompt_id or photo.prompt_claim.prompt_id,discord_message_url=url)

    def credit(self,photo,action,actor=None,version=None):
        actor=actor or self.actor
        return change_photo_credit(photo_id=photo.pk,actor=actor,version=version or management_version(photo,actor,action),action=action,reason='Scoped correction')

    def route(self,name,photo,*extra):return reverse(name,args=[self.group.slug,self.month.pk,photo.pk,*extra])

    def complete(self):
        self.book_prompt.submission_type='photo';self.book_prompt.save()
        first=self.create();second=create_photo_submission(month_id=self.month.pk,actor=self.reader.user,prompt_id=self.book_prompt.pk,reader_confirmed=True)
        first=self.review(first);second=self.review(second)
        return first,second

    def test_link_only_staff_correction_preserves_frozen_awards_and_books(self):
        photo=self.review(self.create());before=(self.protected(),list(ModifierProvenance.objects.values()),photo.prompt_claim.earning_evidence.copy())
        self.photo_prompt.points=900;self.photo_prompt.save()
        plan=correction_preview(photo,self.users['owner'],prompt_id=self.photo_prompt.pk)
        self.assertEqual(plan['net'],0);self.assertEqual(plan['new_points'],25);self.assertEqual(plan['configured'],900)
        photo=self.correct(photo,actor=self.users['owner'],url='https://discord.com/channels/111/222/333')
        self.assertEqual(photo.discord_message_url,'https://discord.com/channels/111/222/333')
        self.assertEqual((self.protected(),list(ModifierProvenance.objects.values()),photo.prompt_claim.earning_evidence),before)
        self.assertNotIn('discord.com',AuditEvent.objects.get(action='scavenger_photo.corrected').summary)

    def test_pending_staff_prompt_change_and_conflict(self):
        third=self.prompt(3,kind='scavenger',submission_type='photo')
        photo=self.create()
        photo=self.correct(photo,actor=self.users['owner'],prompt_id=third.pk)
        self.assertEqual(photo.prompt_claim.prompt_id,third.pk)
        other=self.create()
        with self.assertRaises(ValidationError):self.correct(photo,prompt_id=self.photo_prompt.pk)
        photo.refresh_from_db();self.assertEqual(photo.prompt_claim.prompt_id,third.pk)
        self.assertFalse(ModifierProvenance.objects.exists())

    def test_approved_identity_requires_preview_and_can_be_confirmed(self):
        third=self.prompt(3,kind='scavenger',submission_type='photo',points=99)
        photo=self.review(self.create())
        preview=correction_preview(photo,self.actor,prompt_id=third.pk)
        self.assertFalse(preview['blocked']);self.assertEqual(preview['net'],74)
        with self.assertRaises(ValidationError):self.correct(photo,prompt_id=third.pk)
        version=management_version(photo,self.actor,'correct')
        response=self.client.post(self.route('photo-correct',photo),{'action':'preview','version':version,'prompt':third.pk,'discord_message_url':''})
        self.assertContains(response,'Confirm Correction')
        response=self.client.post(self.route('photo-correct',photo),{'action':'save','version':version,'prompt':third.pk,'discord_message_url':'','preview_token':preview['token']})
        self.assertEqual(response.status_code,302)
        photo.refresh_from_db();self.assertEqual(photo.prompt_claim.prompt_id,third.pk)
        self.assertEqual(self.score()['modifier_pages'],99)

    def apply_preview(self,photo,destination,actor=None,preview=None):
        actor=actor or self.actor
        preview=preview if preview is not None else correction_preview(photo,actor,prompt_id=destination.pk)['token']
        return correct_photo(photo_id=photo.pk,actor=actor,version=management_version(photo,actor,'correct'),
            prompt_id=destination.pk,preview=preview)

    def test_correction_higher_lower_equal_frozen_history_and_latest_void_restore(self):
        third=self.prompt(3,kind='scavenger',submission_type='photo',points=99)
        photo=self.review(self.create());photo_id=photo.pk;claim_id=photo.prompt_claim.pk
        original=(photo.submitted_at,photo.reviewed_by_id,photo.reviewed_at,photo.acceptance_evidence,list(BookSubmission.objects.values()))
        for amount in [99,10,10]:
            destination=third if photo.prompt_claim.prompt_id==self.photo_prompt.pk else self.photo_prompt
            destination.points=amount;destination.save()
            photo=self.apply_preview(photo,destination,actor=self.users['owner'])
            self.assertEqual((photo.pk,photo.prompt_claim.pk),(photo_id,claim_id))
            self.assertEqual((photo.submitted_at,photo.reviewed_by_id,photo.reviewed_at,photo.acceptance_evidence,list(BookSubmission.objects.values())),original)
            self.assertEqual(self.score()['modifier_pages'],amount)
            rows=ModifierProvenance.objects.filter(source_type='scavenger_prompt')
            self.assertEqual(rows.filter(is_active=True).count(),1)
            self.assertEqual(rows.get(is_active=True).effective_date,photo.submitted_at.date())
            self.assertFalse(PromptCompletion.objects.exists())
        self.assertEqual(len(photo.prompt_claim.earning_evidence['history']),3)
        self.assertEqual(AuditEvent.objects.filter(action='scavenger_photo.prompt_corrected').count(),3)
        last=AuditEvent.objects.filter(action='scavenger_photo.prompt_corrected').latest('pk')
        self.assertEqual(last.actor,self.users['owner']);self.assertIn('"impact": 0',last.summary)
        destination.points=777;destination.save()
        self.assertEqual(self.score()['modifier_pages'],10)
        photo=self.credit(photo,'void');self.assertEqual(self.score()['modifier_pages'],0)
        photo=self.credit(photo,'restore');self.assertEqual(self.score()['modifier_pages'],10)
        self.assertEqual(rows.filter(is_active=True).count(),1)
        self.assertFalse(rows.get(source_reference=f'prompt_claim:{claim_id}').is_active)
        from .participation import assign_participant_to_team
        assign_participant_to_team(month=self.month,participant=self.reader,team=self.other_team,actor=self.actor)
        self.assertEqual(challenge_team_totals(month=self.month,team_ids=[self.team.pk,self.other_team.pk]),{self.team.pk:0,self.other_team.pk:378})

    def test_correction_preview_tamper_stale_destination_and_atomic_rollback(self):
        third=self.prompt(3,kind='scavenger',submission_type='photo',points=99)
        photo=self.review(self.create());preview=correction_preview(photo,self.actor,prompt_id=third.pk)['token']
        before=(list(ModifierProvenance.objects.values()),list(PromptClaim.objects.values()),AuditEvent.objects.count(),photo.version)
        for token in ['',preview+'tampered']:
            with self.assertRaises(ValidationError):self.apply_preview(photo,third,preview=token)
        third.points=101;third.save()
        with self.assertRaises(ValidationError):self.apply_preview(photo,third,preview=preview)
        preview=correction_preview(photo,self.actor,prompt_id=third.pk)['token']
        with patch('core.prompt_scoring._completion',side_effect=ValidationError('synthetic failure')):
            with self.assertRaises(ValidationError):self.apply_preview(photo,third,preview=preview)
        photo.refresh_from_db()
        self.assertEqual((list(ModifierProvenance.objects.values()),list(PromptClaim.objects.values()),AuditEvent.objects.count(),photo.version),before)

    def test_complete_reader_conflict_preserves_frozen_bonus_and_no_historical_repair(self):
        photo,other=self.complete();before=(self.score(),list(ModifierProvenance.objects.values()),list(PromptCompletion.objects.values()))
        plan=correction_preview(photo,self.actor,prompt_id=other.prompt_claim.prompt_id)
        self.assertTrue(plan['blocked'])
        with self.assertRaises(ValidationError):self.apply_preview(photo,other.prompt_claim.prompt,preview=plan['token'])
        self.assertEqual((self.score(),list(ModifierProvenance.objects.values()),list(PromptCompletion.objects.values())),before)
        self.assertEqual(plan['bonus_before'],100)
        # A historically inconsistent completion must stop, not silently repair.
        PromptCompletion.objects.update(is_active=False)
        with self.assertRaisesMessage(ValidationError,'completion provenance'):
            correction_preview(photo,self.actor,prompt_id=other.prompt_claim.prompt_id)

    def test_approved_correction_conflict_scope_and_closed_gate(self):
        third=self.prompt(3,kind='scavenger',submission_type='photo')
        photo=self.review(self.create())
        create_photo_submission(month_id=self.month.pk,actor=self.reader.user,prompt_id=third.pk,reader_confirmed=True)
        plan=correction_preview(photo,self.actor,prompt_id=third.pk)
        self.assertIn('reservation',plan['blocked'])
        with self.assertRaises(ValidationError):self.apply_preview(photo,third,preview=plan['token'])
        with override_settings(NORTHBOUND_PROMPT_RESERVATIONS_READY=False):
            with self.assertRaises(ValidationError):correction_preview(photo,self.actor,prompt_id=third.pk)
        from .participation import assign_participant_to_team
        assign_participant_to_team(month=self.month,participant=self.reader,team=self.other_team,actor=self.actor)
        for role in ['participating','leader','moderator']:
            with self.assertRaises(PermissionDenied):correction_preview(photo,self.users[role],prompt_id=third.pk)

    def test_zero_point_destination_and_stale_changed_request(self):
        third=self.prompt(3,kind='scavenger',submission_type='photo',points=0)
        photo=self.review(self.create());plan=correction_preview(photo,self.actor,prompt_id=third.pk)
        with self.assertRaises(ValidationError):
            correct_photo(photo_id=photo.pk,actor=self.actor,version=management_version(photo,self.actor,'correct'),
                prompt_id=third.pk,discord_message_url='https://discord.com/channels/1/2/3',preview=plan['token'])
        photo=self.apply_preview(photo,third,preview=plan['token'])
        self.assertEqual(self.score()['modifier_pages'],0)
        self.assertFalse(ModifierProvenance.objects.filter(is_active=True).exists())
        photo=self.credit(photo,'void');photo=self.credit(photo,'restore')
        self.assertEqual(self.score()['modifier_pages'],0)
        self.assertEqual(photo.prompt_claim.earning_evidence['amount'],0)

    def test_historical_completion_gain_and_loss_paths_stop_without_writes(self):
        from .models import PromptCategory
        from django.utils import timezone
        third=self.prompt(3,kind='scavenger',submission_type='photo',points=99)
        photo=self.review(self.create());category=photo.prompt_claim.category
        # Isolated legacy fixtures intentionally have an inconsistent frozen requirement set.
        # Ordinary configuration forbids these structural changes after first activity.
        for gaining in [True,False]:
            required=[{'id':third.pk if gaining else self.photo_prompt.pk}]
            PromptCategory.objects.filter(pk=category.pk).update(frozen_prompt_set=required)
            if not gaining:
                award=PromptCompletion.objects.create(category=PromptCategory.objects.get(pk=category.pk),participant=self.reader,
                    frozen_prompt_set=required,bonus_amount_snapshot=100,is_active=True,qualified_at=timezone.now(),effective_date=photo.submitted_at.date())
                ModifierProvenance.objects.create(month=self.month,participant=self.reader,source_type='scavenger_completion',
                    source_reference=f'prompt_completion:{award.pk}',amount=100,effective_date=award.effective_date,is_active=True)
            photo.refresh_from_db()
            before=(list(ModifierProvenance.objects.values()),list(PromptCompletion.objects.values()),list(PromptClaim.objects.values()),AuditEvent.objects.count())
            plan=correction_preview(photo,self.actor,prompt_id=third.pk)
            self.assertIn('frozen required set',plan['blocked'])
            self.assertEqual(plan['bonus_delta'],100 if gaining else -100)
            with self.assertRaises(ValidationError):self.apply_preview(photo,third,preview=plan['token'])
            self.assertEqual((list(ModifierProvenance.objects.values()),list(PromptCompletion.objects.values()),list(PromptClaim.objects.values()),AuditEvent.objects.count()),before)

    def test_void_reverses_receipt_and_completion_once_and_restore_frozen(self):
        photo,_=self.complete();before_books=list(BookSubmission.objects.values());original=photo.prompt_claim.earning_evidence.copy()
        self.assertEqual(self.score()['modifier_pages'],150)
        photo=self.credit(photo,'void');self.assertEqual(photo.status,'voided');self.assertEqual(photo.prompt_claim.state,'released')
        self.assertEqual(self.score()['modifier_pages'],25);self.assertFalse(PromptCompletion.objects.get().is_active)
        count=AuditEvent.objects.count();self.credit(photo,'void');self.assertEqual(AuditEvent.objects.count(),count)
        self.photo_prompt.points=999;self.photo_prompt.save()
        category=self.photo_prompt.category;category.refresh_from_db();category.completion_bonus=888;category.save()
        photo=self.credit(photo,'restore')
        self.assertEqual(photo.status,'approved');self.assertEqual(photo.prompt_claim.earning_evidence,original)
        self.assertEqual(self.score()['modifier_pages'],150);self.assertEqual(PromptCompletion.objects.count(),1)
        count=ModifierProvenance.objects.count();self.credit(photo,'restore');self.assertEqual(ModifierProvenance.objects.count(),count)
        self.assertEqual(list(BookSubmission.objects.values()),before_books)

    def test_restore_conflict_missing_receipt_and_missing_evidence_fail_closed(self):
        photo=self.review(self.create());photo=self.credit(photo,'void')
        replacement=self.create()
        with self.assertRaises(ValidationError):self.credit(photo,'restore')
        self.assertEqual(self.score()['modifier_pages'],0)
        self.assertEqual(ScavengerPhotoSubmission.objects.get(pk=photo.pk).status,'voided')
        PromptClaim.objects.filter(photo_submission=replacement).update(state='released')
        ScavengerPhotoSubmission.objects.filter(pk=replacement.pk).update(status='withdrawn',removed_at=photo.removed_at)
        ScavengerPhotoSubmission.objects.filter(pk=photo.pk).update(accepted_team_assignment=None)
        photo.refresh_from_db()
        with self.assertRaises(ValidationError):self.credit(photo,'restore')
        self.assertFalse(ModifierProvenance.objects.get().is_active)

    def test_missing_provenance_blocks_void_without_partial_changes(self):
        photo=self.review(self.create());ModifierProvenance.objects.all().delete()
        with self.assertRaises(ValidationError):self.credit(photo,'void')
        photo.refresh_from_db();self.assertEqual(photo.status,'approved');self.assertEqual(photo.prompt_claim.state,'qualified')

    def test_stale_tampered_confirmation_and_transaction_rollback(self):
        photo=self.review(self.create());version=management_version(photo,self.actor,'void')
        photo=self.correct(photo,url='https://discord.com/channels/1/2/3')
        for value in [version,management_version(photo,self.actor,'void')+'tampered']:
            with self.assertRaises(ValidationError):self.credit(photo,'void',version=value)
        before=(photo.version,self.score(),AuditEvent.objects.count())
        with patch('core.prompt_scoring._completion',side_effect=ValidationError('synthetic')):
            with self.assertRaises(ValidationError):self.credit(photo,'void')
        photo.refresh_from_db();self.assertEqual((photo.version,self.score(),AuditEvent.objects.count()),before)

    def test_permissions_team_scope_and_no_new_moderator_correction(self):
        photo=self.review(self.create())
        for role in ['reader','moderator','outsider']:
            with self.assertRaises(PermissionDenied):self.correct(photo,actor=self.users[role],url='https://discord.com/channels/1/2/3')
        # Ordinary Book void authority includes Group Moderators; correction/report authority does not.
        photo=self.credit(photo,'void',actor=self.users['moderator'])
        photo=self.credit(photo,'restore',actor=self.users['moderator'])
        from .participation import assign_participant_to_team
        assign_participant_to_team(month=self.month,participant=self.reader,team=self.other_team,actor=self.actor)
        Membership.objects.filter(pk=self.members['participating'].pk).update(role='owner')
        for role in ['leader','participating']:
            with self.assertRaises(PermissionDenied):self.correct(photo,actor=self.users[role])
            with self.assertRaises(PermissionDenied):self.credit(photo,'void',actor=self.users[role])
            self.client.force_login(self.users[role]);self.assertEqual(self.client.get(self.route('photo-detail',photo)).status_code,404)

    def test_transfer_rule_and_lifecycle_gates(self):
        photo,_=self.complete()
        from .participation import assign_participant_to_team
        assign_participant_to_team(month=self.month,participant=self.reader,team=self.other_team,actor=self.actor)
        photo.refresh_from_db();self.credit(photo,'void');photo.refresh_from_db();self.credit(photo,'restore')
        self.assertEqual(challenge_team_totals(month=self.month,team_ids=[self.team.pk,self.other_team.pk]),{self.team.pk:0,self.other_team.pk:518})
        for state in ['finalizing','completed','archived']:
            ChallengeMonth.objects.filter(pk=self.month.pk).update(status=state);photo.month.refresh_from_db()
            with self.assertRaises(PermissionDenied):self.credit(photo,'void')

    def test_mixed_progress_and_reports_history_when_disabled(self):
        photo=self.review(self.create(discord_message_url='https://discord.com/channels/111/222/333'))
        from .prompt_reservations import replace_selections
        from .prompt_scoring import review_prompts
        replace_selections(submission=self.book,actor=self.actor,selections={'scavenger':self.book_prompt.pk})
        review_prompts(self.book,self.actor,{'scavenger':'qualified'})
        report=reverse('prompt-progress-report',args=[self.group.slug,self.month.pk])
        response=self.client.get(report,{'category':'scavenger','completion':'complete'})
        self.assertContains(response,'2 / 2 completed');self.assertContains(response,'SH');self.assertContains(response,'BOOK')
        self.assertNotContains(response,photo.discord_message_url)
        self.assertContains(response,self.route('photo-detail',photo))
        category=self.photo_prompt.category;category.refresh_from_db();category.enabled=False;category.save()
        ChallengeMonth.objects.filter(pk=self.month.pk).update(status='completed')
        with override_settings(NORTHBOUND_PROMPT_RESERVATIONS_READY=False):
            response=self.client.get(report);self.assertContains(response,'2 / 2 completed');self.assertNotContains(response,'Review Photo')
            response=self.client.get(self.route('photo-detail',photo));self.assertContains(response,'Photo Activity History');self.assertNotContains(response,'Correct Photo')
            self.client.force_login(self.reader.user)
            self.assertContains(self.client.get(reverse('prompt-progress',args=[self.group.slug,self.month.pk])),'2 / 2 completed')
        self.client.force_login(self.users['moderator']);self.assertEqual(self.client.get(report).status_code,403)

    def test_progress_rejected_withdrawn_voided_states_and_reservation_priority(self):
        photo=self.create();photo=self.review(photo,decision='rejected',verified=False)
        self.client.force_login(self.reader.user)
        url=reverse('prompt-progress',args=[self.group.slug,self.month.pk])
        self.assertContains(self.client.get(url),'Rejected')
        photo=self.create();from .photo_submissions import transition_photo_reservation
        photo=transition_photo_reservation(photo_id=photo.pk,actor=self.reader.user,version=photo.version,status='withdrawn')
        self.assertContains(self.client.get(url),'Withdrawn')
        photo=self.review(self.create());photo=self.credit(photo,'void')
        self.assertContains(self.client.get(url),'Voided')
        self.create();self.assertContains(self.client.get(url),'Pending confirmation')

    def test_all_submissions_unified_filters_and_nonapplicable_pages(self):
        photo=self.review(self.create(discord_message_url='https://discord.com/channels/111/222/333'))
        from .prompt_reservations import replace_selections
        replace_selections(submission=self.book,actor=self.actor,selections={'scavenger':self.book_prompt.pk})
        self.client.force_login(self.users['root']);url=self.month.get_absolute_url()
        response=self.client.get(url);self.assertContains(response,'<span class="pill">BOOK</span>',html=True);self.assertContains(response,'<span class="pill">SH</span>',html=True)
        self.assertContains(response,'Not applicable — no book pages');self.assertEqual(response.context['submission_page'].paginator.count,2)
        response=self.client.get(url,{'submission_q':self.photo_prompt.description});self.assertEqual(response.context['submission_page'].paginator.count,1)
        photo=self.credit(photo,'void')
        response=self.client.get(url,{'submission_status':'voided'});self.assertEqual(response.context['submission_page'].paginator.count,1)
        self.assertContains(response,'Restore Photo')
        self.assertNotContains(response,'https://discord.com/')

    def test_union_pagination_uses_database_limit_and_no_duplicate_book_claim(self):
        photo=self.create()
        for i in range(28):BookSubmission.objects.create(month=self.month,participant=self.reader,title=f'Additional Book {i}',author='Author',book_format='ebook',completed_on='2027-01-03',submitted_pages=100)
        with CaptureQueriesContext(connection) as queries:page=submission_page(self.month,self.users['root'],query='',status='all',size=25,number=1)
        self.assertEqual(page.paginator.count,30);self.assertEqual(len(page.object_list),25)
        self.assertTrue(any('UNION' in q['sql'] and 'LIMIT 25' in q['sql'] for q in queries.captured_queries))
        page2=submission_page(self.month,self.users['root'],query='',status='all',size=25,number=2)
        self.assertEqual(len(page2.object_list),5)
        allids=[('photo' if getattr(row,'is_photo',False) else 'book',row.pk) for row in page.object_list+page2.object_list]
        self.assertEqual(len(set(allids)),30)

    def test_no_activation_bypass_or_november_mutation(self):
        photo=self.review(self.create())
        with override_settings(NORTHBOUND_PROMPT_RESERVATIONS_READY=False):
            with self.assertRaises(ValidationError):self.correct(photo,url='https://discord.com/channels/1/2/3')
            with self.assertRaises(ValidationError):self.credit(photo,'void')
        ChallengeMonth.objects.filter(pk=self.month.pk).update(prompt_automation_excluded=True);photo.month.refresh_from_db()
        with self.assertRaises(PermissionDenied):self.credit(photo,'void')
        with self.assertRaises(PermissionDenied):self.correct(photo)

    def test_completion_receipt_mismatch_blocks_void_atomically(self):
        photo,_=self.complete()
        ModifierProvenance.objects.filter(source_type='scavenger_completion').update(amount=999)
        before=self.score()
        with self.assertRaises(ValidationError):self.credit(photo,'void')
        photo.refresh_from_db();self.assertEqual(photo.status,'approved');self.assertEqual(self.score(),before)

    def test_withdrawn_table_filter_and_nonadministrator_table_denial(self):
        photo=self.create()
        from .photo_submissions import transition_photo_reservation
        transition_photo_reservation(photo_id=photo.pk,actor=self.reader.user,version=photo.version,status='withdrawn')
        page=submission_page(self.month,self.users['root'],query='',status='withdrawn',size=25,number=1)
        self.assertEqual(page.paginator.count,1);self.assertTrue(page.object_list[0].is_photo)
        with self.assertRaises(PermissionDenied):submission_page(self.month,self.actor,query='',status='all',size=25,number=1)

    def test_restore_does_not_invent_a_missing_historical_completion_award(self):
        self.book_prompt.submission_type='photo';self.book_prompt.save()
        photo=self.review(self.create());photo=self.credit(photo,'void')
        second=create_photo_submission(month_id=self.month.pk,actor=self.reader.user,prompt_id=self.book_prompt.pk,reader_confirmed=True)
        self.review(second)
        self.assertFalse(PromptCompletion.objects.exists())
        with self.assertRaisesMessage(ValidationError,'Historical completion has no frozen award evidence'):
            self.credit(photo,'restore')
        photo.refresh_from_db();self.assertEqual(photo.status,'voided');self.assertEqual(self.score()['modifier_pages'],25)

    def test_denied_staff_correction_does_not_fetch_private_discord_reference(self):
        photo=self.review(self.create(discord_message_url='https://discord.com/channels/111/222/333'))
        self.client.force_login(self.reader.user)
        with CaptureQueriesContext(connection) as queries:response=self.client.get(self.route('photo-correct',photo))
        self.assertEqual(response.status_code,403)
        self.assertFalse(any('discord_message_url' in query['sql'] for query in queries.captured_queries))
        self.assertNotContains(response,photo.discord_message_url,status_code=403)

    def test_optional_browser_fixtures(self):
        target=os.environ.get('NB_PHOTO_D_FIXTURES')
        if not target:return
        root=Path(target);root.mkdir(parents=True,exist_ok=True)
        self.photo_prompt.description='A long seasonal photograph prompt with reading spaces and decorations '*4;self.photo_prompt.save()
        third=self.prompt(3,kind='scavenger',submission_type='photo',description='Proposed alternate Photo prompt')
        photo=self.review(self.create())
        pages={'correction':self.route('photo-correct',photo),'void':self.route('photo-credit-action',photo,'void'),
               'report':reverse('prompt-progress-report',args=[self.group.slug,self.month.pk]),'progress':reverse('prompt-progress',args=[self.group.slug,self.month.pk])}
        preview=self.client.post(self.route('photo-correct',photo),{'action':'preview','version':management_version(photo,self.actor,'correct'),'prompt':third.pk,'discord_message_url':''})
        self.assertEqual(preview.status_code,200);(root/'preview.html').write_bytes(preview.content)
        for name,url in pages.items():
            if name=='progress':self.client.force_login(self.reader.user)
            response=self.client.get(url);self.assertEqual(response.status_code,200);(root/(name+'.html')).write_bytes(response.content)
        self.client.force_login(self.users['root']);response=self.client.get(self.month.get_absolute_url());self.assertEqual(response.status_code,200);(root/'table.html').write_bytes(response.content)
