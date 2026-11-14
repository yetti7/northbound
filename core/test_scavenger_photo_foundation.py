from unittest.mock import patch
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from .models import (AuditEvent, BookSubmission, ChallengeMonth, ChallengePrompt, ChallengeStaffAssignment,
    Membership, ModifierProvenance, MonthEnrollment, PromptClaim, PromptCompletion,
    ScavengerPhotoSubmission, TeamAssignment)
from .photo_validation import validate_discord_message_url
from .photo_submissions import (create_photo_submission, transition_photo_reservation,
    available_photo_prompts, can_edit_pending_photo, can_review_photo)
from .prompt_reservations import available_prompts, replace_selections
from .prompt_configuration import save_prompt
from . import test_prompt_foundations as fixtures


class DiscordPhotoLinkTests(SimpleTestCase):
    def test_optional_and_recognized_message_links(self):
        for url in ['', 'https://discord.com/channels/123/456/789',
                    'https://canary.discord.com/channels/1/2/3', 'https://discordapp.com/channels/1/2/3']:
            validate_discord_message_url(url)

    def test_unsafe_or_malformed_links(self):
        for url in ['http://discord.com/channels/1/2/3', 'javascript:alert(1)',
                    'https://example.com/channels/1/2/3', 'https://discord.com.evil.test/channels/1/2/3',
                    'https://user:password@discord.com/channels/1/2/3',
                    'https://discord.com:443/channels/1/2/3', 'https://discord.com/channels/@me/2/3',
                    'https://discord.com/channels/1/2/no', 'https://discord.com/channels/1/2/3?x=y',
                    'https://discord.com/channels/1/2/3#photo', ' https://discord.com/channels/1/2/3',
                    'https://discord.com/channels/1/2/3\n', 'https://discord.com/attachments/1/2/photo.png']:
            with self.subTest(url=url), self.assertRaises(ValidationError):
                validate_discord_message_url(url)


