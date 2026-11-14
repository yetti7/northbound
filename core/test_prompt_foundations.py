from datetime import date
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied,ValidationError
from django.db import IntegrityError,transaction,connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError
from django.test import TestCase,TransactionTestCase,override_settings
from django.urls import reverse
from .models import (ReadingGroup,Membership,ChallengeMonth,ChallengeStaffAssignment,MonthEnrollment,Team,TeamAssignment,
    BookSubmission,ModifierProvenance,AuditEvent,PromptCategory,ChallengePrompt,PromptClaim,PromptCompletion)
from .prompt_configuration import configuration_token,save_category,save_prompt,automation_excluded,can_configure_prompts
from .score_aggregation import challenge_score_totals,challenge_team_totals


@override_settings(NORTHBOUND_PROMPT_RESERVATIONS_READY=True)
class PromptFoundationTests(TestCase):
    def setUp(self):
        User=get_user_model()
        self.group=ReadingGroup.objects.create(name='Prompt fixtures',slug='prompt-fixtures')
        self.month=ChallengeMonth.objects.create(group=self.group,name='Future Challenge',status='active',starts_on=date(2027,1,1),ends_on=date(2027,1,31))
        self.users={k:User.objects.create_user('prompt-'+k,is_superuser=k=='root') for k in ['root','host','participating','owner','moderator','floater','leader','reader','outsider']}
        self.members={k:Membership.objects.create(group=self.group,user=u,display_name=k,role='owner' if k=='owner' else 'moderator' if k=='moderator' else 'member') for k,u in self.users.items() if k not in ['root','outsider']}
        self.team=Team.objects.create(month=self.month,name='One')
        self.other_team=Team.objects.create(month=self.month,name='Two')
        for k in ['reader','participating','leader']:
            MonthEnrollment.objects.create(month=self.month,participant=self.members[k])
            TeamAssignment.objects.create(month=self.month,participant=self.members[k],team=self.team)
        for k in ['host','participating','floater','leader']:
            ChallengeStaffAssignment.objects.create(month=self.month,membership=self.members[k],role='host' if k in ['host','participating'] else 'team_leader' if k=='leader' else 'floater',team=self.team if k=='leader' else None)
        self.book=BookSubmission.objects.create(month=self.month,participant=self.members['reader'],title='Preserve this book',author='Author',book_format='ebook',completed_on=date(2027,1,3),submitted_pages=368,approved_pages=368,final_scored_pages=368,status='approved')
        self.actor=self.users['host']

    def token(self,kind='bingo',actor=None):return configuration_token(self.month,kind,actor or self.actor)
    def prompt(self,number=1,kind='bingo',**values):
        return save_prompt(month_id=self.month.pk,kind=kind,actor=self.actor,version=self.token(kind),values={'number':number,'description':'Full prompt wording','points':25,'is_active':True,**values})[0]
    def settings(self,kind='bingo',actor=None,**values):
        actor=actor or self.actor
        return save_category(month_id=self.month.pk,kind=kind,actor=actor,version=self.token(kind,actor),values={'enabled':False,'completion_bonus':0,'exclude_automation':False,**values})
    def claim(self,prompt,book=None,**values):
        return PromptClaim.objects.create(category=prompt.category,prompt=prompt,submission=book or self.book,participant=self.members['reader'],**values)
    def protected(self):
        return {m.__name__:list(m.objects.order_by('pk').values()) for m in [BookSubmission,ModifierProvenance,MonthEnrollment,TeamAssignment]},challenge_score_totals(month=self.month),challenge_team_totals(month=self.month,team_ids=[self.team.pk,self.other_team.pk])
    def url(self,kind='bingo'):return reverse('challenge-prompt-settings',args=[self.group.slug,self.month.pk,kind])

    def test_disabled_defaults_get_is_readonly_and_categories_independent(self):
        self.client.force_login(self.actor)
        self.assertEqual(self.client.get(self.url()).status_code,200)
        self.assertEqual(PromptCategory.objects.count(),0)
        self.assertFalse(self.month.prompt_automation_excluded)
        self.prompt();self.prompt(kind='scavenger')
        self.settings(enabled=True,completion_bonus=100)
        self.settings(kind='scavenger',completion_bonus=200)
        bingo=PromptCategory.objects.get(kind='bingo');scavenger=PromptCategory.objects.get(kind='scavenger')
        self.assertTrue(bingo.enabled);self.assertFalse(scavenger.enabled)
        self.assertEqual((bingo.completion_bonus,scavenger.completion_bonus),(100,200))
        self.assertFalse(PromptClaim.objects.exists());self.assertFalse(PromptCompletion.objects.exists())

    def test_thirty_slots_unique_numbering_and_database_constraints(self):
        category=PromptCategory.objects.create(month=self.month,kind='bingo')
        ChallengePrompt.objects.bulk_create([ChallengePrompt(category=category,number=i,description='Slot',points=0) for i in range(1,31)])
        self.assertEqual(category.prompts.count(),30)
        for number in [0,31,1]:
            with self.assertRaises(ValidationError):ChallengePrompt.objects.create(category=category,number=number,description='Invalid')
        for number in [31,1]:
            with self.assertRaises(IntegrityError),transaction.atomic():
                ChallengePrompt.objects.bulk_create([ChallengePrompt(category=category,number=number,description='Invalid')])
        self.prompt(kind='scavenger')

    def test_point_and_bonus_validation(self):
        self.prompt(points=0)
        for amount in [-1,2147483648,'not-a-number','1.5']:
            with self.assertRaises(ValidationError):self.prompt(2,points=amount)
            with self.assertRaises(ValidationError):self.settings(completion_bonus=amount)
        self.assertEqual(ChallengePrompt.objects.count(),1)

    def test_roles_and_direct_service_authorization(self):
        for key in self.users:
            user=self.users[key];self.client.force_login(user)
            expected=200 if key in ['root','host'] else 403
            self.assertEqual(self.client.get(self.url()).status_code,expected,key)
            if expected==403:
                with self.assertRaises(PermissionDenied):self.settings(actor=user)
        # Group ownership must not bypass participating-Host restrictions.
        Membership.objects.filter(pk=self.members['participating'].pk).update(role='owner')
        self.assertFalse(can_configure_prompts(self.users['participating'],self.month))
        self.users['host'].is_active=False;self.users['host'].save(update_fields=['is_active'])
        with self.assertRaises(PermissionDenied):self.settings()

    def test_cross_group_and_cross_category_prompt_requests_are_denied(self):
        prompt=self.prompt(kind='scavenger')
        with self.assertRaises(PermissionDenied):
            save_prompt(month_id=self.month.pk,kind='bingo',actor=self.actor,version=self.token(),prompt_id=prompt.pk,values={'number':1,'description':'Attack','points':0,'is_active':True})
        foreign=ReadingGroup.objects.create(name='Other',slug='other-prompt-group')
        other=ChallengeMonth.objects.create(group=foreign,name='Other',status='active')
        with self.assertRaises(PermissionDenied):save_category(month_id=other.pk,kind='bingo',actor=self.actor,version=configuration_token(other,'bingo',self.actor),values={'enabled':False,'completion_bonus':0,'exclude_automation':False})
        self.client.force_login(self.actor)
        self.assertEqual(self.client.get(reverse('challenge-prompt-settings',args=[foreign.slug,self.month.pk,'bingo'])).status_code,404)
        self.assertEqual(self.client.get(self.url()+'?edit='+str(prompt.pk)).status_code,404)

    def test_lifecycle_gates_at_endpoint_and_service(self):
        for stage in ['draft','upcoming','active','finalizing','completed','archived']:
            ChallengeMonth.objects.filter(pk=self.month.pk).update(status=stage);self.month.refresh_from_db()
            for key in ['root','host']:
                self.client.force_login(self.users[key]);editable=stage in ['draft','upcoming','active']
                self.assertEqual(self.client.get(self.url()).status_code,200 if editable else 403)
                if editable:self.settings(actor=self.users[key],completion_bonus=1)
                else:
                    with self.assertRaises(PermissionDenied):self.settings(actor=self.users[key])

    def test_wording_and_points_update_in_place_preserve_activity_and_scores(self):
        prompt=self.prompt();claim=self.claim(prompt);before=self.protected();evidence=claim.selection_evidence.copy()
        _,changed=save_prompt(month_id=self.month.pk,kind='bingo',actor=self.actor,version=self.token(),prompt_id=prompt.pk,values={'number':1,'description':'Corrected full wording','points':50,'is_active':True})
        self.assertTrue(changed);prompt.refresh_from_db();claim.refresh_from_db()
        self.assertEqual(prompt.revision,2);self.assertEqual(claim.selection_evidence,evidence)
        self.assertEqual(self.protected(),before)
        event=AuditEvent.objects.get(action='prompt.updated',object_id=str(prompt.pk),summary__contains='Corrected full wording')
        self.assertEqual(event.actor_id,self.actor.pk);self.assertEqual(event.group_id,self.group.pk)
        self.assertFalse(PromptCompletion.objects.exists());self.assertFalse(ModifierProvenance.objects.exists())

    def test_first_reservation_permanently_freezes_structure_even_after_release(self):
        prompt=self.prompt();claim=self.claim(prompt);category=PromptCategory.objects.get(pk=prompt.category_id)
        self.assertIsNotNone(category.structure_frozen_at)
        self.assertEqual(category.frozen_prompt_set,[{'id':prompt.pk,'number':1}])
        self.client.force_login(self.actor)
        response=self.client.get(self.url()+'?edit='+str(prompt.pk))
        self.assertTrue(response.context['prompt_form'].fields['number'].disabled)
        self.assertTrue(response.context['prompt_form'].fields['is_active'].disabled)
        claim.state='released';claim.save(update_fields=['state'])
        with self.assertRaises(ValidationError):self.prompt(2)
        for changes in [{'number':2},{'is_active':False}]:
            prompt.refresh_from_db()
            for key,value in changes.items():setattr(prompt,key,value)
            with self.assertRaises(ValidationError):prompt.save()
        category.frozen_prompt_set=[]
        with self.assertRaises(ValidationError):category.save()

    def test_claim_constraints_supersession_and_history_protection(self):
        prompt=self.prompt();second=self.prompt(2);claim=self.claim(prompt)
        with self.assertRaises(ValidationError):self.claim(second)
        with self.assertRaises(IntegrityError),transaction.atomic():
            PromptClaim.objects.bulk_create([PromptClaim(category=prompt.category,prompt=second,submission=self.book,participant=self.members['reader'])])
        duplicate=BookSubmission.objects.create(month=self.month,participant=self.members['reader'],title='Second',author='Author',book_format='ebook',completed_on=self.book.completed_on,submitted_pages=100)
        with self.assertRaises(ValidationError):self.claim(prompt,duplicate)
        another=self.prompt(kind='scavenger');self.claim(another)
        with self.assertRaises(IntegrityError),transaction.atomic():
            PromptClaim.objects.bulk_create([PromptClaim(category=prompt.category,prompt=prompt,submission=duplicate,participant=self.members['reader'])])
        claim.state='released';claim.is_current=False;claim.save()
        self.claim(prompt,duplicate)
        with self.assertRaises(ValidationError):claim.delete()
        with self.assertRaises(ProtectedError):ChallengePrompt.objects.filter(pk=prompt.pk).delete()
        with self.assertRaises(ValidationError):prompt.delete()

    def test_completion_foundation_uniqueness_and_frozen_evidence(self):
        prompt=self.prompt();self.claim(prompt);category=PromptCategory.objects.get(pk=prompt.category_id)
        completion=PromptCompletion.objects.create(category=category,participant=self.members['reader'],frozen_prompt_set=category.frozen_prompt_set)
        self.assertFalse(completion.is_active)
        with self.assertRaises(IntegrityError),transaction.atomic():
            PromptCompletion.objects.bulk_create([PromptCompletion(category=category,participant=self.members['reader'])])
        with self.assertRaises(ValidationError):PromptCompletion.objects.create(category=category,participant=self.members['reader'])
        completion.bonus_amount_snapshot=100
        with self.assertRaises(ValidationError):completion.save()
        self.assertFalse(ModifierProvenance.objects.exists())

    def test_stale_tampered_and_actor_bound_forms(self):
        token=self.token();prompt=self.prompt()
        for version in [token,token+'tampered',configuration_token(self.month,'bingo',self.users['root'])]:
            with self.assertRaises(ValidationError):save_category(month_id=self.month.pk,kind='bingo',actor=self.actor,version=version,values={'enabled':False,'completion_bonus':0,'exclude_automation':False})
        token=self.token();self.claim(prompt)
        with self.assertRaises(ValidationError):save_category(month_id=self.month.pk,kind='bingo',actor=self.actor,version=token,values={'enabled':False,'completion_bonus':0,'exclude_automation':False})

    def test_november_guard_uses_schedule_and_group_and_persists_without_backfill(self):
        ReadingGroup.objects.filter(pk=self.group.pk).update(slug='guildwars')
        self.group.refresh_from_db()
        ChallengeMonth.objects.filter(pk=self.month.pk).update(starts_on=date(2026,11,1),ends_on=date(2026,11,30),name='Renamable November')
        self.month.refresh_from_db();before=self.protected()
        self.assertTrue(automation_excluded(self.month));self.assertFalse(self.month.prompt_automation_excluded)
        prompt=self.prompt();self.month.refresh_from_db()
        self.assertTrue(self.month.prompt_automation_excluded)
        self.assertTrue(AuditEvent.objects.filter(action='prompt.automation_excluded',actor=self.actor).exists())
        for kind in ['bingo','scavenger']:
            with self.assertRaises(ValidationError):self.settings(kind=kind,enabled=True)
            with self.assertRaises(ValidationError):PromptCategory.objects.create(month=self.month,kind=kind,enabled=True)
        ChallengeMonth.objects.filter(pk=self.month.pk).update(name='Changed',starts_on=date(2027,2,1),ends_on=date(2027,2,28));self.month.refresh_from_db()
        self.assertTrue(automation_excluded(self.month));self.settings(exclude_automation=False)
        self.month.refresh_from_db();self.assertTrue(self.month.prompt_automation_excluded)
        self.assertEqual(self.protected(),before);self.assertFalse(PromptClaim.objects.exists())

    def test_configuration_posts_and_mobile_markup_preserve_all_existing_data(self):
        before=self.protected();self.client.force_login(self.actor)
        response=self.client.get(self.url());self.assertContains(response,'configuration only')
        self.assertContains(response,'stacked-form standard-form');self.assertContains(response,'Back to Challenge Settings')
        self.assertNotContains(response,'prompt_claims')
        response=self.client.post(self.url(),{'action':'prompt','prompt-version':self.token(),'prompt-number':1,'prompt-description':'Full description with a very long title '*10,'prompt-points':25,'prompt-is_active':'on'})
        self.assertEqual(response.status_code,302)
        self.assertEqual(self.protected(),before);self.assertEqual(PromptClaim.objects.count(),0);self.assertEqual(PromptCompletion.objects.count(),0)
        self.assertContains(self.client.get(reverse('challenge-settings',args=[self.group.slug,self.month.pk])),'Bingo Settings')

    def test_noop_and_invalid_writes_do_not_audit_or_mutate(self):
        prompt=self.prompt();count=AuditEvent.objects.count()
        _,changed=save_prompt(month_id=self.month.pk,kind='bingo',actor=self.actor,version=self.token(),prompt_id=prompt.pk,values={'number':1,'description':prompt.description,'points':25,'is_active':True})
        self.assertFalse(changed);self.assertEqual(AuditEvent.objects.count(),count)
        with self.assertRaises(ValidationError):self.prompt(2,description='   ')
        self.assertEqual(AuditEvent.objects.count(),count)


