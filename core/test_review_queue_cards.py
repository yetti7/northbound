"""The existing Immersive controls share the ordinary responsive review grid."""
from html.parser import HTMLParser
from pathlib import Path
from django.test import TestCase, Client
from django.urls import reverse
from . import test_immersive_ui as fixture
from .models import ModifierProvenance


class GridParser(HTMLParser):
    def __init__(self, text):
        super().__init__();self.grids=[];self.in_grid=0;self.immersive_in_grid=[];self.grid_stack=[];self.feed(text)
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs);classes=attrs.get('class','').split()
        if tag=='div':
            is_grid='review-queue-grid' in classes
            self.grid_stack.append(is_grid)
            if is_grid:self.in_grid+=1;self.grids.append(classes)
        if tag=='article' and 'immersive-review-card' in classes:self.immersive_in_grid.append(self.in_grid>0)
    def handle_endtag(self, tag):
        if tag=='div' and self.grid_stack:
            if self.grid_stack.pop():self.in_grid-=1


class ReviewQueueCardTests(TestCase):
    setUp=fixture.ImmersiveInterfaceTests.setUp
    actor=fixture.ImmersiveInterfaceTests.actor
    submission=fixture.ImmersiveInterfaceTests.submission
    enable=fixture.ImmersiveInterfaceTests.enable
    request=fixture.ImmersiveInterfaceTests.request
    route=fixture.ImmersiveInterfaceTests.route

    def test_immersive_cards_share_one_grid_with_existing_actions_and_data(self):
        self.enable(25);claim=self.request()
        self.client.force_login(self.host)
        page=self.client.get(self.route('review-queue'))
        parser=GridParser(page.content.decode())
        self.assertEqual(len(parser.grids),1);self.assertEqual(parser.immersive_in_grid,[True])
        self.assertNotContains(page,'<h2>Immersive Reading</h2>',html=True)
        self.assertNotContains(page,'No pending reviews.')
        for text in ['Immersive Reading · Pending','350 approved base pages','88 bonus pages','Requested',claim.submission.title]:self.assertContains(page,text)
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

    def test_common_grid_keeps_existing_mobile_and_dark_mode_rules(self):
        self.enable();self.request();self.client.force_login(self.host)
        page=self.client.get(self.route('review-queue'));self.assertNotContains(page,'immersive-review-grid')
        css=(Path(__file__).resolve().parent.parent/'static/css/theme.css').read_text()
        self.assertIn('.review-queue-grid{grid-template-columns:1fr}',css)
        self.assertIn('.immersive-review-card',css)
        # The moved card still uses the same theme-aware card classes; no separate styling system.
        self.assertContains(page,'class="card immersive-review-card"')