@override_settings(NORTHBOUND_PROMPT_RESERVATIONS_READY=True)
class PhotoFoundationTests(TestCase):
    token = fixtures.PromptFoundationTests.token
    prompt = fixtures.PromptFoundationTests.prompt
    settings = fixtures.PromptFoundationTests.settings
    protected = fixtures.PromptFoundationTests.protected

    def setUp(self):
        fixtures.PromptFoundationTests.setUp(self)
        self.reader = self.members['reader']
        self.photo_prompt = self.prompt(kind='scavenger', submission_type='photo')
        self.book_prompt = self.prompt(number=2, kind='scavenger')
        self.settings(kind='scavenger', enabled=True)

    def create(self, **values):
        return create_photo_submission(month_id=self.month.pk, actor=self.reader.user,
            prompt_id=self.photo_prompt.pk, reader_confirmed=True, **values)

    def transition(self, photo, status, actor=None, **values):
        return transition_photo_reservation(photo_id=photo.pk, actor=actor or self.actor,
            version=photo.version, status=status, **values)

    def test_pending_creation_evidence_and_no_scoring_side_effects(self):
        before = self.protected()
        photo = self.create()
        claim = PromptClaim.objects.get(photo_submission=photo)
        self.assertEqual(photo.status, 'pending'); self.assertTrue(photo.reader_confirmed)
        self.assertIsNone(claim.submission_id); self.assertEqual(claim.state, 'reserved')
        self.assertEqual(photo.participation_period.enrollment.participant_id, self.reader.pk)
        self.assertEqual(photo.accepted_team_assignment.team_id, self.team.pk)
        self.assertEqual(photo.acceptance_evidence['team_id'], self.team.pk)
        self.assertEqual(photo.submitted_at.isoformat(), photo.acceptance_evidence['accepted_at'])
        self.assertEqual(claim.selection_evidence['required_prompt_set'], claim.category.frozen_prompt_set)
        self.assertEqual(self.protected(), before)
        self.assertFalse(ModifierProvenance.objects.exists()); self.assertFalse(PromptCompletion.objects.exists())
        self.assertTrue(AuditEvent.objects.filter(action='scavenger_photo.created', actor=self.reader.user).exists())

    def test_confirmation_required_and_immutable(self):
        for value in [False, None, 'on', 1]:
            with self.assertRaises(ValidationError):
                create_photo_submission(month_id=self.month.pk, actor=self.reader.user,
                    prompt_id=self.photo_prompt.pk, reader_confirmed=value)
        photo = self.create(); photo.reader_confirmed = False
        with self.assertRaises(ValidationError): photo.save()
        photo.refresh_from_db(); self.assertTrue(photo.reader_confirmed)

    def test_url_optional_validated_and_not_copied_into_audit(self):
        photo = self.create(discord_message_url='https://discord.com/channels/1/2/3')
        self.assertEqual(photo.discord_message_url, 'https://discord.com/channels/1/2/3')
        self.assertNotIn('discord.com', AuditEvent.objects.filter(object_type='ScavengerPhotoSubmission').last().summary)
        photo.discord_message_url = 'https://example.com/image.png'
        with self.assertRaises(ValidationError): photo.save()

    def test_book_default_bingo_type_and_structural_type_lock(self):
        self.assertEqual(self.book_prompt.submission_type, 'book')
        with self.assertRaises(ValidationError): self.prompt(kind='bingo', submission_type='photo')
        self.create(); self.photo_prompt.refresh_from_db()
        with self.assertRaises(ValidationError):
            save_prompt(month_id=self.month.pk, kind='scavenger', actor=self.actor,
                version=self.token('scavenger'), prompt_id=self.photo_prompt.pk,
                values={'number':1,'description':'Changed','points':30,'is_active':True,'submission_type':'book'})
        self.photo_prompt.refresh_from_db(); self.assertEqual(self.photo_prompt.submission_type,'photo')

    def test_configuration_type_selection_before_activity(self):
        p = self.prompt(3, kind='scavenger', submission_type='photo')
        self.assertEqual(p.submission_type,'photo')
        p.submission_type='book';p.save();self.assertEqual(p.revision,2)

    def test_photo_prompts_absent_from_book_choices_and_direct_book_write_denied(self):
        self.assertEqual(list(available_prompts(self.book,'scavenger')), [self.book_prompt])
        with self.assertRaises(ValidationError):
            replace_selections(submission=self.book,actor=self.actor,selections={'scavenger':self.photo_prompt.pk})
        self.assertFalse(PromptClaim.objects.exists())

    def test_wrong_type_and_cross_challenge_selection_denied(self):
        for prompt_id in [self.book_prompt.pk, 999999]:
            with self.assertRaises(ValidationError):
                create_photo_submission(month_id=self.month.pk,actor=self.reader.user,prompt_id=prompt_id,reader_confirmed=True)
        other=ChallengeMonth.objects.create(group=self.group,name='Other',status='active')
        with self.assertRaises(ValidationError):
            create_photo_submission(month_id=other.pk,actor=self.reader.user,prompt_id=self.photo_prompt.pk,reader_confirmed=True)

    def test_claim_ownership_validation(self):
        photo=self.create();claim=photo.prompt_claim
        for field,value in [('participant',self.members['leader']),('prompt',self.book_prompt)]:
            claim.refresh_from_db();setattr(claim,field,value)
            with self.assertRaises(ValidationError):claim.save()
        other=self.prompt(1,kind='bingo')
        claim.refresh_from_db();claim.category=other.category;claim.prompt=other
        with self.assertRaises(ValidationError):claim.save()

    def test_exactly_one_target_database_constraint(self):
        photo=self.create()
        for targets in [{}, {'submission':self.book,'photo_submission':photo}]:
            with self.assertRaises(IntegrityError), transaction.atomic():
                PromptClaim.objects.bulk_create([PromptClaim(category=self.photo_prompt.category,
                    prompt=self.photo_prompt,participant=self.reader,**targets)])

    def test_photo_target_unique_even_after_release(self):
        photo=self.create();self.transition(photo,'withdrawn',actor=self.reader.user)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PromptClaim.objects.bulk_create([PromptClaim(category=self.photo_prompt.category,
                prompt=self.photo_prompt,participant=self.reader,photo_submission=photo,state='released')])

    def test_duplicate_reservation_rolls_back_new_photo_and_audit(self):
        self.create();before=(ScavengerPhotoSubmission.objects.count(),PromptClaim.objects.count(),AuditEvent.objects.count())
        with self.assertRaises(ValidationError):self.create()
        self.assertEqual((ScavengerPhotoSubmission.objects.count(),PromptClaim.objects.count(),AuditEvent.objects.count()),before)

    def test_shared_book_photo_reservation_constraint(self):
        self.create()
        # Deliberately bypass model type checks to verify the shared DB reservation key.
        with self.assertRaises(IntegrityError),transaction.atomic():
            PromptClaim.objects.bulk_create([PromptClaim(category=self.photo_prompt.category,prompt=self.photo_prompt,
                participant=self.reader,submission=self.book,state='reserved')])

    def test_rejection_withdrawal_release_reuse_and_restore_conflict(self):
        for state in ['rejected','withdrawn']:
            with self.subTest(state=state):
                photo=self.create()
                photo=self.transition(photo,state,actor=self.reader.user if state=='withdrawn' else self.actor)
                self.assertNotEqual(PromptClaim.objects.get(photo_submission=photo).state,'reserved')
                replacement=self.create()
                with self.assertRaises(ValidationError):self.transition(photo,'pending')
                photo.refresh_from_db();self.assertEqual(photo.status,state)
                self.assertEqual(PromptClaim.objects.get(photo_submission=replacement).state,'reserved')
                self.transition(replacement,'withdrawn',actor=self.reader.user)

    def test_restore_without_conflict_and_stale_request_denial(self):
        photo=self.create();old_version=photo.version
        photo=self.transition(photo,'withdrawn',actor=self.reader.user)
        with self.assertRaises(ValidationError):
            transition_photo_reservation(photo_id=photo.pk,actor=self.actor,version=old_version,status='pending')
        restored=self.transition(photo,'pending')
        self.assertEqual(restored.status,'pending');self.assertEqual(restored.prompt_claim.state,'reserved')

    def test_atomic_failure_leaves_no_photo_claim_or_audit(self):
        before=(AuditEvent.objects.count(),self.protected())
        with patch('core.photo_submissions._audit',side_effect=RuntimeError('synthetic failure')):
            with self.assertRaises(RuntimeError):self.create()
        self.assertFalse(ScavengerPhotoSubmission.objects.exists());self.assertFalse(PromptClaim.objects.exists())
        self.assertEqual((AuditEvent.objects.count(),self.protected()),before)
        self.photo_prompt.category.refresh_from_db();self.assertFalse(self.photo_prompt.category.structure_locked)

    def test_reader_ownership_pending_state_and_staff_scope(self):
        photo=self.create()
        self.assertTrue(can_edit_pending_photo(self.reader.user,photo))
        self.assertFalse(can_edit_pending_photo(self.users['leader'],photo))
        for role in ['root','host','floater','leader','participating']:
            self.assertTrue(can_review_photo(self.users[role],photo),role)
        for role in ['owner','moderator','reader','outsider']:
            self.assertFalse(can_review_photo(self.users[role],photo),role)
        # Ordinary staff self-review policy is unchanged.
        own=create_photo_submission(month_id=self.month.pk,actor=self.users['leader'],prompt_id=self.photo_prompt.pk,reader_confirmed=True)
        self.assertTrue(can_review_photo(self.users['leader'],own))
        TeamAssignment.objects.filter(participant=self.reader,ended_at__isnull=True).update(ended_at=timezone.now())
        TeamAssignment.objects.create(month=self.month,participant=self.reader,team=self.other_team)
        for role in ['leader','participating']:
            self.assertFalse(can_review_photo(self.users[role],photo),role)
            with self.assertRaises(PermissionDenied):self.transition(photo,'rejected',actor=self.users[role])
        photo=self.transition(photo,'rejected');self.assertFalse(can_edit_pending_photo(self.reader.user,photo))

    def test_inactive_ineligible_floater_and_foreign_actor_denied(self):
        enrollment=MonthEnrollment.objects.get(month=self.month,participant=self.reader)
        for field,value in [('is_active',False),('awaiting_scoring_team',True)]:
            with patch.object(MonthEnrollment,'save'):
                MonthEnrollment.objects.filter(pk=enrollment.pk).update(**{field:value})
                with self.assertRaises(ValidationError):self.create()
                MonthEnrollment.objects.filter(pk=enrollment.pk).update(**{field:not value})
        Membership.objects.filter(pk=self.reader.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):self.create()
        Membership.objects.filter(pk=self.reader.pk).update(is_active=True)
        # Simulate contradictory legacy state to verify the service's independent guard.
        ChallengeStaffAssignment.objects.bulk_create([ChallengeStaffAssignment(month=self.month,membership=self.reader,role='floater')])
        with self.assertRaises(ValidationError):self.create()
        for actor in [AnonymousUser(),self.users['outsider'],self.users['root']]:
            with self.assertRaises(PermissionDenied):
                create_photo_submission(month_id=self.month.pk,actor=actor,prompt_id=self.photo_prompt.pk,reader_confirmed=True)
        self.assertFalse(ScavengerPhotoSubmission.objects.exists())

    def test_creation_lifecycle_and_disabled_category(self):
        for state in ['draft','upcoming','finalizing','completed','archived']:
            ChallengeMonth.objects.filter(pk=self.month.pk).update(status=state)
            with self.assertRaises((ValidationError,PermissionDenied)):self.create()
        ChallengeMonth.objects.filter(pk=self.month.pk).update(status='active')
        self.photo_prompt.category.enabled=False;self.photo_prompt.category.save()
        with self.assertRaises(ValidationError):self.create()

    def test_closed_gate_blocks_model_services_and_leaves_scores_unchanged(self):
        photo=self.create();before=self.protected()
        with override_settings(NORTHBOUND_PROMPT_RESERVATIONS_READY=False):
            for operation in [self.create,lambda:photo.save(),lambda:self.transition(photo,'withdrawn',actor=self.reader.user),lambda:photo.prompt_claim.save()]:
                with self.assertRaises(ValidationError):operation()
        self.assertEqual(self.protected(),before)

    def test_november_exclusion(self):
        from datetime import date
        self.group.slug='guildwars';self.group.save()
        ChallengeMonth.objects.filter(pk=self.month.pk).update(starts_on=date(2026,11,1),ends_on=date(2026,11,30))
        with self.assertRaises(ValidationError):self.create()

    def test_no_photo_approval_or_qualification_in_phase_a(self):
        photo=self.create();before=self.protected()
        with self.assertRaises(ValidationError):self.transition(photo,'approved')
        photo.status='approved';photo.reviewer_verified=True;photo.reviewed_by=self.actor;photo.reviewed_at=timezone.now()
        with self.assertRaises(ValidationError):photo.save()
        claim=PromptClaim.objects.get(photo_submission=photo);claim.state='qualified'
        with self.assertRaises(ValidationError):claim.save()
        self.assertEqual(self.protected(),before);self.assertFalse(ModifierProvenance.objects.exists())

    def test_acceptance_identity_and_history_preserved(self):
        photo=self.create();before=photo.acceptance_evidence.copy()
        photo.acceptance_evidence={**before,'team_id':self.other_team.pk}
        with self.assertRaises(ValidationError):photo.save()
        photo.refresh_from_db();self.assertEqual(photo.acceptance_evidence,before)
        with self.assertRaises(ValidationError):photo.delete()


    def test_terminal_stages_deny_all_reservation_changes_without_writes(self):
        photo=self.create();before=(photo.version, photo.status, AuditEvent.objects.count(),self.protected())
        for stage in ['completed','archived']:
            ChallengeMonth.objects.filter(pk=self.month.pk).update(status=stage)
            for actor in [self.users['root'],self.actor,self.reader.user]:
                with self.assertRaises(PermissionDenied):self.transition(photo,'withdrawn' if actor==self.reader.user else 'voided',actor=actor)
        photo.refresh_from_db()
        self.assertEqual((photo.version,photo.status,AuditEvent.objects.count(),self.protected()),before)

    def test_departure_preserves_acceptance_but_historical_restore_is_unavailable(self):
        photo=self.create();evidence=photo.acceptance_evidence.copy()
        photo=self.transition(photo,'withdrawn',actor=self.reader.user)
        ended=timezone.now()
        MonthEnrollment.objects.filter(month=self.month,participant=self.reader).update(is_active=False,credited_team=self.team,inactivated_at=ended,inactive_reason='withdrawn')
        photo.participation_period.__class__.objects.filter(pk=photo.participation_period_id).update(ended_at=ended,end_reason='withdrawn')
        TeamAssignment.objects.filter(pk=photo.accepted_team_assignment_id).update(ended_at=ended)
        with self.assertRaises(ValidationError):self.transition(photo,'pending')
        photo.refresh_from_db();self.assertEqual(photo.acceptance_evidence,evidence)
        self.assertEqual(photo.accepted_team_assignment.team_id,self.team.pk)
        self.assertEqual(photo.status,'withdrawn')

    def test_same_challenge_and_reader_required_for_photo_claim(self):
        photo=self.create();claim=photo.prompt_claim
        other_month=ChallengeMonth.objects.create(group=self.group,name='Other',status='active')
        from .models import PromptCategory
        other_category=PromptCategory.objects.create(month=other_month,kind='scavenger')
        other_prompt=ChallengePrompt.objects.create(category=other_category,number=1,description='Other',submission_type='photo')
        claim.category=other_category;claim.prompt=other_prompt
        with self.assertRaises(ValidationError):claim.save()
        claim.refresh_from_db();claim.participant=self.members['leader']
        with self.assertRaises(ValidationError):claim.save()

    def test_anonymous_transition_and_invalid_state_rejected(self):
        photo=self.create()
        with self.assertRaises(PermissionDenied):self.transition(photo,'withdrawn',actor=AnonymousUser())
        with self.assertRaises(ValidationError):self.transition(photo,'unknown')
        photo.refresh_from_db();self.assertEqual(photo.status,'pending')

    def test_void_and_reacquire_are_idempotent_and_preserve_identity(self):
        photo=self.create();pk=photo.pk
        photo=self.transition(photo,'voided',reason='Foundation test')
        version=photo.version;count=AuditEvent.objects.count()
        again=self.transition(photo,'voided')
        self.assertEqual(again.version,version);self.assertEqual(AuditEvent.objects.count(),count)
        photo=self.transition(photo,'pending')
        self.assertEqual(photo.pk,pk);self.assertEqual(photo.prompt_claim.state,'reserved')
        self.assertFalse(ModifierProvenance.objects.exists())


