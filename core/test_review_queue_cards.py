"""The existing Immersive controls share the ordinary responsive review grid."""
from html.parser import HTMLParser
from pathlib import Path
from django.test import TestCase, Client
from django.urls import reverse
from . import test_immersive_ui as fixture
from .models import ModifierProvenance


class GroupedQueueParser(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.details = []
        self.rows = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        classes = set(dict(attrs).get('class', '').split())
        if tag == 'details':
            self.details.append(classes)
        if tag == 'article' and 'review-row' in classes:
            self.rows.append(('review-team' in set().union(*self.details),
                              'review-reader' in set().union(*self.details)))

    def handle_endtag(self, tag):
        if tag == 'details':
            self.details.pop()


class ReviewQueueCardTests(TestCase):
    setUp=fixture.ImmersiveInterfaceTests.setUp
    actor=fixture.ImmersiveInterfaceTests.actor
    submission=fixture.ImmersiveInterfaceTests.submission
    enable=fixture.ImmersiveInterfaceTests.enable
    request=fixture.ImmersiveInterfaceTests.request
    route=fixture.ImmersiveInterfaceTests.route

    def test_immersive_request_is_grouped_with_existing_actions_and_data(self):
        self.enable(25);claim=self.request()
        self.client.force_login(self.host)
        page=self.client.get(self.route('review-queue'))
        parser=GroupedQueueParser(page.content.decode())
        self.assertEqual(parser.rows, [(True, True)])
        self.assertEqual(page.context["queue_total"], 1)
        self.assertEqual(page.context["queue_readers"], 1)
        self.assertNotContains(page,'<h2>Immersive Reading</h2>',html=True)
        self.assertNotContains(page,'No pending reviews.')
        for text in ['Immersive Reading','350 approved base pages','88 bonus pages',claim.submission.title,'Review &amp; Scoring']:self.assertContains(page,text)
        for decision in ['approve','reject']:
            self.assertContains(page,self.route('immersive-action',claim.pk,decision))
        self.assertEqual(self.client.post(self.route('immersive-action',claim.pk,'approve'),{'confirm':'on'}).status_code,302)
        self.assertEqual(ModifierProvenance.objects.filter(source_type='immersive').count(),1)
        self.client.post(self.route('immersive-action',claim.pk,'approve'),{'confirm':'on'})
        self.assertEqual(ModifierProvenance.objects.filter(source_type='immersive').count(),1)

    def test_reject_permission_and_csrf_are_unchanged(self):
        self.enable();claim=self.request()
        url=self.route('immersive-action',claim.pk,'reject')
        self.client.force_login(self.reader.user)
        self.assertEqual(self.client.post(url,{'confirm':'on','reason':'Review reason'}).status_code,403)
        protected=Client(enforce_csrf_checks=True);protected.force_login(self.host)
        self.assertEqual(protected.post(url,{'confirm':'on','reason':'Review reason'}).status_code,403)
        self.client.force_login(self.host)
        self.assertEqual(self.client.post(url,{'confirm':'on','reason':'Not eligible'}).status_code,302)
        claim.refresh_from_db();self.assertEqual(claim.status,'rejected')

    def test_grouped_queue_keeps_responsive_theme_aware_rules(self):
        self.enable();self.request();self.client.force_login(self.host)
        page=self.client.get(self.route('review-queue'))
        self.assertEqual(GroupedQueueParser(page.content.decode()).rows, [(True, True)])
        css=(Path(__file__).resolve().parent.parent/'static/css/review_queue.css').read_text()
        self.assertIn('@media(max-width:640px)',css)
        self.assertIn('.review-row{flex-direction:column}',css)
        self.assertIn('var(--border',css)
        self.assertIn(':focus-visible',css)
        self.assertContains(page, 'class="card review-team"')
