from datetime import date
from django.test import TestCase
from django.urls import reverse
from . import test_prompt_foundations as fixture
from .models import (BookSubmission,MonthTheme,ThemeClaim,ChallengeMonth,Membership,MonthEnrollment,TeamAssignment,UserProfile)
from .prompt_reservations import replace_selections
from .immersive import request_immersive
from .photo_submissions import create_photo_submission
from .review_attention import challenge_attention_count


class ReviewQueueGroupingTests(TestCase):
    token=fixture.PromptFoundationTests.token
    prompt=fixture.PromptFoundationTests.prompt
    settings=fixture.PromptFoundationTests.settings

    def setUp(self):
        fixture.PromptFoundationTests.setUp(self)
        self.reader=self.members['reader']
        self.bingo=self.prompt()
        self.scavenger=self.prompt(kind='scavenger')
        self.photo_prompt=self.prompt(kind='scavenger',number=2,submission_type='photo')
        self.settings(enabled=True);self.settings(kind='scavenger',enabled=True)
        ChallengeMonth.objects.filter(pk=self.month.pk).update(immersive_enabled=True)
        self.month.refresh_from_db();self.book.month=self.month
        replace_selections(submission=self.book,actor=self.actor,selections={'bingo':self.bingo.pk,'scavenger':self.scavenger.pk})
        for n in range(2):
            theme=MonthTheme.objects.create(month=self.month,name='Theme '+str(n),starts_on=date(2027,1,1),ends_on=date(2027,1,31))
            ThemeClaim.objects.create(submission=self.book,theme=theme,status='pending')
        request_immersive(submission=self.book,actor=self.reader.user)
        self.photo=create_photo_submission(month_id=self.month.pk,actor=self.reader.user,prompt_id=self.photo_prompt.pk,reader_confirmed=True)
        self.second=self.pending(self.reader,'Long title '+('a very long title '*16))
        self.other=Membership.objects.create(group=self.group,user=self.users['outsider'],display_name='Other Reader')
        MonthEnrollment.objects.create(month=self.month,participant=self.other)
        TeamAssignment.objects.create(month=self.month,participant=self.other,team=self.other_team)
        self.other_book=self.pending(self.other,'Outside Team book')
        UserProfile.objects.create(user=self.reader.user,discord_username='Current Display Name')
        self.url=reverse('review-queue',args=[self.group.slug,self.month.pk])

    def pending(self,member,title):
        return BookSubmission.objects.create(month=self.month,participant=member,title=title,author='Writer',book_format='ebook',completed_on=date(2027,1,4),submitted_pages=100)

    def response(self,user=None):
        self.client.force_login(user or self.actor)
        return self.client.get(self.url)

    def test_item_counts_deduplication_and_photo_separation(self):
        response=self.response();self.assertEqual(response.status_code,200)
        self.assertEqual(response.context['queue_total'],8)
        self.assertEqual(response.context['queue_total'],challenge_attention_count(self.actor,self.month))
        groups=response.context['queue_groups'];self.assertEqual(len(groups),2)
        self.assertEqual(sum(g['count'] for g in groups),8)
        self.assertEqual(sum(r['count'] for g in groups for r in g['readers']),8)
        rows=[row for g in groups for r in g['readers'] for row in r['rows']]
        self.assertEqual(len(rows),4)
        self.assertEqual(len([r for r in rows if r['kind']=='book' and r['book'].pk==self.book.pk]),1)
        self.assertEqual(next(r for r in rows if r['kind']=='book' and r['book'].pk==self.book.pk)['count'],5)
        self.assertEqual(len([r for r in rows if r['kind']=='photo']),1)
        self.assertContains(response,'Current Display Name')
        self.assertContains(response,'Review Photo');self.assertContains(response,'Review &amp; Scoring')

    def test_scope_matrix_and_backend_setting(self):
        for role,expected in [('leader',7),('participating',7),('host',8),('floater',8)]:
            response=self.response(self.users[role]);self.assertEqual(response.status_code,200)
            self.assertEqual(response.context['queue_total'],expected)
            self.assertEqual(expected,challenge_attention_count(self.users[role],self.month))
            if expected==7:self.assertNotContains(response,'Outside Team book')
        self.assertEqual(self.response(self.users['moderator']).status_code,403)
        ChallengeMonth.objects.filter(pk=self.month.pk).update(host_review_access='unrestricted')
        response=self.response(self.users['participating']);self.assertEqual(response.context['queue_total'],8)
        self.assertContains(response,'Outside Team book')
        self.assertEqual(self.response(self.users['leader']).context['queue_total'],7)

    def test_unassigned_work_survives_without_leaking_to_team_scoped_reviewers(self):
        TeamAssignment.objects.filter(month=self.month,participant=self.other).delete()
        response=self.response();self.assertEqual(response.context['queue_total'],8)
        self.assertContains(response,'Unassigned or historical Team')
        self.assertNotContains(self.response(self.users['leader']),'Outside Team book')

    def test_completed_immersive_only_and_empty_queue(self):
        ChallengeMonth.objects.filter(pk=self.month.pk).update(status='completed')
        self.month.refresh_from_db();response=self.response()
        self.assertEqual(response.context['queue_total'],1)
        self.assertEqual(response.context['queue_total'],challenge_attention_count(self.actor,self.month))
        self.assertContains(response,'Approve Immersive')
        from .models import ImmersiveRequest
        ImmersiveRequest.objects.filter(submission__month=self.month).update(status='withdrawn')
        response=self.response();self.assertEqual(response.context['queue_total'],0)
        self.assertContains(response,'No pending reviews.')
