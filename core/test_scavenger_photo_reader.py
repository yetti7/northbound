import os
from pathlib import Path
from unittest.mock import patch
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TestCase, RequestFactory, Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from . import test_scavenger_photo_foundation as fixtures
from .models import (AuditEvent,BookSubmission,ChallengeMonth,MonthEnrollment,PromptClaim,
    ModifierProvenance,PromptCompletion,ScavengerPhotoSubmission)
from .photo_submissions import create_photo_submission,edit_pending_photo
from .photo_history import reader_photo_history


@override_settings(NORTHBOUND_PROMPT_RESERVATIONS_READY=True)
class PhotoReaderTests(TestCase):
    token=fixtures.PhotoFoundationTests.token
    prompt=fixtures.PhotoFoundationTests.prompt
    settings=fixtures.PhotoFoundationTests.settings
    protected=fixtures.PhotoFoundationTests.protected
    create=fixtures.PhotoFoundationTests.create
    transition=fixtures.PhotoFoundationTests.transition

    def setUp(self):
        fixtures.PhotoFoundationTests.setUp(self)
        self.second=self.prompt(3,kind='scavenger',submission_type='photo',description='A second Photo prompt')
        self.client.force_login(self.reader.user)

    def url(self,name,photo=None):
        return reverse(name,args=[self.group.slug,self.month.pk]+([photo.pk] if photo else []))

    def payload(self,**values):
        return {'prompt':self.photo_prompt.pk,'reader_confirmed':'on','discord_message_url':'',**values}

    def edit(self,photo,**values):
        return self.client.post(self.url('photo-edit',photo),{'prompt':photo.prompt_claim.prompt_id,
            'discord_message_url':photo.discord_message_url,'version':photo.version,**values})

    def test_entry_point_book_form_and_photo_choices(self):
        for name in ['month-detail','submission-create']:
            response=self.client.get(reverse(name,args=[self.group.slug,self.month.pk]))
            self.assertContains(response,'Photo');self.assertContains(response,self.url('photo-create'))
        response=self.client.get(self.url('photo-create'))
        self.assertContains(response,'Submission Type');self.assertContains(response,'Book')
        self.assertContains(response,'Required confirmation')
        self.assertContains(response,'I have posted my photo in the designated Discord channel.')
        self.assertContains(response,'Submit Photo')
        options=list(response.context['form'].fields['prompt'].queryset)
        self.assertEqual(options,[self.photo_prompt,self.second]);self.assertNotIn(self.book_prompt,options)
        for field in ['title','author','completed_on','submitted_pages','edition','image']:
            self.assertNotContains(response,'name="'+field+'"')
        self.assertNotContains(response,'type="file"')

    def test_creation_is_pending_atomic_and_creates_no_book_or_points(self):
        before=self.protected()
        response=self.client.post(self.url('photo-create'),self.payload(),follow=True)
        self.assertEqual(response.status_code,200);self.assertContains(response,'Pending review')
        photo=ScavengerPhotoSubmission.objects.get();self.assertEqual(photo.status,'pending')
        self.assertEqual(photo.prompt_claim.state,'reserved');self.assertEqual(photo.participant_id,self.reader.pk)
        self.assertEqual(self.protected(),before);self.assertFalse(ModifierProvenance.objects.exists())
        self.assertFalse(PromptCompletion.objects.exists())

    def test_confirmation_missing_invalid_link_and_forged_ownership(self):
        for payload in [self.payload(reader_confirmed=''),self.payload(discord_message_url='https://example.com/photo')]:
            response=self.client.post(self.url('photo-create'),payload)
            self.assertEqual(response.status_code,200);self.assertTrue(response.context['form'].errors)
        for field in ['participant_id','team_id','month_id','status','reviewer_verified']:
            self.assertEqual(self.client.post(self.url('photo-create'),self.payload(**{field:'999'})).status_code,403)
        self.assertFalse(ScavengerPhotoSubmission.objects.exists())

    def test_optional_valid_discord_url_and_duplicate_denial(self):
        url='https://discord.com/channels/111/222/333'
        self.assertEqual(self.client.post(self.url('photo-create'),self.payload(discord_message_url=url)).status_code,302)
        response=self.client.post(self.url('photo-create'),self.payload())
        self.assertEqual(response.status_code,200);self.assertTrue(response.context['form'].errors)
        photo=ScavengerPhotoSubmission.objects.get();self.assertEqual(photo.discord_message_url,url)
        response=self.client.get(self.url('photo-detail',photo));self.assertContains(response,'rel="noopener noreferrer"')

    def test_owner_edits_prompt_in_place_and_original_becomes_available(self):
        photo=self.create();claim_id=photo.prompt_claim.pk
        self.assertContains(self.client.get(self.url('photo-edit',photo)),'Back to My Stats')
        original=(photo.pk,photo.participant_id,photo.month_id,photo.submitted_at,photo.reader_confirmed,photo.acceptance_evidence)
        self.assertEqual(self.edit(photo,prompt=self.second.pk).status_code,302)
        photo.refresh_from_db();claim=photo.prompt_claim
        self.assertEqual(claim.pk,claim_id);self.assertEqual(claim.prompt_id,self.second.pk)
        self.assertEqual(claim.state,'reserved')
        self.assertEqual((photo.pk,photo.participant_id,photo.month_id,photo.submitted_at,photo.reader_confirmed,photo.acceptance_evidence),original)
        response=self.client.get(self.url('photo-create'))
        self.assertEqual(list(response.context['form'].fields['prompt'].queryset),[self.photo_prompt])
        audit=AuditEvent.objects.get(action='scavenger_photo.edited')
        self.assertIn(str(self.photo_prompt.pk),audit.summary);self.assertIn(str(self.second.pk),audit.summary)

    def test_link_add_replace_remove_preserves_selection_and_scoring(self):
        photo=self.create();before=self.protected();selection=photo.prompt_claim.selection_evidence
        for value in ['https://discord.com/channels/1/2/3','https://discord.com/channels/1/2/4','']:
            self.assertEqual(self.edit(photo,discord_message_url=value).status_code,302)
            photo.refresh_from_db();self.assertEqual(photo.discord_message_url,value)
            self.assertEqual(photo.prompt_claim.selection_evidence,selection)
        self.assertEqual(self.protected(),before)
        for event in AuditEvent.objects.filter(action='scavenger_photo.edited'):self.assertNotIn('discord.com',event.summary)

    def test_conflicting_edit_and_stale_edit_preserve_original(self):
        photo=self.create()
        create_photo_submission(month_id=self.month.pk,actor=self.reader.user,prompt_id=self.second.pk,reader_confirmed=True)
        response=self.edit(photo,prompt=self.second.pk)
        self.assertEqual(response.status_code,200);self.assertTrue(response.context['form'].errors)
        photo.refresh_from_db();self.assertEqual(photo.prompt_claim.prompt_id,self.photo_prompt.pk)
        version=photo.version
        self.assertEqual(self.edit(photo,discord_message_url='https://discord.com/channels/1/2/3').status_code,302)
        response=self.edit(photo,version=version)
        self.assertEqual(response.status_code,200);self.assertContains(response,'Reload before saving')
        photo.refresh_from_db();self.assertEqual(photo.discord_message_url,'https://discord.com/channels/1/2/3')

    def test_direct_edit_service_rechecks_scope_and_rolls_back_failure(self):
        photo=self.create();before=self.protected()
        with self.assertRaises(PermissionDenied):
            edit_pending_photo(photo_id=photo.pk,actor=self.users['leader'],version=photo.version,prompt_id=self.second.pk)
        with patch('core.photo_submissions._audit',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):
                edit_pending_photo(photo_id=photo.pk,actor=self.reader.user,version=photo.version,prompt_id=self.second.pk)
        photo.refresh_from_db();self.assertEqual(photo.version,1);self.assertEqual(photo.prompt_claim.prompt_id,self.photo_prompt.pk)
        self.assertEqual(self.protected(),before)

    def test_direct_claim_mutation_still_denied(self):
        photo=self.create();claim=photo.prompt_claim;claim.prompt=self.second
        with self.assertRaises(ValidationError):claim.save()
        claim.refresh_from_db();self.assertEqual(claim.prompt_id,self.photo_prompt.pk)

    def test_nonowners_and_cross_group_urls_denied(self):
        photo=self.create(discord_message_url='https://discord.com/channels/1/2/3')
        for actor in [self.users['leader'],self.users['outsider'],self.users['root']]:
            self.client.force_login(actor)
            for name in ['photo-detail','photo-edit']:
                response=self.client.get(self.url(name,photo))
                expected=200 if name=='photo-detail' and actor in [self.users['leader'],self.users['root']] else 404
                self.assertEqual(response.status_code,expected)
                if expected==404:self.assertNotContains(response,photo.discord_message_url,status_code=404)
                else:self.assertContains(response,'Photo Activity History')
            self.assertEqual(self.client.post(self.url('photo-withdraw',photo),{'version':photo.version}).status_code,404)
        self.client.force_login(self.reader.user)
        response=self.client.get(self.url('photo-detail',photo).replace(self.group.slug,'wrong-group'))
        self.assertEqual(response.status_code,404)

    def test_nonpending_states_deny_reader_edits_and_withdrawal(self):
        photo=self.create()
        for state in ['approved','rejected','withdrawn','voided']:
            # Disposable fixture only; no application approval operation is introduced.
            ScavengerPhotoSubmission.objects.filter(pk=photo.pk).update(status=state,reviewer_verified=state=='approved',reviewed_by=self.actor if state=='approved' else None,
                reviewed_at=timezone.now() if state=='approved' else None,removed_at=timezone.now() if state in ['withdrawn','voided'] else None)
            self.assertEqual(self.edit(photo).status_code,403)
            self.assertEqual(self.client.post(self.url('photo-withdraw',photo),{'version':photo.version}).status_code,403)

    def test_withdrawal_is_post_only_releases_and_keeps_history(self):
        photo=self.create();before=self.protected()
        self.assertEqual(self.client.get(self.url('photo-withdraw',photo)).status_code,405)
        response=self.client.post(self.url('photo-withdraw',photo),{'version':photo.version})
        self.assertEqual(response.status_code,302);photo.refresh_from_db()
        self.assertEqual(photo.status,'withdrawn');self.assertEqual(photo.prompt_claim.state,'released')
        self.assertEqual(ScavengerPhotoSubmission.objects.count(),1)
        response=self.client.get(reverse('my-stats'));self.assertContains(response,'Withdrawn')
        self.assertEqual(self.protected(),before);self.assertFalse(ModifierProvenance.objects.exists())
        self.assertTrue(AuditEvent.objects.filter(action='scavenger_photo.reservation_changed').exists())
        self.assertEqual(self.client.post(self.url('photo-create'),self.payload()).status_code,302)

    def test_stale_withdrawal_rejected_and_csrf_required(self):
        photo=self.create()
        response=self.client.post(self.url('photo-withdraw',photo),{'version':photo.version+1})
        self.assertEqual(response.status_code,400);photo.refresh_from_db();self.assertEqual(photo.status,'pending')
        client=Client(enforce_csrf_checks=True);client.force_login(self.reader.user)
        self.assertEqual(client.post(self.url('photo-withdraw',photo),{'version':photo.version}).status_code,403)

    def test_gate_category_november_and_lifecycle_deny_operations_but_keep_history(self):
        photo=self.create()
        with override_settings(NORTHBOUND_PROMPT_RESERVATIONS_READY=False):
            self.assertEqual(self.client.get(self.url('photo-create')).status_code,403)
            self.assertEqual(self.edit(photo).status_code,403)
            self.assertEqual(self.client.post(self.url('photo-withdraw',photo),{'version':photo.version}).status_code,403)
            self.assertContains(self.client.get(reverse('my-stats')),'Scavenger Hunt — Photo')
            self.assertEqual(self.client.get(self.url('photo-detail',photo)).status_code,200)
        self.photo_prompt.category.refresh_from_db();self.photo_prompt.category.enabled=False;self.photo_prompt.category.save()
        self.assertEqual(self.client.get(self.url('photo-create')).status_code,403);self.assertEqual(self.edit(photo).status_code,403)
        self.assertContains(self.client.get(reverse('my-stats')),'Scavenger Hunt — Photo')
        self.photo_prompt.category.enabled=True;self.photo_prompt.category.save()
        for state in ['completed','archived']:
            ChallengeMonth.objects.filter(pk=self.month.pk).update(status=state)
            self.assertEqual(self.client.get(self.url('photo-create')).status_code,403);self.assertEqual(self.edit(photo).status_code,403)
            self.assertEqual(self.client.get(self.url('photo-detail',photo)).status_code,200)
        ChallengeMonth.objects.filter(pk=self.month.pk).update(status='active',prompt_automation_excluded=True)
        self.assertEqual(self.client.get(self.url('photo-create')).status_code,403)

    def test_inactive_reader_creation_and_edit_denied(self):
        photo=self.create()
        MonthEnrollment.objects.filter(month=self.month,participant=self.reader).update(is_active=False)
        self.assertEqual(self.client.get(self.url('photo-create')).status_code,403)
        self.assertEqual(self.edit(photo).status_code,403)

    def test_private_history_book_counts_and_other_outputs_unchanged(self):
        before=self.client.get(reverse('my-stats')).context['approved_books']
        photo=self.create(discord_message_url='https://discord.com/channels/111/222/333')
        response=self.client.get(reverse('my-stats'));self.assertContains(response,'Scavenger Hunt — Photo')
        self.assertContains(response,'My Book History');self.assertContains(response,self.book.title)
        self.assertEqual(response.context['approved_books'],before)
        self.assertContains(self.client.get(reverse('month-detail',args=[self.group.slug,self.month.pk])),'Scavenger Hunt Photo History')
        self.client.force_login(self.users['leader'])
        for url in [reverse('my-stats'),reverse('month-detail',args=[self.group.slug,self.month.pk]),
                    reverse('team-detail',args=[self.group.slug,self.month.pk,self.team.pk]),
                    reverse('global-search')+'?q=Photo',reverse('recap-wrapped')+'?group='+str(self.group.pk)+'&challenge='+str(self.month.pk)]:
            response=self.client.get(url)
            self.assertNotIn(photo.discord_message_url.encode(),response.content)

    def test_history_pagination_preserves_other_query_parameters_and_batched_queries(self):
        prompts=[self.photo_prompt,self.second]+[self.prompt(i,kind='scavenger',submission_type='photo') for i in range(4,28)]
        for prompt in prompts[:2]:create_photo_submission(month_id=self.month.pk,actor=self.reader.user,prompt_id=prompt.pk,reader_confirmed=True)
        request=RequestFactory().get('/account/stats/?existing=keep');request.user=self.reader.user
        with CaptureQueriesContext(connection) as small:reader_photo_history(request)
        for prompt in prompts[2:]:create_photo_submission(month_id=self.month.pk,actor=self.reader.user,prompt_id=prompt.pk,reader_confirmed=True)
        with CaptureQueriesContext(connection) as large:data=reader_photo_history(request)
        self.assertLessEqual(len(large),len(small)+1)
        self.assertEqual(len(data['photo_cards']),25);self.assertIn('existing=keep',data['photo_query'])
        response=self.client.get(reverse('my-stats'),{'photo_page':2,'existing':'keep'})
        self.assertEqual(response.context['photo_page'].number,2);self.assertEqual(len(response.context['photo_cards']),1)

    def test_optional_local_browser_fixtures(self):
        target=os.environ.get('NB_PHOTO_B_FIXTURES')
        if not target:return
        root=Path(target);root.mkdir(parents=True,exist_ok=True)
        photo=self.create();self.photo_prompt.description='A long Photo prompt about the reading setup and seasonal decorations '*5
        # Frozen wording may change; the captured description remains historical evidence.
        self.photo_prompt.save()
        for name,url in [('create',self.url('photo-create')),('edit',self.url('photo-edit',photo)),('detail',self.url('photo-detail',photo)),('history',reverse('my-stats'))]:
            response=self.client.get(url);self.assertEqual(response.status_code,200)
            (root/(name+'.html')).write_bytes(response.content)
