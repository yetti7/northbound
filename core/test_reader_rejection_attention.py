from django.test import TestCase,Client
from django.urls import reverse
from django.utils import timezone
from django.template.loader import render_to_string
from . import test_reader_rejection_notices as fixture
from .models import ReaderRejectionNotice,ChallengeStaffAssignment
from .reader_rejections import outstanding
from .review_attention import needs_attention_summary,challenge_attention_count


class ReaderRejectionAttentionTests(TestCase):
    setUp=fixture.RejectionNoticeTests.setUp
    token=fixture.RejectionNoticeTests.token
    prompt=fixture.RejectionNoticeTests.prompt
    settings=fixture.RejectionNoticeTests.settings
    pending=fixture.RejectionNoticeTests.pending
    reject_photo=fixture.RejectionNoticeTests.reject_photo

    def page(self,user=None):
        self.client.force_login(user or self.reader.user)
        return self.client.get(reverse('needs-attention'))

    def test_reader_navigation_and_get_does_not_acknowledge(self):
        self.reject_photo('PRIVATE_READER_REASON')
        response=self.page();self.assertEqual(response.status_code,200)
        self.assertContains(response,'Your Rejected Submissions')
        self.assertContains(response,'PRIVATE_READER_REASON')
        self.assertNotIn('PRIVATE_READER_REASON',response.content.decode().split('<title>')[1].split('</title>')[0])
        self.assertContains(response,'Acknowledge');self.assertContains(response,'Resubmit')
        self.assertEqual(response.context['needs_attention']['total'],1)
        self.assertContains(response,'href="/needs-attention/"',count=2)
        self.page();self.assertTrue(outstanding(self.reader.user).exists())
        self.assertFalse(ReaderRejectionNotice.objects.get().acknowledged_at)

    def test_only_recipient_can_acknowledge_and_csrf_post_is_required(self):
        self.reject_photo();n=ReaderRejectionNotice.objects.get()
        url=reverse('rejection-acknowledge',args=[n.pk])
        self.client.force_login(self.actor);self.assertEqual(self.client.post(url).status_code,404)
        self.assertNotContains(self.client.get(reverse('needs-attention')),fixture.REASON)
        self.client.force_login(self.reader.user);self.assertEqual(self.client.get(url).status_code,405)
        csrf=Client(enforce_csrf_checks=True);csrf.force_login(self.reader.user);self.assertEqual(csrf.post(url).status_code,403)
        self.assertEqual(self.client.post(url).status_code,302);self.assertEqual(self.client.post(url).status_code,302)
        response=self.page();self.assertNotContains(response,'href="/needs-attention/"')

    def test_combined_counts_keep_review_item_semantics(self):
        ChallengeStaffAssignment.objects.create(month=self.month,membership=self.reader,role='team_leader',team=self.team)
        self.reject_photo()
        summary=needs_attention_summary(self.reader.user)
        self.assertEqual(summary['rejection_count'],1)
        self.assertEqual(summary['review_total'],challenge_attention_count(self.reader.user,self.month))
        self.assertEqual(summary['total'],summary['review_total']+1)
        response=self.page();self.assertContains(response,'Reviews Requiring Your Attention');self.assertContains(response,'Your Rejected Submissions')

    def test_explicit_photo_retry_existing_form_and_no_cross_reader_link(self):
        self.reject_photo();n=ReaderRejectionNotice.objects.get()
        self.client.force_login(self.actor)
        url=reverse('photo-create',args=[self.group.slug,self.month.pk])
        # A reviewer cannot act as the recipient.
        self.assertIn(self.client.get(url+'?retry_notice='+str(n.pk)).status_code,[403,404])
        self.client.force_login(self.reader.user)
        response=self.client.get(url+'?retry_notice='+str(n.pk));self.assertEqual(response.status_code,200)
        self.assertContains(response,'name="replacement_notice"')
        response=self.client.post(url,{'replacement_notice':n.pk,'prompt':self.photo_prompt.pk,'reader_confirmed':'on'})
        self.assertEqual(response.status_code,302);self.assertFalse(outstanding(self.reader.user).exists())

    def test_unsupported_book_correction_has_no_retry_and_queue_layout_independence(self):
        from .matching_resolution import review_submission_with_matches
        self.second.status='rejected'
        review_submission_with_matches(self.second,[],self.actor,timezone.now(),rejection_reason=fixture.REASON)
        response=self.page();self.assertNotContains(response,'>Resubmit<')
        # Render the original queue from Git history in an isolated template loader.
        from pathlib import Path
        original=(Path(__file__).resolve().parent.parent/'test-fixtures/review_queue_before_85ce353.html').read_text()
        from django.test import override_settings
        from django.conf import settings
        import copy
        templates=copy.deepcopy(settings.TEMPLATES);templates[0]['APP_DIRS']=False
        templates[0]['OPTIONS']['loaders']=[('django.template.loaders.locmem.Loader',{'core/review_queue.html':original}),'django.template.loaders.filesystem.Loader','django.template.loaders.app_directories.Loader']
        with override_settings(TEMPLATES=templates):
            response=self.page();self.assertContains(response,fixture.REASON)
            self.client.force_login(self.actor)
            queue=self.client.get(reverse('review-queue',args=[self.group.slug,self.month.pk]));self.assertEqual(queue.status_code,200)
            self.assertContains(self.page(),fixture.REASON)
