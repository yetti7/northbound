"""Filtered Team reporting uses existing credit attribution and visibility."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from django.urls import reverse

from .models import ChallengeMonth, ChallengeStaffAssignment, Membership, MonthEnrollment, TeamAssignment, BookSubmission, ModifierProvenance
from .permissions import can_view_reader_scores, can_view_team_standings
from .score_aggregation import challenge_team_totals
from . import test_team_detail_reporting as fixtures


class TeamTotalReportingTests(TestCase):
    setUp = fixtures.TeamDetailReportingTests.setUp
    member = fixtures.TeamDetailReportingTests.member
    submission = fixtures.TeamDetailReportingTests.submission
    get_as = fixtures.TeamDetailReportingTests.get_as
    add_progress = fixtures.TeamDetailReportingTests.add_progress

    def test_modes_card_format_and_presentation_filters(self):
        self.alpha_submission.approved_pages = 123456
        self.alpha_submission.save(update_fields=['approved_pages'])
        before = list(BookSubmission.objects.values()), list(ModifierProvenance.objects.values())
        for bonus, total, label in [('0', 123956, 'Base Pages'), ('1', 124016, 'Base + Bonus')]:
            for display in [{}, {'sort':'total', 'direction':'desc'}, {'show_average':'1', 'show_last':'1', 'show_completed':'1'}]:
                with self.subTest(bonus=bonus, display=display):
                    response = self.get_as(self.host_user, {'bonuses':bonus, **display})
                    self.assertEqual(response.context['team_total'], total)
                    html = response.content.decode().split('data-team-total-card>', 1)[1].split('</div>',1)[0]
                    self.assertEqual(html.count('<strong'), 1)
                    self.assertEqual(html.count('<small'), 1)
                    self.assertIn(f'>{total:,}</strong>', html)
                    self.assertIn(f'<small>{label}</small>', html)
                    self.assertNotIn('Filtered roster', html)
                    self.assertNotIn('historical', html)
        self.assertEqual((list(BookSubmission.objects.values()), list(ModifierProvenance.objects.values())), before)

    def test_progress_filters_and_reset(self):
        self.add_progress()
        for filter_name in ['themes', 'tbr', 'botm']:
            for bonus, expected in [('0',360),('1',380)]:
                response = self.get_as(self.host_user, {filter_name:'complete', 'bonuses':bonus})
                self.assertEqual(response.context['team_total'], expected)
                self.assertEqual(sum(row['pages'] for row in response.context['team_report_data']), expected)
        self.assertEqual(self.get_as(self.host_user, {'themes':'complete','tbr':'none'}).context['team_total'], 0)
        self.assertEqual(self.get_as(self.host_user).context['team_total'], 720)
        self.assertEqual(self.get_as(self.host_user, {'from':'2026-08-05','to':'2026-08-10','bonuses':'0'}).context['team_total'], 400)
        self.assertEqual(self.get_as(self.host_user, {'from':'2026-08-05','to':'2026-08-10','bonuses':'1'}).context['team_total'], 440)

    def test_historical_credit_follows_filters_and_transfer_without_double_count(self):
        self.add_progress()
        enrollment = self.month.enrollments.get(participant=self.alpha)
        enrollment.is_active = False; enrollment.credited_team = self.team
        enrollment.save(update_fields=['is_active','credited_team'])
        TeamAssignment.objects.filter(month=self.month, participant=self.alpha).update(ended_at=timezone.now())
        before = list(BookSubmission.objects.values()), list(ModifierProvenance.objects.values())
        default = self.get_as(self.host_user)
        self.assertEqual(default.context['full_roster_count'], 2)
        self.assertNotIn(self.alpha.pk, [row.participant_id for row in default.context['roster']])
        self.assertEqual(default.context['team_total'], 720)
        self.assertEqual(default.context['team_total'], challenge_team_totals(month=self.month,team_ids=[self.team.pk])[self.team.pk])
        self.assertEqual(self.get_as(self.host_user, {'bonuses':'0'}).context['team_total'], 660)
        for name in ['themes','tbr','botm']:
            response = self.get_as(self.host_user, {name:'complete'})
            self.assertEqual(response.context['team_total'], 380)
            self.assertEqual(response.context['roster'], [])
            self.assertEqual(len(response.context['team_report_data']), 1)
            self.assertIn(self.alpha_user.public_name, response.context['team_report_data'][0]['names'])
        self.assertEqual(self.get_as(self.host_user, {'from':'2026-08-05','to':'2026-08-10'}).context['team_total'], 440)
        other = self.month.teams.exclude(pk=self.team.pk).get()
        TeamAssignment.objects.filter(month=self.month,participant=self.beta).update(team=other)
        self.assertEqual(self.get_as(self.host_user).context['team_total'],380)
        totals=challenge_team_totals(month=self.month,team_ids=[self.team.pk,other.pk])
        self.assertEqual(totals,{self.team.pk:380,other.pk:340})
        self.assertEqual((list(BookSubmission.objects.values()),list(ModifierProvenance.objects.values())),before)

    def test_all_roles_obey_existing_visibility_and_filter_scope(self):
        users=[self.ordinary_user,self.leader_user,self.host_user]
        for role in ['floater','owner','moderator','delegated-moderator','platform']:
            user = get_user_model().objects.create_superuser('total-'+role) if role=='platform' else get_user_model().objects.create_user('total-'+role)
            if role!='platform':
                member=self.member(user,role)
                if role=='floater':ChallengeStaffAssignment.objects.create(month=self.month,membership=member,role='floater')
                if 'moderator' in role or role=='owner':
                    member.role='moderator' if 'moderator' in role else 'owner'
                    if role=='delegated-moderator':member.permission_overrides={'manage_months':True}
                    member.save()
            users.append(user)
        for level in ChallengeMonth.CompetitionVisibility.values:
            self.month.reader_scores_visibility=level;self.month.team_standings_visibility=level;self.month.save()
            for user in users:
                with self.subTest(level=level,user=user.username):
                    allowed=can_view_team_standings(user,self.month,team=self.team)
                    scores=can_view_reader_scores(user,self.month,team=self.team)
                    response=self.get_as(user, {'from':'2026-08-05','to':'2026-08-10','bonuses':'0'})
                    self.assertEqual(response.context['team_total'],400 if allowed else None)
                    self.assertEqual(response.context['team_report_data'] is not None, allowed and scores)
                    self.assertEqual('data-team-total>' in response.content.decode(),allowed)
                    if not allowed:self.assertNotContains(response,'id="team-report-data"')
        MonthEnrollment.objects.create(month=self.month,participant=self.host)
        TeamAssignment.objects.create(month=self.month,participant=self.host,team=self.team)
        other=self.month.teams.exclude(pk=self.team.pk).get()
        for access in ['restricted','unrestricted',None]:
            self.month.host_review_access=access;self.month.reader_scores_visibility='hosts';self.month.team_standings_visibility='hosts';self.month.save()
            response=self.get_as(self.host_user,{'bonuses':'0'})
            self.assertEqual(response.context['team_total'],600)
            self.client.force_login(self.host_user)
            response=self.client.get(reverse("team-detail",args=[self.group.slug,self.month.pk,other.pk]))
            self.assertEqual(response.context['team_total'],None if access in ['restricted',None] else 0)

    def test_standings_only_has_aggregate_filters_without_reader_score_payload(self):
        self.month.reader_scores_visibility='hosts'
        self.month.team_standings_visibility='everybody';self.month.save()
        for bonus,expected in [('0',400),('1',440)]:
            response=self.get_as(self.ordinary_user,{'from':'2026-08-05','to':'2026-08-10','bonuses':bonus})
            self.assertEqual(response.context['team_total'],expected)
            self.assertContains(response,'type="date" name="from"')
            self.assertContains(response,'type="checkbox" name="bonuses"')
            self.assertNotContains(response,'<th>Base</th>',html=True)
            self.assertNotContains(response,'id="team-report-data"')