class PhotoMigrationTests(TransactionTestCase):
    def tearDown(self):
        executor=MigrationExecutor(connection);executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_0076_to_0077_preserves_existing_book_claims_and_provenance(self):
        executor=MigrationExecutor(connection);executor.migrate([('core','0076_team_branding')])
        apps=executor.loader.project_state([('core','0076_team_branding')]).apps
        group=apps.get_model('core','ReadingGroup').objects.create(name='Migration',slug='photo-migration')
        month=apps.get_model('core','ChallengeMonth').objects.create(group=group,name='Legacy',status='active')
        user=apps.get_model('auth','User').objects.create(username='photo-migration-reader')
        member=apps.get_model('core','Membership').objects.create(group=group,user=user)
        book=apps.get_model('core','BookSubmission').objects.create(month=month,participant=member,title='Preserved',author='Author',book_format='ebook',completed_on='2027-01-03',submitted_pages=100,status='approved',approved_pages=100)
        category=apps.get_model('core','PromptCategory').objects.create(month=month,kind='scavenger',enabled=True)
        prompt=apps.get_model('core','ChallengePrompt').objects.create(category=category,number=1,description='Book',points=10)
        apps.get_model('core','PromptClaim').objects.create(category=category,prompt=prompt,submission=book,participant=member,state='qualified',earning_evidence={'amount':10})
        apps.get_model('core','ModifierProvenance').objects.create(month=month,participant=member,submission=book,source_type='scavenger_prompt',source_reference='prompt_claim:legacy',source_label='Preserved',amount=10,effective_date='2027-01-03')
        names=['BookSubmission','PromptClaim','ModifierProvenance','ChallengeMonth','TeamAssignment']
        before={n:list(apps.get_model('core',n).objects.order_by('pk').values()) for n in names}
        executor=MigrationExecutor(connection);executor.migrate([('core','0077_scavenger_photo_foundation')])
        apps=executor.loader.project_state([('core','0077_scavenger_photo_foundation')]).apps
        after={n:list(apps.get_model('core',n).objects.order_by('pk').values()) for n in names}
        for row in after['PromptClaim']:self.assertIsNone(row.pop('photo_submission_id'))
        self.assertEqual(before,after)
        self.assertEqual(apps.get_model('core','ChallengePrompt').objects.get(pk=prompt.pk).submission_type,'book')
        self.assertFalse(apps.get_model('core','ScavengerPhotoSubmission').objects.exists())
        with connection.cursor() as cursor:
            cursor.execute('PRAGMA integrity_check');self.assertEqual(cursor.fetchall(),[('ok',)])
            cursor.execute('PRAGMA foreign_key_check');self.assertEqual(cursor.fetchall(),[])
        # Empty-Photo rollback is safe and preserves the original Book claims.
        MigrationExecutor(connection).migrate([('core','0076_team_branding')])
        self.assertEqual(list(apps.get_model('core','ModifierProvenance').objects.values()),before['ModifierProvenance'])
