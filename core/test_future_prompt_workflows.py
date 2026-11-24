from datetime import date
from django.test import TestCase
from . import test_prompt_foundations as fixtures
from .models import (ChallengeMonth,ChallengeStaffAssignment,MonthEnrollment,Team,TeamAssignment,
                     BookSubmission,PromptClaim,ModifierProvenance)
from .prompt_reservations import reservations_ready,category_available,replace_selections
from .prompt_scoring import review_prompts


class FutureChallengePromptTests(TestCase):
    setUp=fixtures.PromptFoundationTests.setUp
    token=fixtures.PromptFoundationTests.token
    prompt=fixtures.PromptFoundationTests.prompt
    settings=fixtures.PromptFoundationTests.settings

    def test_independent_new_challenges_no_month_or_environment_activation(self):
        self.assertTrue(reservations_ready())
        cases=[('bingo',),('scavenger',),('bingo','scavenger'),()]
        baseline=self.month
        for index,kinds in enumerate(cases):
            with self.subTest(categories=kinds):
                self.month=ChallengeMonth.objects.create(group=self.group,name='Future '+str(index),status='upcoming',starts_on=date(2028,5,1),ends_on=date(2028,5,31))
                ChallengeStaffAssignment.objects.create(month=self.month,membership=self.members['host'],role='host')
                team=Team.objects.create(month=self.month,name='Future Team')
                MonthEnrollment.objects.create(month=self.month,participant=self.members['reader'])
                TeamAssignment.objects.create(month=self.month,participant=self.members['reader'],team=team)
                prompts={}
                for kind in kinds:
                    prompts[kind]=self.prompt(kind=kind);self.settings(kind=kind,enabled=True,completion_bonus=100)
                    self.assertFalse(category_available(self.month,kind))
                ChallengeMonth.objects.filter(pk=self.month.pk).update(status='active');self.month.refresh_from_db()
                for kind in ['bingo','scavenger']:self.assertEqual(category_available(self.month,kind),kind in kinds)
                book=BookSubmission.objects.create(month=self.month,participant=self.members['reader'],title='Future Book',author='A',book_format='ebook',completed_on=date(2028,5,2),submitted_pages=100,status='approved',approved_pages=100)
                if prompts:
                    replace_selections(submission=book,actor=self.actor,selections={k:p.pk for k,p in prompts.items()})
                    review_prompts(book,self.actor,{k:'qualified' for k in kinds})
                    self.assertEqual(PromptClaim.objects.filter(submission=book,state='qualified').count(),len(kinds))
                    self.assertEqual(ModifierProvenance.objects.filter(submission=book).count(),len(kinds))
                else:self.assertFalse(PromptClaim.objects.filter(submission=book).exists())
        self.assertFalse(baseline.prompt_categories.exists())