class PromptMigrationSafetyTests(TransactionTestCase):
    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_schema_only_migration_preserves_legacy_data_and_creates_no_activity(self):
        executor=MigrationExecutor(connection);executor.migrate([('core','0073_community_branding')])
        apps=executor.loader.project_state([('core','0073_community_branding')]).apps
        Group=apps.get_model('core','ReadingGroup');Month=apps.get_model('core','ChallengeMonth')
        group=Group.objects.create(name='Legacy',slug='guildwars')
        month=Month.objects.create(group=group,name='November',status='active',starts_on=date(2026,11,1),ends_on=date(2026,11,30))
        User=apps.get_model('auth','User');Member=apps.get_model('core','Membership');Book=apps.get_model('core','BookSubmission')
        user=User.objects.create(username='migration-prompt-reader');member=Member.objects.create(group=group,user=user,display_name='Legacy')
        Book.objects.create(month=month,participant=member,title='Legacy book',author='Author',book_format='ebook',completed_on=date(2026,11,4),submitted_pages=368,approved_pages=368,final_scored_pages=368,status='approved')
        before=list(Book.objects.order_by('pk').values())
        executor=MigrationExecutor(connection);executor.migrate([('core','0074_prompt_foundations')])
        current=executor.loader.project_state([('core','0074_prompt_foundations')]).apps
        self.assertEqual(list(current.get_model('core','BookSubmission').objects.order_by('pk').values()),before)
        for name in ['PromptCategory','ChallengePrompt','PromptClaim','PromptCompletion']:
            self.assertEqual(current.get_model('core',name).objects.count(),0)
        self.assertFalse(current.get_model('core','ChallengeMonth').objects.get(pk=month.pk).prompt_automation_excluded)
