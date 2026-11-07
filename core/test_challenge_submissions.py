from datetime import timedelta
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from . import test_submission_hotfix as fixture
from . import test_submission_editing as editing
from .models import BookSubmission, ChallengeMonth, Membership, ReadingGroup


class ChallengeSubmissionTableTests(TestCase):
    actor = editing.SubmissionEditingTests.actor
    submission = editing.SubmissionEditingTests.submission

    def setUp(self):
        fixture.SubmissionHotfixTests.setUp(self)
        self.owner = self.actor('platform')
        self.client.force_login(self.owner)
        self.url = self.month.get_absolute_url()

    def get(self, **params):
        return self.client.get(self.url, {'submissions_fragment': '1', **params})

    def populate(self, count=55):
        return BookSubmission.objects.bulk_create([
            BookSubmission(month=self.month, participant=self.reader, title=f'Book {i:03}', author='Writer',
                book_format='ebook', completed_on=self.month.starts_on, submitted_pages=100,
                approved_pages=100, status='approved') for i in range(count)])

    def ids(self, response):
        return [x.pk for x in response.context['submission_page']]

    def test_search_all_rows_and_three_fields_before_pagination(self):
        books = self.populate()
        oldest = books[0]
        oldest.title = 'Unique distant title'; oldest.save(update_fields=['title'])
        self.assertNotIn(oldest.pk, self.ids(self.get()))
        self.assertEqual(self.ids(self.get(submission_q='DISTANT')), [oldest.pk])
        for query in (self.reader.user.username, self.reader.display_name):
            result = self.get(submission_q=query)
            self.assertEqual(result.context['submission_page'].paginator.count, 55)
        self.assertContains(self.get(submission_q='nonexistent'), 'No submissions match')

    def test_status_filters_use_removal_flag(self):
        items = self.populate(4)
        for item, status in zip(items, ('pending', 'approved', 'rejected', 'approved')):
            BookSubmission.objects.filter(pk=item.pk).update(status=status, is_removed=item==items[-1])
        self.assertEqual(self.get().context['submission_page'].paginator.count, 4)
        for status, item in zip(('pending','approved','rejected','voided'), items):
            response = self.get(submission_status=status)
            self.assertEqual(self.ids(response), [item.pk])
            if status=='voided':
                self.assertContains(response,'Voided')
                self.assertNotContains(response,'Void Submission')
                self.assertContains(response,'Manage Submission')
                self.assertContains(response, reverse('submission-manage', args=[self.month.group.slug, self.month.pk, item.pk]))
                self.assertNotContains(response, reverse('submission-edit', args=[self.month.group.slug, self.month.pk, item.pk]))
                self.assertContains(response,'Permanently Delete')

    def test_sizes_boundaries_and_stable_ordering(self):
        books = self.populate(105)
        BookSubmission.objects.filter(month=self.month).update(submitted_at=timezone.now())
        expected = sorted([x.pk for x in books], reverse=True)
        self.assertEqual(self.ids(self.get()), expected[:25])
        self.assertEqual(self.ids(self.get(submission_page='2')), expected[25:50])
        self.assertEqual(self.ids(self.get(submission_page='-1')), expected[:25])
        self.assertEqual(self.ids(self.get(submission_page='bad')), expected[:25])
        self.assertEqual(self.ids(self.get(submission_page='999')), expected[100:])
        for size in ('25','50','100'):
            self.assertEqual(len(self.ids(self.get(submission_size=size))), int(size))
        self.assertEqual(len(self.ids(self.get(submission_size='999'))), 25)
        BookSubmission.objects.filter(pk=books[0].pk).update(submitted_at=timezone.now()+timedelta(days=1))
        self.assertEqual(self.ids(self.get())[0], books[0].pk)

    def test_filter_state_links_and_fragment_contract(self):
        self.populate(55)
        response = self.get(submission_q='Book',submission_status='approved',submission_size='50')
        self.assertEqual(response['X-Submission-Fragment'], '1')
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        self.assertNotContains(response, '<html')
        for state in ('submission_q=Book','submission_status=approved','submission_size=50','submission_page=2'):
            self.assertContains(response,state)
        self.assertEqual(self.get(submission_status='bad').context['submission_status'], 'all')
        full = self.client.get(self.url)
        self.assertContains(full, 'data-submission-filters')
        self.assertContains(full, 'challenge_submissions.js')
        for label in ('Reader','Book','Base Pages','Bonus Pages','Total Pages','Status','Actions'):
            self.assertContains(full, f'<th>{label}</th>', html=True)

    def test_no_cross_challenge_or_group_exposure(self):
        item = self.submission()
        group = ReadingGroup.objects.create(name='Secret group',slug='secret-group')
        other = ChallengeMonth.objects.create(group=group,name='Other',status='active',starts_on=self.month.starts_on,ends_on=self.month.ends_on)
        user = get_user_model().objects.create_user('private-reader')
        member = Membership.objects.create(group=group,user=user,display_name='Private')
        BookSubmission.objects.create(month=other,participant=member,title='Private book',author='Writer',book_format='ebook',completed_on=other.starts_on,submitted_pages=100)
        self.assertEqual(self.ids(self.get()), [item.pk])
        self.assertNotContains(self.get(), 'Private book')
        self.assertEqual(self.client.get(reverse('month-detail',args=['secret-group',self.month.pk]),{'submissions_fragment':'1'}).status_code,404)

    def test_only_platform_owner_can_fetch_all_submission_fragment(self):
        item = self.submission()
        for role in ('owner','moderator','host','floater','team_leader'):
            actor = self.actor(role)
            self.client.force_login(actor)
            self.assertEqual(self.get().status_code,403)
            full = self.client.get(self.url)
            self.assertContains(full, '<h2>All Submissions</h2>', html=True)
            self.assertNotContains(full, 'data-submission-filters')
            self.assertNotContains(full, item.title)
        self.client.force_login(self.reader.user)
        self.assertEqual(self.get().status_code,403)
        self.client.logout()
        self.assertEqual(self.get().status_code,302)
        self.client.force_login(self.owner)
        response = self.get()
        for action in ('Manage Submission','Void Submission','Permanently Delete'):
            self.assertContains(response, action)
        self.assertContains(response,reverse('recovery-submission-purge',args=[item.pk]))
        self.assertFalse(BookSubmission.objects.get(pk=item.pk).is_removed)

    def test_pagination_links_replace_existing_query_values(self):
        from urllib.parse import urlparse, parse_qs
        self.populate(80)
        response = self.get(submission_page='2', submission_q='Book', submission_status='approved')
        params = parse_qs(urlparse(response.context['submission_next']).query)
        self.assertEqual(params['submission_page'], ['3'])
        self.assertEqual(params['submission_status'], ['approved'])
        self.assertEqual(params['submission_q'], ['Book'])

    def test_scoped_pending_badge_and_accessible_full_reader_name(self):
        item = self.submission(status='pending')
        name = 'A very long Reader display name that should remain accessible'
        self.reader.display_name = name
        self.reader.save(update_fields=['display_name'])
        response = self.get()
        self.assertContains(response, 'class="pill status-warning"')
        self.assertContains(response, 'class="submission-reader-name"')
        self.assertContains(response, f'title="{name}"')
        self.assertContains(response, f'aria-label="{name}"')
        self.assertContains(response, 'Pending review')
        BookSubmission.objects.filter(pk=item.pk).update(is_removed=True)
        response = self.get()
        self.assertContains(response, 'Voided')
        self.assertNotContains(response, 'class="pill status-warning"')

    def test_neutral_heading_and_single_visible_counter_with_live_announcements(self):
        self.populate(34)
        response = self.client.get(self.url)
        self.assertContains(response, '<h2>All Submissions</h2>', html=True)
        self.assertNotContains(response, 'All Submissions · Platform Administration')
        self.assertContains(response, 'class="sr-only" data-submission-feedback role="status" aria-live="polite"')
        self.assertContains(response, 'data-submission-count', count=1)
        self.assertContains(response, '34 results · Page 1 of 2', count=1)
