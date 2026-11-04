from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import OperationalError
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from . import test_submission_hotfix as fixture
from .models import (
    BookSubmission, Membership, MonthEnrollment, ChallengeStaffAssignment,
    Team, TeamAssignment, MonthTheme, ThemeClaim, ModifierProvenance, CatalogBook, CatalogEdition, AuditEvent,
)
from .submission_corrections import correction_version
from .personal_tbr_matching import synchronize_submission


class SubmissionEditingTests(TransactionTestCase):
    setUp=fixture.SubmissionHotfixTests.setUp

    def actor(self, role='host'):
        user=get_user_model().objects.create_user('edit-'+role,is_superuser=role=='platform')
        if role!='platform':
            member=Membership.objects.create(group=self.month.group,user=user,display_name=role,
                role=role if role in ('owner','moderator') else 'member')
            if role in ('host','floater','team_leader'):
                kwargs=dict(month=self.month,membership=member,role=role)
                if role=='team_leader':
                    team=Team.objects.create(month=self.month,name='Edit team')
                    MonthEnrollment.objects.create(month=self.month,participant=member)
                    TeamAssignment.objects.create(month=self.month,team=team,participant=member)
                    TeamAssignment.objects.create(month=self.month,team=team,participant=self.reader)
                    self.team=team
                    kwargs['team']=team
                ChallengeStaffAssignment.objects.create(**kwargs)
        return user

    def submission(self, status='approved', catalog=False):
        item=BookSubmission.objects.create(month=self.month,participant=self.reader,title=self.book.title,
            author=self.book.author,book_format='ebook',completed_on=date(2026,11,3),submitted_pages=350,
            approved_pages=350 if status=='approved' else None,status=status,
            catalog_book=self.book if catalog else None,catalog_edition=self.edition if catalog else None,
            scoring_catalog_edition=self.edition if catalog else None,
            metadata_pages=350 if catalog else None,verification_method='hardcover' if catalog and status=='approved' else 'manual')
        synchronize_submission(item)
        self.edit_url=reverse('submission-edit',args=['hotfix',self.month.pk,item.pk])
        return item

    def payload(self,item,actor,**changes):
        data={'version':correction_version(item,actor),'title':item.title,'author':item.author,
              'completed_on':item.completed_on.isoformat(),'submitted_pages':item.approved_pages or item.submitted_pages,
              'edition':item.catalog_edition_id or '', 'themes':[]}
        data.update(changes)
        return data

    def test_prepopulation_reused_fields_guidance_and_no_status_input(self):
        item=self.submission(catalog=True);actor=self.actor();self.client.force_login(actor)
        theme=MonthTheme.objects.create(month=self.month,name='Claim',starts_on=self.month.starts_on,ends_on=self.month.ends_on)
        ThemeClaim.objects.create(submission=item,theme=theme,status='approved')
        response=self.client.get(self.edit_url)
        self.assertEqual(response.status_code,200)
        form=response.context['form']
        self.assertEqual(form['edition'].value(),self.edition.pk)
        self.assertEqual(form['submitted_pages'].value(),350)
        self.assertEqual(form['themes'].value(),[theme.pk])
        self.assertContains(response,'min="2026-11-01"')
        self.assertContains(response,'max="2026-11-30"')
        self.assertContains(response,'Edit Submission')
        self.assertContains(response,'Hardcover-provided reference:')
        self.assertContains(response,'not Hardcover-verified')
        self.assertNotIn('status',form.fields)
        self.assertNotIn('notes',form.fields)
        self.assertNotContains(response,'View correction audit history')

    def test_authorization_roles_get_post_and_current_team_scope(self):
        item=self.submission()
        self.client.force_login(self.reader.user)
        self.assertEqual(self.client.get(self.edit_url).status_code,403)
        self.assertEqual(self.client.post(self.edit_url,{}).status_code,403)
        for role in ('owner','host','floater','platform','team_leader'):
            actor=self.actor(role);self.client.force_login(actor)
            self.assertEqual(self.client.get(self.edit_url).status_code,200)
            item.refresh_from_db()
            self.assertEqual(self.client.post(self.edit_url,self.payload(item,actor,title='Corrected by '+role)).status_code,302)
            if role=='team_leader':
                TeamAssignment.objects.filter(team=self.team,participant=self.reader).update(ended_at=timezone.now())
                self.assertEqual(self.client.get(self.edit_url).status_code,403)
                self.assertEqual(self.client.post(self.edit_url,{}).status_code,403)
        moderator=self.actor('moderator');self.client.force_login(moderator)
        self.assertEqual(self.client.get(self.edit_url).status_code,403)

    def test_active_finalizing_and_readonly_lifecycle(self):
        item=self.submission();actor=self.actor();self.client.force_login(actor)
        for status in ('active','finalizing'):
            self.month.__class__.objects.filter(pk=self.month.pk).update(status=status)
            self.assertEqual(self.client.get(self.edit_url).status_code,200)
        for status in ('upcoming','completed','archived'):
            self.month.__class__.objects.filter(pk=self.month.pk).update(status=status)
            self.assertEqual(self.client.get(self.edit_url).status_code,403)
            self.assertEqual(self.client.post(self.edit_url,{}).status_code,403)

    def test_edition_preserved_without_downgrading_verified_pages(self):
        item=self.submission(catalog=True);actor=self.actor();self.client.force_login(actor)
        data=self.payload(item,actor,title='Corrected title',edition='')
        with patch('core.views._staff_tbr_search_token') as credentials:
            response=self.client.post(self.edit_url,data,follow=True)
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'Submission corrections saved.')
        credentials.assert_not_called()
        item.refresh_from_db()
        self.assertEqual((item.catalog_edition_id,item.verification_method,item.approved_pages),(self.edition.pk,'hardcover',350))
        self.assertEqual(BookSubmission.objects.count(),1)

    def test_manual_page_override_and_no_duplicate_awards(self):
        item=self.submission(catalog=True);actor=self.actor();self.client.force_login(actor)
        response=self.client.post(self.edit_url,self.payload(item,actor,submitted_pages=400),follow=True)
        self.assertContains(response,'Submission corrections saved.')
        item.refresh_from_db()
        self.assertEqual((item.approved_pages,item.metadata_pages,item.verification_method),(400,350,'manual'))
        self.assertEqual(item.catalog_edition_id,self.edition.pk)
        self.assertEqual(BookSubmission.objects.count(),1)
        self.assertEqual(ModifierProvenance.objects.filter(submission=item,is_active=True).count(),1)

    def test_theme_changes_preserve_claim_history(self):
        item=self.submission();actor=self.actor();self.client.force_login(actor)
        theme=MonthTheme.objects.create(month=self.month,name='25%',starts_on=self.month.starts_on,ends_on=self.month.ends_on,bonus_percentage=25)
        response=self.client.post(self.edit_url,self.payload(item,actor,themes=[theme.pk]))
        self.assertEqual(response.status_code,302)
        claim=ThemeClaim.objects.get(submission=item,theme=theme);pk=claim.pk
        self.assertEqual(claim.approved_bonus_pages,88)
        item.refresh_from_db()
        self.client.post(self.edit_url,self.payload(item,actor,themes=[],confirm_theme_removal=True))
        claim.refresh_from_db();self.assertEqual((claim.pk,claim.status),(pk,'rejected'))
        self.assertFalse(ModifierProvenance.objects.filter(source_reference=f'theme_claim:{pk}',is_active=True).exists())

    def test_status_transitions_and_group_owner_redirect(self):
        actor=self.actor('owner');self.client.force_login(actor)
        for status in ('pending','approved','rejected'):
            item=self.submission(status=status)
            response=self.client.post(self.edit_url,self.payload(item,actor,title='Corrected '+status,status='approved'))
            self.assertEqual(response.status_code,302)
            self.assertIn('/participants/',response.url)
            item.refresh_from_db()
            self.assertEqual(item.status,'approved' if status=='approved' else 'pending')
        self.assertEqual(BookSubmission.objects.count(),3)

    def test_inline_dates_and_rejected_out_of_window_correction(self):
        item=self.submission(status='rejected');actor=self.actor();self.client.force_login(actor)
        BookSubmission.objects.filter(pk=item.pk).update(completed_on=date(2026,10,31))
        item.refresh_from_db()
        data=self.payload(item,actor)
        response=self.client.post(self.edit_url,data)
        self.assertEqual(response.status_code,200)
        self.assertIn('completed_on',response.context['form'].errors)
        data['completed_on']='2026-11-04'
        response=self.client.post(self.edit_url,data)
        self.assertEqual(response.status_code,302)
        item.refresh_from_db();self.assertEqual(item.status,'pending')
        audit=AuditEvent.objects.get(action='submission.corrected')
        self.assertIn('rejected',audit.summary)
        self.assertIn('2026-10-31',audit.summary)

    def test_stale_permissions_and_lock_feedback(self):
        item=self.submission();actor=self.actor();self.client.force_login(actor)
        data=self.payload(item,actor)
        self.client.post(self.edit_url,data)
        response=self.client.post(self.edit_url,data)
        self.assertContains(response,'Reload before editing again')
        self.assertTrue(response.context['reload_required'])
        self.assertContains(response,'Save Corrections</button>')
        item.refresh_from_db();fresh=self.payload(item,actor)
        with patch('core.submission_corrections.correct_submission',side_effect=OperationalError('PRIVATE INTERNAL DATA')):
            response=self.client.post(self.edit_url,fresh)
        self.assertContains(response,'Another update prevented saving')
        self.assertNotContains(response,'PRIVATE INTERNAL DATA')
        with patch('core.submission_corrections.correct_submission',side_effect=PermissionDenied):
            self.assertEqual(self.client.post(self.edit_url,fresh).status_code,403)
        ChallengeStaffAssignment.objects.filter(month=self.month).update(ended_at=timezone.now())
        self.assertEqual(self.client.post(self.edit_url,fresh).status_code,403)

    def test_edit_actions_and_audit_visibility(self):
        item=self.submission();actor=self.actor();self.client.force_login(actor)
        review=reverse('submission-review',args=['hotfix',self.month.pk,item.pk])
        self.assertContains(self.client.get(review),self.edit_url)
        participant=reverse('challenge-participant-detail',args=['hotfix',self.month.pk,self.reader.pk])
        self.assertContains(self.client.get(participant),self.edit_url)
        self.client.force_login(self.reader.user)
        self.assertNotContains(self.client.get(self.month.get_absolute_url()),self.edit_url)
        platform=self.actor('platform');self.client.force_login(platform)
        self.assertContains(self.client.get(self.edit_url),'View correction audit history')

    def test_saved_edition_change_and_missing_pages_feedback(self):
        item=self.submission(catalog=True);actor=self.actor();self.client.force_login(actor)
        work=CatalogBook.objects.create(provider='hardcover',provider_book_id='101',title='Different book',author='Writer')
        edition=CatalogEdition.objects.create(provider='hardcover',provider_edition_id='202',book=work,format_name='Ebook')
        data=self.payload(item,actor,edition=edition.pk)
        with patch('core.views._staff_tbr_search_token',return_value='PRIVATE CREDENTIAL'),patch(
                'core.submission_corrections.fetch_edition_metadata',return_value={'title':'Fresh title','author':'Fresh author','page_count':None}):
            response=self.client.post(self.edit_url,data)
            self.assertIn('submitted_pages',response.context['form'].errors)
            self.assertContains(response,'explicit manual page override')
            self.assertNotContains(response,'PRIVATE CREDENTIAL')
            item.refresh_from_db();self.assertEqual(item.catalog_edition_id,self.edition.pk)
            data['manual_page_override']='on'
            response=self.client.post(self.edit_url,data)
        self.assertEqual(response.status_code,302)
        item.refresh_from_db()
        self.assertEqual(item.catalog_edition_id,edition.pk)
        self.assertEqual(item.title,'Fresh title')
        self.assertEqual(item.author,'Fresh author')
        self.assertEqual(item.verification_method,'manual')
        self.assertEqual(BookSubmission.objects.count(),1)

    def test_invalid_edition_inline_and_prompt_claim_history(self):
        item=self.submission();actor=self.actor();self.client.force_login(actor)
        data=self.payload(item,actor,edition=999999)
        response=self.client.post(self.edit_url,data)
        self.assertIn('edition',response.context['form'].errors)
        theme=MonthTheme.objects.create(month=self.month,name='Prompt',prompt='Explain the selection',
            starts_on=self.month.starts_on,ends_on=self.month.ends_on,bonus_percentage=25)
        data=self.payload(item,actor,themes=[theme.pk])
        response=self.client.post(self.edit_url,data)
        self.assertContains(response,'Answer the Theme prompt')
        data[f'theme_response_{theme.pk}']='Original explanation'
        self.assertEqual(self.client.post(self.edit_url,data).status_code,302)
        item.refresh_from_db()
        response=self.client.get(self.edit_url)
        self.assertTrue(response.context['form'].fields[f'theme_response_{theme.pk}'].disabled)
        data=self.payload(item,actor,themes=[theme.pk])
        data[f'theme_response_{theme.pk}']='Unrequested answer rewrite'
        self.client.post(self.edit_url,data)
        self.assertEqual(ThemeClaim.objects.get(submission=item,theme=theme).response,'Original explanation')

    def test_cross_group_and_queue_action_visibility(self):
        item=self.submission(status='pending');actor=self.actor('floater');self.client.force_login(actor)
        queue=reverse('review-queue',args=['hotfix',self.month.pk])
        self.assertContains(self.client.get(queue),self.edit_url)
        self.assertEqual(self.client.get(reverse('submission-edit',args=['foreign',self.month.pk,item.pk])).status_code,404)
        self.assertEqual(self.client.post(reverse('submission-edit',args=['foreign',self.month.pk,item.pk]),{}).status_code,404)
        self.month.__class__.objects.filter(pk=self.month.pk).update(status='completed')
        self.assertNotContains(self.client.get(self.month.get_absolute_url()),self.edit_url)

    def test_missing_edit_session_and_internal_validation_are_safe(self):
        item=self.submission();actor=self.actor();self.client.force_login(actor)
        data=self.payload(item,actor)
        data.pop('version')
        response=self.client.post(self.edit_url,data)
        self.assertContains(response,'The edit session is unavailable')
        self.assertTrue(response.context['reload_required'])
        with patch('core.submission_corrections.correct_submission',side_effect=ValidationError('Frozen provenance PRIVATE DETAILS')):
            response=self.client.post(self.edit_url,self.payload(item,actor))
        self.assertContains(response,'Ask a Host to review the bonus history')
        self.assertNotContains(response,'PRIVATE DETAILS')
        self.assertFalse(AuditEvent.objects.filter(action='submission.corrected').exists())
