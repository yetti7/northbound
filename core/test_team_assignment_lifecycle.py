from django.test import TestCase
from django.urls import reverse
from django.core.exceptions import PermissionDenied
from . import test_challenge_participant_detail as fixture
from .models import ChallengeMonth,TeamAssignment,MonthEnrollment,Membership,BookSubmission,ModifierProvenance,AuditEvent,ChallengeStaffAssignment,ParticipationPeriod
from .permissions import can_manage_team_assignments
from .participation import assign_participant_to_team,end_team_assignment,deactivate_participation
from .score_aggregation import challenge_score_totals,challenge_team_totals


class TeamAssignmentLifecycleTests(TestCase):
    setUp=fixture.ChallengeParticipantDetailTests.setUp
    detail_url=fixture.ChallengeParticipantDetailTests.detail_url

    def state(self):
        models=[TeamAssignment,MonthEnrollment,BookSubmission,ModifierProvenance,AuditEvent,ChallengeStaffAssignment,ParticipationPeriod]
        return {m.__name__:list(m.objects.order_by('pk').values()) for m in models},challenge_score_totals(month=self.month),challenge_team_totals(month=self.month,team_ids=[self.north.pk,self.south.pk])

    def status(self,value):
        ChallengeMonth.objects.filter(pk=self.month.pk).update(status=value)

    def test_allowed_stages_preserve_admin_host_owner_and_floater_transfer_authority(self):
        Membership.objects.filter(pk=self.members['other'].pk).update(role='owner')
        for status in ['draft','upcoming','active']:
            self.status(status);month=ChallengeMonth.objects.get(pk=self.month.pk)
            for role in ['platform','host','other','floater']:
                with self.subTest(status=status,role=role):
                    user=self.users[role]
                    self.assertTrue(can_manage_team_assignments(user,month,self.members['reader']))
                    assign_participant_to_team(month=self.month,participant=self.members['reader'],team=self.south,actor=user)
                    assign_participant_to_team(month=self.month,participant=self.members['reader'],team=self.north,actor=user)
                    current=TeamAssignment.objects.get(month=self.month,participant=self.members['reader'],ended_at__isnull=True)
                    end_team_assignment(assignment=current,actor=user,reason='ordinary unassignment')
                    assign_participant_to_team(month=self.month,participant=self.members['reader'],team=self.north,actor=user)
        self.assertEqual(challenge_score_totals(month=self.month)[self.members['reader'].pk]['total_pages'],125)

    def test_locked_stages_deny_direct_transfer_assignment_and_removal_without_mutation(self):
        Membership.objects.filter(pk=self.members['other'].pk).update(role='owner')
        MonthEnrollment.objects.create(month=self.month,participant=self.members['outsider'])
        assignment=TeamAssignment.objects.get(month=self.month,participant=self.members['reader'],ended_at__isnull=True)
        for status in ['finalizing','completed','archived']:
            self.status(status);month=ChallengeMonth.objects.get(pk=self.month.pk)
            for role in ['platform','host','other','floater','leader','reader']:
                before=self.state();actor=self.users[role]
                with self.subTest(status=status,role=role):
                    self.assertFalse(can_manage_team_assignments(actor,month))
                    with self.assertRaises(PermissionDenied):assign_participant_to_team(month=self.month,participant=self.members['reader'],team=self.south,actor=actor)
                    with self.assertRaises(PermissionDenied):assign_participant_to_team(month=self.month,participant=self.members['outsider'],team=self.north,actor=actor)
                    with self.assertRaises(PermissionDenied):end_team_assignment(assignment=assignment,actor=actor,reason='ordinary removal')
                    with self.assertRaises(PermissionDenied):end_team_assignment(assignment=assignment,actor=actor,reason='ordinary removal',ordinary_staff_change=True)
                    self.assertEqual(before,self.state())

    def test_locked_http_controls_and_post_endpoints(self):
        assignment=TeamAssignment.objects.get(month=self.month,participant=self.members['reader'],ended_at__isnull=True)
        for status in ['finalizing','completed','archived']:
            self.status(status)
            for role in ['platform','host','floater']:
                self.client.force_login(self.users[role]);before=self.state()
                for name,args in [('month-participant-list',[self.group.slug,self.month.pk]),('team-detail',[self.group.slug,self.month.pk,self.north.pk])]:
                    response=self.client.get(reverse(name,args=args));self.assertEqual(response.status_code,200)
                    self.assertContains(response,'Team assignments are locked after the Active stage.')
                    self.assertNotContains(response,'Edit Team');self.assertNotContains(response,'Assign Participant')
                for name,args,data in [('month-participant-edit',[self.group.slug,self.month.pk,self.reader_enrollment.pk],{'team':self.south.pk}),('team-assignment-create',[self.group.slug,self.month.pk],{'participant':self.members['reader'].pk,'team':self.south.pk}),('team-assignment-remove',[self.group.slug,self.month.pk,assignment.pk],{})]:
                    self.assertEqual(self.client.post(reverse(name,args=args),data).status_code,403)
                self.assertEqual(before,self.state())

    def test_participating_host_cross_team_transfer_and_self_restriction_survive(self):
        host=self.members['host'];MonthEnrollment.objects.create(month=self.month,participant=host)
        TeamAssignment.objects.create(month=self.month,participant=host,team=self.north)
        self.client.force_login(self.users['host'])
        url=reverse('month-participant-edit',args=[self.group.slug,self.month.pk,self.other_enrollment.pk])
        self.assertEqual(self.client.post(url,{'team':self.north.pk}).status_code,302)
        self.assertEqual(self.client.post(url,{'team':self.south.pk}).status_code,302)
        with self.assertRaises(PermissionDenied):assign_participant_to_team(month=self.month,participant=host,team=self.south,actor=self.users['host'])
        own=TeamAssignment.objects.get(month=self.month,participant=host,ended_at__isnull=True)
        with self.assertRaises(PermissionDenied):end_team_assignment(assignment=own,actor=self.users['host'],reason='self unassignment')

    def test_reader_withdrawal_preserves_its_existing_internal_end_path(self):
        enrollment=self.reader_enrollment
        before=challenge_score_totals(month=self.month)
        team_before=challenge_team_totals(month=self.month,team_ids=[self.north.pk,self.south.pk])
        deactivate_participation(enrollment=enrollment,actor=self.users['reader'],reason=MonthEnrollment.InactiveReason.WITHDRAWN)
        self.assertFalse(TeamAssignment.objects.filter(month=self.month,participant=self.members['reader'],ended_at__isnull=True).exists())
        self.assertEqual(before,challenge_score_totals(month=self.month))
        self.assertEqual(team_before,challenge_team_totals(month=self.month,team_ids=[self.north.pk,self.south.pk]))
