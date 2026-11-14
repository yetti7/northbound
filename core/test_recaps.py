import json
from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from .models import (ReadingGroup, Membership, ChallengeMonth, MonthEnrollment, Team, TeamAssignment,
    ChallengeStaffAssignment, BookSubmission, ModifierProvenance, MonthTheme, ThemeClaim,
    UserProfile, CatalogBook, CatalogEdition, ImmersiveRequest, AuditEvent,
    PersonalTBR, PersonalTBRBook, PersonalTBRMatch, PersonalTBRCompletionAward, BotmBook, BotmMatch, BotmCompletionAward)
from .recaps import build_recap, period_bounds


class RecapTests(TestCase):
    def setUp(self):
        User=get_user_model()
        self.admin=User.objects.create_user('recap-admin',is_superuser=True)
        self.reader=User.objects.create_user('recap-reader')
        self.host=User.objects.create_user('recap-host')
        self.leader=User.objects.create_user('recap-leader')
        self.other=User.objects.create_user('forbidden-reader')
        self.outsider=User.objects.create_user('unrelated-reader')
        for user in [self.admin,self.reader,self.host,self.leader,self.other,self.outsider]:UserProfile.objects.create(user=user)
        self.group=ReadingGroup.objects.create(name='Reading Group',slug='recap-group',timezone='America/New_York')
        self.foreign=ReadingGroup.objects.create(name='Secret foreign Group',slug='recap-foreign')
        self.month=ChallengeMonth.objects.create(group=self.group,name='November recap',status='active',
            starts_on=date(2026,11,1),ends_on=date(2026,11,30),reader_scores_visibility='hosts',team_standings_visibility='hosts')
        self.members={}
        for user,role in [(self.reader,'member'),(self.host,'owner'),(self.leader,'member'),(self.other,'member')]:
            member=Membership.objects.create(group=self.group,user=user,display_name=user.username,role=role)
            MonthEnrollment.objects.create(month=self.month,participant=member)
            self.members[user.pk]=member
        self.team=Team.objects.create(month=self.month,name='Team A')
        self.team_b=Team.objects.create(month=self.month,name='Forbidden violet team')
        for user in [self.reader,self.host,self.leader,self.other]:
            TeamAssignment.objects.create(month=self.month,participant=self.members[user.pk],team=self.team_b if user==self.other else self.team)
        ChallengeStaffAssignment.objects.create(month=self.month,membership=self.members[self.host.pk],role='host')
        ChallengeStaffAssignment.objects.create(month=self.month,membership=self.members[self.leader.pk],role='team_leader',team=self.team)
        foreign_member=Membership.objects.create(group=self.foreign,user=self.outsider,role='member',display_name='Foreign reader')
        foreign_month=ChallengeMonth.objects.create(group=self.foreign,name='Secret foreign Challenge',status='completed',starts_on=self.month.starts_on,ends_on=self.month.ends_on)
        MonthEnrollment.objects.create(month=foreign_month,participant=foreign_member)
        BookSubmission.objects.create(month=foreign_month,participant=foreign_member,title='Secret foreign book',author='Author',book_format='ebook',completed_on=date(2026,11,3),submitted_pages=888777,approved_pages=888777,status='approved')
        self.book=self.make_book(self.reader,100)
        self.secret=self.make_book(self.other,978321)
        self.url=reverse('recap-dashboard')

    def make_book(self,user,pages,month=None,completed=None,**kwargs):
        return BookSubmission.objects.create(month=month or self.month,participant=self.members[user.pk],
            title='Private title',author='Author',book_format='ebook',completed_on=completed or date(2026,11,3),
            submitted_pages=pages,approved_pages=pages,status='approved',final_scored_pages=999999,**kwargs)

    def cards(self,result,index=0):
        return {c['key']:c['value'] for c in result['sections'][index]['cards']}

    def status(self,status):
        ChallengeMonth.objects.filter(pk=self.month.pk).update(status=status)
        self.month.refresh_from_db()

    def test_personal_active_recap_uses_authoritative_points_not_cache(self):
        ModifierProvenance.objects.create(month=self.month,participant=self.members[self.reader.pk],submission=self.book,
            source_type='legacy_modifier',source_reference='fixture:1',source_label='Fixture',amount=25,effective_date=date(2026,11,5))
        result=build_recap(viewer=self.reader)
        self.assertEqual(result['sections'][0]['mode'],'Progress Snapshot')
        cards=self.cards(result)
        self.assertEqual((cards['entries'],cards['pages'],cards['base_pages'],cards['modifier_pages'],cards['total_pages']),(1,100,100,25,125))
        self.assertNotIn('978321',json.dumps(result));self.assertNotIn('Private title',json.dumps(result))

    def test_lifecycle_modes_and_unavailable_stages(self):
        for status,mode in [('active','Progress Snapshot'),('completed','Final Wrapped'),('archived','Final Wrapped')]:
            self.status(status);self.assertEqual(build_recap(viewer=self.reader)['sections'][0]['mode'],mode)
        for status in ['draft','upcoming','finalizing']:
            self.status(status);self.assertEqual(build_recap(viewer=self.reader)['sections'],[])

    def test_active_aggregate_statistics_are_not_calculated_for_reader_or_host_owner(self):
        for viewer in [self.reader,self.host]:
            with patch('core.recaps.challenge_score_totals') as scores:
                result=build_recap(viewer=viewer,perspective='challenge',challenge_id=self.month.pk)
                self.assertTrue(all(not section['cards'] for section in result['sections']));scores.assert_not_called()
                self.assertNotIn('978321',json.dumps(result))
        result=build_recap(viewer=self.admin,perspective='challenge',challenge_id=self.month.pk)
        self.assertEqual(self.cards(result)['total_pages'],978421)

    def test_completed_challenge_public_results_do_not_grant_private_reader_recaps(self):
        self.status('completed')
        result=build_recap(viewer=self.reader,perspective='challenge',challenge_id=self.month.pk)
        self.assertEqual(self.cards(result)['entries'],2)
        self.assertEqual(len(result['sections'][0]['teams']),2)
        self.assertNotIn('themes',self.cards(result))
        with self.assertRaises(PermissionDenied):build_recap(viewer=self.reader,target_id=self.other.pk)

    def test_personal_history_after_membership_ends_hides_group_metadata(self):
        self.status('completed')
        member=self.members[self.reader.pk];member.is_active=False;member.save(update_fields=['is_active'])
        self.group.name='Secret renamed Group';self.group.save(update_fields=['name'])
        result=build_recap(viewer=self.reader)
        self.assertEqual(self.cards(result)['pages'],100)
        text=json.dumps(result);self.assertNotIn('Secret renamed Group',text);self.assertNotIn('November recap',text)
        self.assertNotIn('Forbidden violet team',text)
        self.client.force_login(self.reader)
        page=self.client.get(self.url)
        self.assertNotContains(page,'Secret renamed Group');self.assertNotContains(page,'November recap')
        self.assertContains(page,'Personal history')
        self.assertEqual(build_recap(viewer=self.reader,perspective='challenge')['sections'],[])

    def test_host_owner_and_leader_scope_private_reader_data(self):
        for viewer in [self.host,self.leader]:
            # Participating Host and Team Leader see assigned-team staff-only data when the audience permits it.
            self.month.reader_scores_visibility='hosts_leaders';self.month.team_standings_visibility='hosts_leaders'
            self.month.save(update_fields=['reader_scores_visibility','team_standings_visibility'])
            result=build_recap(viewer=viewer,perspective='team',target_id=self.team.pk)
            text=json.dumps(result);self.assertNotIn('978321',text);self.assertNotIn('forbidden-reader',text)
            with self.assertRaises(PermissionDenied):build_recap(viewer=viewer,target_id=self.other.pk)
            with self.assertRaises(PermissionDenied):build_recap(viewer=viewer,perspective='team',target_id=self.team_b.pk)

    def test_team_standings_and_reader_scores_are_independent(self):
        self.month.team_standings_visibility='everybody';self.month.save(update_fields=['team_standings_visibility'])
        result=build_recap(viewer=self.reader,perspective='team',target_id=self.team_b.pk)
        self.assertEqual(self.cards(result)['team_points'],978321)
        self.assertEqual(result['sections'][0]['contributions'],[])
        self.assertNotIn('entries',self.cards(result))
        self.month.team_standings_visibility='hosts';self.month.reader_scores_visibility='hosts_leaders'
        self.month.save(update_fields=['team_standings_visibility','reader_scores_visibility'])
        result=build_recap(viewer=self.leader,perspective='team',target_id=self.team.pk)
        self.assertNotIn('team_points',self.cards(result))
        self.assertTrue(result['sections'][0]['contributions'])

    def test_forged_filters_cross_group_reader_team_and_http_responses(self):
        for kwargs in [{'group_id':self.foreign.pk},{'target_id':self.outsider.pk},
                       {'perspective':'team','target_id':self.team_b.pk}]:
            with self.assertRaises(PermissionDenied):build_recap(viewer=self.reader,**kwargs)
        self.client.force_login(self.host)
        response=self.client.get(self.url,{'perspective':'team','team':self.team_b.pk})
        self.assertContains(response,'Select a valid choice')
        self.assertNotContains(response,'978321');self.assertNotContains(response,'Forbidden violet team')
        self.assertIn('private',response['Cache-Control'])
        self.assertIn('no-store',response['Cache-Control'])
        self.client.logout();self.assertEqual(self.client.get(self.url).status_code,302)

    def test_custom_ranges_use_completion_and_modifier_effective_dates(self):
        ModifierProvenance.objects.create(month=self.month,participant=self.members[self.reader.pk],submission=self.book,
            source_type='legacy_modifier',source_reference='range',source_label='Fixture',amount=40,effective_date=date(2026,11,20))
        early=self.cards(build_recap(viewer=self.reader,period='custom',start=date(2026,11,3),end=date(2026,11,3)))
        self.assertEqual((early['entries'],early['base_pages'],early['modifier_pages']),(1,100,0))
        late=self.cards(build_recap(viewer=self.reader,period='custom',start=date(2026,11,20),end=date(2026,11,20)))
        self.assertEqual((late['entries'],late['base_pages'],late['modifier_pages']),(0,0,40))
        with self.assertRaises(ValidationError):build_recap(viewer=self.reader,period='custom',start=date(2026,11,4),end=date(2026,11,3))

    def test_calendar_ranges_and_group_local_today(self):
        for period,anchor,expected in [('month',date(2024,2,4),(date(2024,2,1),date(2024,2,29))),
            ('quarter',date(2026,11,3),(date(2026,10,1),date(2026,12,31))),
            ('year',date(2026,11,3),(date(2026,1,1),date(2026,12,31)))]:
            self.assertEqual(period_bounds(period,group=self.group,anchor=anchor),expected)
        with patch('core.recaps.timezone.localdate',return_value=date(2026,11,30)) as local:
            period_bounds('month',group=self.group)
            self.assertEqual(str(local.call_args.kwargs['timezone']),'America/New_York')

    def test_cross_challenge_entries_points_and_trusted_catalog_work_dedup(self):
        second=ChallengeMonth.objects.create(group=self.group,name='Other recap',status='completed',starts_on=self.month.starts_on,ends_on=self.month.ends_on)
        other_book=self.make_book(self.reader,120,month=second)
        work=CatalogBook.objects.create(provider='hardcover',provider_book_id='same-work',title='Work',author='Author')
        for book in [self.book,other_book]:
            book.catalog_book=work;book.save(update_fields=['catalog_book'])
        result=build_recap(viewer=self.reader)
        total={c['key']:c['value'] for c in result['summary']}
        self.assertEqual((total['entries'],total['pages'],total['total_pages'],total['works']),(2,220,220,1))
        filtered=build_recap(viewer=self.reader,challenge_id=second.pk)
        self.assertEqual(len(filtered['sections']),1);self.assertEqual(self.cards(filtered)['pages'],120)

    def test_transfer_withdrawal_and_reactivation_follow_authoritative_team_attribution(self):
        result=build_recap(viewer=self.admin,perspective='team',target_id=self.team.pk)
        self.assertEqual(self.cards(result)['team_points'],100)
        assignment=TeamAssignment.objects.get(participant=self.members[self.reader.pk],month=self.month,ended_at__isnull=True)
        assignment.ended_at=timezone.now();assignment.save(update_fields=['ended_at'])
        moved=TeamAssignment.objects.create(month=self.month,participant=self.members[self.reader.pk],team=self.team_b)
        enrollment=MonthEnrollment.objects.get(month=self.month,participant=self.members[self.reader.pk])
        enrollment.credited_team=self.team_b;enrollment.save(update_fields=['credited_team'])
        self.assertEqual(self.cards(build_recap(viewer=self.admin,perspective='team',target_id=self.team_b.pk))['team_points'],978421)
        moved.ended_at=timezone.now();moved.save(update_fields=['ended_at'])
        enrollment.is_active=False;enrollment.save(update_fields=['is_active'])
        self.assertEqual(self.cards(build_recap(viewer=self.admin,perspective='team',target_id=self.team_b.pk))['team_points'],978421)
        self.assertEqual(self.cards(build_recap(viewer=self.reader))['total_pages'],100)

    def test_void_late_approval_and_active_provenance(self):
        self.book.reviewed_at=timezone.now();self.book.save(update_fields=['reviewed_at'])
        theme=MonthTheme.objects.create(month=self.month,name='Fixture Theme',starts_on=self.month.starts_on,ends_on=self.month.ends_on)
        claim=ThemeClaim.objects.create(submission=self.book,theme=theme,status='approved',approved_bonus_pages=25)
        source=ModifierProvenance.objects.create(month=self.month,participant=self.members[self.reader.pk],submission=self.book,
            source_type='theme_bonus',source_reference=f'theme_claim:{claim.pk}',source_label='Fixture',amount=25,effective_date=self.book.completed_on)
        self.assertEqual(self.cards(build_recap(viewer=self.reader))['themes'],1)
        source.is_active=False;source.save(update_fields=['is_active'])
        self.assertEqual(self.cards(build_recap(viewer=self.reader))['themes'],0)
        self.book.is_removed=True;self.book.save(update_fields=['is_removed'])
        cards=self.cards(build_recap(viewer=self.reader));self.assertEqual(cards['entries'],0);self.assertEqual(cards['total_pages'],0)
        self.book.is_removed=False;self.book.save(update_fields=['is_removed'])
        source.is_active=True;source.save(update_fields=['is_active'])
        self.assertEqual(self.cards(build_recap(viewer=self.reader,period='custom',start=date(2026,11,3),end=date(2026,11,3)))['total_pages'],125)

    def test_service_is_readonly_and_response_contains_only_sanitized_cards(self):
        models=[BookSubmission,ModifierProvenance,MonthEnrollment,TeamAssignment,ThemeClaim,ImmersiveRequest,AuditEvent]
        before={model.__name__:list(model.objects.values()) for model in models}
        result=build_recap(viewer=self.admin,perspective='challenge')
        self.assertIsInstance(json.dumps(result),str)
        for model in models:self.assertEqual(before[model.__name__],list(model.objects.values()))
        self.client.force_login(self.reader)
        page=self.client.get(self.url)
        self.assertContains(page,'Progress Snapshot')
        self.assertNotContains(page,'978321');self.assertNotContains(page,'999999');self.assertNotContains(page,'Private title')
        self.assertNotContains(page,'Secret foreign Group')

    def test_empty_state_and_validation_errors(self):
        empty=get_user_model().objects.create_user('recap-empty')
        UserProfile.objects.create(user=empty)
        self.client.force_login(empty)
        self.assertContains(self.client.get(self.url),'Your next chapter awaits')
        self.client.force_login(self.reader)
        self.assertContains(self.client.get(self.url,{'period':'custom'}),'Choose a start date')
        self.assertContains(self.client.get(self.url,{'period':'custom','start':'2026-11-20','end':'2026-11-01'}),'Through date must be on or after')

    def test_stored_qualifications_zero_bonus_achievements_and_completion_provenance(self):
        member=self.members[self.reader.pk]
        enrollment=MonthEnrollment.objects.get(month=self.month,participant=member)
        tbr=PersonalTBR.objects.create(enrollment=enrollment)
        entry=PersonalTBRBook.objects.create(personal_tbr=tbr,position=1,title_snapshot='Book',author_snapshot='Author')
        tbr.confirmed_at=timezone.now();tbr.save(update_fields=['confirmed_at'])
        match=PersonalTBRMatch.objects.create(personal_tbr_book=entry,month=self.month,participant=member,submission=self.book,
            method='manual_review',status='confirmed',is_qualifying=True,tbr_title_snapshot='Book',tbr_author_snapshot='Author',
            submission_title_snapshot=self.book.title,submission_author_snapshot=self.book.author)
        botm=BotmBook.objects.create(month=self.month,position=1,title_snapshot='Book',author_snapshot='Author',page_count_snapshot=100)
        BotmMatch.objects.create(botm_book=botm,month=self.month,participant=member,submission=self.book,
            method='manual_review',status='confirmed',is_qualifying=True,botm_title_snapshot='Book',botm_author_snapshot='Author',
            submission_title_snapshot=self.book.title,submission_author_snapshot=self.book.author)
        ImmersiveRequest.objects.create(submission=self.book,requester=self.reader,captured_percent=0,status='approved',is_active=True,awarded_pages=0)
        award=PersonalTBRCompletionAward.objects.create(personal_tbr=tbr,month=self.month,participant=member,
            completion_set_fingerprint='fixture',configured_book_count=9,bonus_amount_snapshot=20,
            qualified_at=timezone.now(),last_qualified_at=timezone.now(),effective_date=date(2026,11,20))
        source=ModifierProvenance.objects.create(month=self.month,participant=member,source_type='tbr_completion',
            source_reference=f'personal_tbr_completion:{award.pk}',source_label='Fixture',amount=20,effective_date=date(2026,11,20))
        BotmCompletionAward.objects.create(month=self.month,participant=member,completion_set_fingerprint='fixture',
            configured_book_count=1,bonus_amount_snapshot=0,qualified_at=timezone.now(),last_qualified_at=timezone.now(),effective_date=self.book.completed_on)
        cards=self.cards(build_recap(viewer=self.reader))
        self.assertEqual((cards['tbr'],cards['botm'],cards['immersive'],cards['tbr_completion'],cards['botm_completion']),(1,1,1,1,1))
        early=self.cards(build_recap(viewer=self.reader,period='custom',start=date(2026,11,3),end=date(2026,11,3)))
        self.assertEqual((early['tbr'],early['tbr_completion']),(1,0))
        source.is_active=False;source.save(update_fields=['is_active'])
        self.assertEqual(self.cards(build_recap(viewer=self.reader))['tbr_completion'],0)
        match.is_qualifying=False;match.save(update_fields=['is_qualifying'])
        self.assertEqual(self.cards(build_recap(viewer=self.reader))['tbr'],0)

    def test_authorized_cross_challenge_aggregate_and_separate_team_entities(self):
        self.status('completed')
        second=ChallengeMonth.objects.create(group=self.group,name='December recap',status='archived',starts_on=date(2026,12,1),ends_on=date(2026,12,31))
        self.make_book(self.reader,120,month=second,completed=date(2026,12,2))
        aggregate=build_recap(viewer=self.reader,perspective='challenge',group_id=self.group.pk)
        summary={card['key']:card['value'] for card in aggregate['summary']}
        self.assertEqual(summary['total_pages'],978541)
        self.assertNotIn('888777',json.dumps(aggregate))
        other_team=Team.objects.create(month=second,name='Team A')
        enrollment=MonthEnrollment.objects.create(month=second,participant=self.members[self.reader.pk])
        TeamAssignment.objects.create(month=second,participant=self.members[self.reader.pk],team=other_team)
        teams=build_recap(viewer=self.admin,perspective='team',group_id=self.group.pk)
        same_names=[section for section in teams['sections'] if section['subject']=='Team A']
        self.assertEqual(len(same_names),2)
        self.assertFalse(teams['summary'])


    def test_floater_and_nonparticipating_host_respect_configured_audiences(self):
        for role in ['floater','host']:
            user=get_user_model().objects.create_user('recap-'+role+'-staff')
            member=Membership.objects.create(group=self.group,user=user,display_name='Staff')
            ChallengeStaffAssignment.objects.create(month=self.month,membership=member,role=role)
            if role=='floater':
                with self.assertRaises(PermissionDenied):build_recap(viewer=user,target_id=self.other.pk)
                self.month.reader_scores_visibility='hosts_floaters';self.month.team_standings_visibility='hosts_floaters'
                self.month.save(update_fields=['reader_scores_visibility','team_standings_visibility'])
            with self.assertRaises(PermissionDenied):build_recap(viewer=user,target_id=self.other.pk)
            result=build_recap(viewer=user,perspective='team',target_id=self.team_b.pk)
            self.assertEqual(self.cards(result)['pages'],978321)
            self.month.reader_scores_visibility='hosts';self.month.team_standings_visibility='hosts'
            self.month.save(update_fields=['reader_scores_visibility','team_standings_visibility'])

    def test_manually_named_books_are_not_fuzzily_deduplicated(self):
        self.make_book(self.reader,100)
        result=build_recap(viewer=self.reader)
        self.assertEqual(self.cards(result)['entries'],2)
        self.assertNotIn('works',self.cards(result))


    def test_public_reader_contributions_do_not_require_private_detail_or_team_standings(self):
        self.month.reader_scores_visibility='everybody';self.month.save(update_fields=['reader_scores_visibility'])
        result=build_recap(viewer=self.reader,perspective='team',target_id=self.team_b.pk)
        section=result['sections'][0]
        self.assertFalse(section['cards'])
        self.assertEqual(section['contributions'],[{'reader':self.other.username,'points':978321}])
        with self.assertRaises(PermissionDenied):build_recap(viewer=self.reader,target_id=self.other.pk)


    def test_format_cards_use_recorded_formats_without_inventing_audio_duration(self):
        audio=self.make_book(self.reader,80)
        audio.book_format='audio';audio.save(update_fields=['book_format'])
        physical=self.make_book(self.reader,90)
        physical.book_format='paperback';physical.save(update_fields=['book_format'])
        result=build_recap(viewer=self.reader)
        cards=self.cards(result)
        self.assertEqual((cards['format_audio'],cards['format_paperback'],cards['format_ebook']),(1,1,1))
        self.assertNotIn('listening_hours',json.dumps(result))


    def test_final_wrapped_reflects_latest_stored_corrections_without_cached_scores(self):
        self.status('completed')
        self.assertEqual(self.cards(build_recap(viewer=self.reader))['total_pages'],100)
        BookSubmission.objects.filter(pk=self.book.pk).update(approved_pages=150,final_scored_pages=999999)
        latest=build_recap(viewer=self.reader)
        self.assertEqual(latest['sections'][0]['mode'],'Final Wrapped')
        self.assertEqual(self.cards(latest)['total_pages'],150)


    def test_retained_team_credit_does_not_expose_departed_reader_details(self):
        self.month.reader_scores_visibility='everybody';self.month.team_standings_visibility='everybody'
        self.month.save(update_fields=['reader_scores_visibility','team_standings_visibility'])
        MonthEnrollment.objects.filter(month=self.month,participant=self.members[self.other.pk]).update(is_active=False,credited_team=self.team_b)
        # Legacy current assignments do not make an inactive Reader a public roster member.
        result=build_recap(viewer=self.reader,perspective='team',target_id=self.team_b.pk)
        self.assertEqual(self.cards(result)['team_points'],978321)
        self.assertFalse(result['sections'][0]['contributions'])
        self.assertNotIn('forbidden-reader',json.dumps(result))


    def test_switching_perspectives_ignores_stale_irrelevant_reader_target(self):
        self.client.force_login(self.admin)
        page=self.client.get(self.url,{'perspective':'team','team':self.team.pk,'reader':self.other.pk})
        self.assertEqual(page.status_code,200)
        self.assertTrue(page.context['recap']['sections'])
        self.assertNotContains(page,'Select a valid choice')

    def test_challenge_target_is_checked_and_consistent_with_filters(self):
        result=build_recap(viewer=self.admin,perspective='challenge',target_id=self.month.pk)
        self.assertEqual(len(result['sections']),1)
        foreign=ChallengeMonth.objects.get(group=self.foreign)
        with self.assertRaises(PermissionDenied):build_recap(viewer=self.reader,perspective='challenge',target_id=foreign.pk)
        with self.assertRaises(ValidationError):build_recap(viewer=self.admin,perspective='challenge',target_id=self.month.pk,challenge_id=foreign.pk)



from . import test_late_approval as late_fixture

class RecapLateApprovalTests(TestCase):
    setUp=late_fixture.LateApprovalTests.setUp
    activate=late_fixture.LateApprovalTests.activate
    assign=late_fixture.LateApprovalTests.assign
    depart=late_fixture.LateApprovalTests.depart
    submit=late_fixture.LateApprovalTests.submit
    approve=late_fixture.LateApprovalTests.approve

    def test_verified_late_approval_is_attributed_to_completion_without_writes(self):
        book=self.submit();self.depart();book=self.approve(book)
        self.assertTrue(book.late_approval_evidence)
        before=list(AuditEvent.objects.values())
        result=build_recap(viewer=self.user,period='custom',start=book.completed_on,end=book.completed_on)
        cards={card['key']:card['value'] for card in result['sections'][0]['cards']}
        self.assertEqual((cards['entries'],cards['base_pages']),(1,400))
        team=build_recap(viewer=self.owner,perspective='team',target_id=self.team.pk)
        self.assertEqual(next(card['value'] for card in team['sections'][0]['cards'] if card['key']=='team_points'),400)
        self.assertEqual(before,list(AuditEvent.objects.values()))
