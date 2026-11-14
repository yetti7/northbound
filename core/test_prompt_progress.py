from datetime import date
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.test import TransactionTestCase,override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from . import test_prompt_reservations as fixture
from .models import (BookSubmission,PromptClaim,PromptCompletion,PromptCategory,ModifierProvenance,AuditEvent,
    MonthEnrollment,TeamAssignment,Membership,ChallengeMonth)
from .prompt_progress import categories,enrollments,progress_rows
from .prompt_scoring import review_prompts
from .permissions import can_review_submission


@override_settings(NORTHBOUND_PROMPT_RESERVATIONS_READY=True,NORTHBOUND_HARDCOVER_WRITEBACK_ENABLED=False)
class PromptProgressTests(TransactionTestCase):
    setUp=fixture.PromptReservationTests.setUp
    setUp_fixture=fixture.PromptReservationTests.setUp_fixture
    token=fixture.PromptReservationTests.token
    prompt=fixture.PromptReservationTests.prompt
    settings=fixture.PromptReservationTests.settings
    pending=fixture.PromptReservationTests.pending
    reserve=fixture.PromptReservationTests.reserve

    def route(self,staff=False,**query):
        url=reverse('prompt-progress-report' if staff else 'prompt-progress',args=[self.group.slug,self.month.pk])
        return self.client.get(url,query)
    def rows(self):return progress_rows(self.month,enrollments(self.reader.user,self.month),list(categories(self.month)))
    def other(self):
        user=get_user_model().objects.create_user('private-other-reader')
        member=Membership.objects.create(group=self.group,user=user)
        MonthEnrollment.objects.create(month=self.month,participant=member)
        TeamAssignment.objects.create(month=self.month,participant=member,team=self.other_team)
        return member
    def qualify(self,book=None,**decisions):review_prompts(book or self.book,self.actor,decisions)
    def complete(self):
        for i,prompt in enumerate(self.bingo):
            book=self.book if i==0 else BookSubmission.objects.create(month=self.month,participant=self.reader,title='Complete book',author='Author',book_format='ebook',completed_on=date(2027,1,3),submitted_pages=368,approved_pages=368,status='approved')
            self.reserve(book,bingo=prompt.pk);self.qualify(book,bingo='qualified')

    def test_available_pending_completed_and_full_descriptions(self):
        self.reserve(bingo=self.bingo[0].pk,scavenger=self.scavenger[0].pk);self.qualify(bingo='qualified')
        rows=self.rows();bingo=next(r for r in rows if r['category'].kind=='bingo');scavenger=next(r for r in rows if r['category'].kind=='scavenger')
        self.assertEqual((bingo['completed'],bingo['pending'],bingo['remaining'],bingo['total']),(1,0,2,3))
        self.assertEqual((scavenger['completed'],scavenger['pending'],scavenger['remaining']),(0,1,2))
        response=self.route();self.assertContains(response,'Completed');self.assertContains(response,'Available');self.assertContains(response,'Pending confirmation');self.assertContains(response,'Full prompt wording')

    def test_zero_points_and_frozen_completion_bonus(self):
        for p in self.bingo:p.points=0;p.save()
        self.complete();self.assertFalse(ModifierProvenance.objects.exists())
        row=self.rows()[0];self.assertTrue(row['bonus_earned']);self.assertEqual((row['completed'],row['remaining'],row['earned_bonus']),(3,0,0))
        self.assertContains(self.route(),'Earned: 0 points.')

    def test_rejected_voided_and_superseded_claims_not_counted(self):
        self.reserve(bingo=self.bingo[0].pk);claim=self.book.prompt_claims.get()
        for values in [{'state':'rejected'},{'state':'released'},{'state':'qualified','is_current':False}]:
            PromptClaim.objects.filter(pk=claim.pk).update(**values)
            self.assertEqual(self.rows()[0]['completed'],0);self.assertEqual(self.rows()[0]['pending'],0)
        PromptClaim.objects.filter(pk=claim.pk).update(state='qualified',is_current=True)
        BookSubmission.objects.filter(pk=self.book.pk).update(is_removed=True)
        self.assertEqual(self.rows()[0]['completed'],0)

    def test_manual_game_rewards_do_not_count(self):
        ModifierProvenance.objects.create(month=self.month,participant=self.reader,source_type='game_reward',source_reference='manual-only',amount=100,effective_date=date(2027,1,3))
        self.assertEqual(self.rows()[0]['completed'],0);self.assertFalse(self.rows()[0]['bonus_earned'])

    def test_reader_self_only_and_query_tampering(self):
        other=self.other();response=self.route();self.assertNotContains(response,'private-other-reader')
        for query in ({'reader':other.pk},{'reader':self.reader.pk},{'team':self.other_team.pk}):self.assertEqual(self.route(**query).status_code,403)
        self.assertEqual(self.route(staff=True).status_code,403)
        self.assertEqual(self.client.get(reverse('prompt-progress',args=['wrong-group',self.month.pk])).status_code,404)

    def test_cross_group_and_nonmember_denied(self):
        self.client.force_login(self.users['outsider'])
        self.assertEqual(self.route().status_code,403);self.assertEqual(self.route(staff=True).status_code,403)

    def test_staff_roles_and_owner_reporting_do_not_grant_review(self):
        for role in ('root','owner','host','floater','leader','participating'):
            self.client.force_login(self.users[role]);self.assertEqual(self.route(staff=True).status_code,200,role)
        self.assertFalse(can_review_submission(self.users['owner'],self.book))
        self.client.force_login(self.users['moderator']);self.assertEqual(self.route(staff=True).status_code,403)

    def test_participating_host_and_team_leader_out_of_team_privacy(self):
        other=self.other()
        for role in ('participating','leader'):
            self.client.force_login(self.users[role]);response=self.route(staff=True)
            self.assertContains(response,self.reader.user.username);self.assertNotContains(response,other.user.username)
            self.assertEqual(self.route(staff=True,reader=other.pk).status_code,403)
            self.assertEqual(self.route(staff=True,team=self.other_team.pk).status_code,403)
        Membership.objects.filter(pk=self.members['participating'].pk).update(role='owner')
        self.client.force_login(self.users['participating']);self.assertNotContains(self.route(staff=True),other.user.username)

    def test_challenge_wide_roles_can_filter_other_team(self):
        other=self.other()
        for role in ('host','floater','owner','root'):
            self.client.force_login(self.users[role]);response=self.route(staff=True,team=self.other_team.pk,reader=other.pk)
            self.assertContains(response,other.user.username)

    def test_team_transfer_updates_private_reporting_scope(self):
        from .participation import assign_participant_to_team
        assign_participant_to_team(month=self.month,participant=self.reader,team=self.other_team,actor=self.actor)
        self.client.force_login(self.users['participating']);self.assertNotContains(self.route(staff=True),self.reader.user.username)
        self.assertEqual(self.route(staff=True,reader=self.reader.pk).status_code,403)
        self.client.force_login(self.users['floater']);self.assertContains(self.route(staff=True),self.reader.user.username)

    def test_category_and_completion_filters_work_independently(self):
        self.complete();self.client.force_login(self.actor)
        complete=self.route(staff=True,reader=self.reader.pk,completion='complete');self.assertEqual(len(complete.context['rows']),1)
        self.assertEqual(complete.context['rows'][0]['category'].kind,'bingo')
        incomplete=self.route(staff=True,reader=self.reader.pk,completion='incomplete');self.assertEqual(len(incomplete.context['rows']),1)
        self.assertEqual(incomplete.context['rows'][0]['category'].kind,'scavenger')
        self.assertEqual(len(self.route(staff=True,category='bingo',reader=self.reader.pk).context['rows']),1)
        self.assertEqual(self.route(staff=True,category='forged').status_code,404)

    def test_approved_book_pending_prompts_queue_and_navigation(self):
        self.reserve(bingo=self.bingo[0].pk);self.client.force_login(self.actor)
        response=self.route(staff=True,reader=self.reader.pk)
        self.assertContains(response,reverse('submission-review',args=[self.group.slug,self.month.pk,self.book.pk]))
        queue=self.client.get(reverse('review-queue',args=[self.group.slug,self.month.pk]))
        self.assertContains(queue,'Book approved · prompt review pending');self.assertContains(queue,'Bingo prompt · needs confirmation');self.assertContains(queue,'Prompt Progress Report')
        self.client.force_login(self.users['owner']);self.assertNotContains(self.route(staff=True),'Review submission')

    @override_settings(NORTHBOUND_PROMPT_RESERVATIONS_READY=False)
    def test_disabled_gate_hides_operational_pages_and_navigation(self):
        self.assertEqual(self.route().status_code,404);self.client.force_login(self.actor);self.assertEqual(self.route(staff=True).status_code,404)
        response=self.client.get(self.month.get_absolute_url());self.assertEqual(response.status_code,200);self.assertNotContains(response,'Prompt Progress')

    def test_november_exclusion_and_disabled_categories(self):
        ChallengeMonth.objects.filter(pk=self.month.pk).update(prompt_automation_excluded=True)
        self.assertEqual(self.route().status_code,404)
        ChallengeMonth.objects.filter(pk=self.month.pk).update(prompt_automation_excluded=False)
        self.group.slug='guildwars';self.group.save()
        ChallengeMonth.objects.filter(pk=self.month.pk).update(starts_on=date(2026,11,1),ends_on=date(2026,11,30))
        self.assertEqual(self.route().status_code,404)
        self.group.slug='prompt-fixtures';self.group.save()
        PromptCategory.objects.update(enabled=False);self.assertEqual(self.route().status_code,404)

    def test_reporting_lifecycle_is_read_only_and_terminal_review_links_absent(self):
        self.reserve(bingo=self.bingo[0].pk);self.client.force_login(self.actor)
        for status in ('active','finalizing','completed','archived'):
            ChallengeMonth.objects.filter(pk=self.month.pk).update(status=status)
            response=self.route(staff=True);self.assertEqual(response.status_code,200,status)
            if status in ('completed','archived'):self.assertNotContains(response,'Review submission')
        for status in ('draft','upcoming'):
            ChallengeMonth.objects.filter(pk=self.month.pk).update(status=status);self.assertEqual(self.route(staff=True).status_code,404)
        self.assertEqual(self.client.post(reverse('prompt-progress-report',args=[self.group.slug,self.month.pk])).status_code,405)

    def test_gets_preserve_all_claims_awards_submissions_and_audits(self):
        self.reserve(bingo=self.bingo[0].pk);self.qualify(bingo='qualified')
        models=(BookSubmission,PromptClaim,PromptCompletion,ModifierProvenance,AuditEvent,MonthEnrollment,TeamAssignment)
        before={m.__name__:list(m.objects.order_by('pk').values()) for m in models}
        self.route();self.client.force_login(self.actor);self.route(staff=True)
        self.assertEqual({m.__name__:list(m.objects.order_by('pk').values()) for m in models},before)

    def test_batched_queries_and_pagination(self):
        self.reserve(bingo=self.bingo[0].pk,scavenger=self.scavenger[0].pk)
        self.client.force_login(self.actor)
        with CaptureQueriesContext(connection) as small:self.route(staff=True)
        for i in range(30):
            user=get_user_model().objects.create_user('bulk-reader-'+str(i));member=Membership.objects.create(group=self.group,user=user)
            MonthEnrollment.objects.create(month=self.month,participant=member)
            book=BookSubmission.objects.create(month=self.month,participant=member,title='Bulk pending book',author='Author',book_format='ebook',completed_on=date(2027,1,3),submitted_pages=368)
            PromptClaim.objects.create(category=self.bingo[0].category,prompt=self.bingo[0],participant=member,submission=book)
        with CaptureQueriesContext(connection) as many:response=self.route(staff=True)
        self.assertLessEqual(len(many),len(small)+4);self.assertLess(len(many),75)
        self.assertEqual(len(response.context['page_obj']),25);self.assertContains(response,'Next')

    def test_mobile_markup_and_challenge_navigation(self):
        response=self.route();self.assertContains(response,'prompt-progress-grid');self.assertContains(response,'<progress');self.assertContains(response,'<summary>Prompt details</summary>',html=True)
        self.assertNotContains(response,'<table')
        response=self.client.get(self.month.get_absolute_url());self.assertEqual(response.status_code,200);self.assertContains(response,'My Prompt Progress')

    def test_established_points_remain_distinct_from_current_prompt_value(self):
        self.reserve(bingo=self.bingo[0].pk);self.qualify(bingo='qualified')
        self.bingo[0].points=90;self.bingo[0].save()
        response=self.route();self.assertContains(response,'90 configured points');self.assertContains(response,'25 points earned')

    def test_positive_completion_bonus_uses_stored_earned_value(self):
        PromptCategory.objects.filter(kind='bingo').update(completion_bonus=100)
        self.complete()
        PromptCategory.objects.filter(kind='bingo').update(completion_bonus=900)
        response=self.route();self.assertContains(response,'Configured completion bonus: 900 points.');self.assertContains(response,'Earned: 100 points.')
