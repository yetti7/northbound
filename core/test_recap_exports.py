from copy import deepcopy
from django.test import SimpleTestCase
from .recap_exports import export_data
from .test_wrapped import WrappedTests


class ExportPresentationTests(SimpleTestCase):
    def test_presentation_does_not_mutate_or_recalculate_and_omits_contribution_names(self):
        recap = {'perspective': 'team', 'period': 'All time', 'sections': [
            {'subject': 'Team A', 'cards': [{'label': 'Points', 'value': 12}], 'books': []}], 'branding': {}}
        slides = [{'kind': 'opening', 'title': 'Private Group'},
                  {'kind': 'contributions', 'title': 'Contributions', 'rows': [{'reader': 'private-reader', 'points': 12}]}]
        before = deepcopy((recap, slides))
        data = export_data(recap, slides)
        self.assertEqual((recap, slides), before)
        self.assertEqual(data['slides'][0]['title'], 'Northbound Wrapped')
        self.assertNotIn('private-reader', str(data))
        self.assertEqual(data['slides'][1]['cards'][0]['value'], 12)

    def test_no_export_when_no_authorized_slides(self):
        self.assertIsNone(export_data(None, []))


class ExportEndpointTests(WrappedTests):
    # Reuse the existing isolated privacy fixtures and assertions for the same GET.
    def test_identity_controls_default_off_and_export_uses_authorized_data(self):
        response = self.wrap(self.reader)
        self.assertEqual(response.status_code, 200)
        data = response.context['export_data']
        self.assertEqual(data['perspective'], 'reader')
        self.assertNotIn('Secret foreign', str(data))
        self.assertNotIn('Restricted tome ZX', str(data))
        self.assertContains(response, 'data-export-identity="reader"')
        self.assertNotContains(response, 'data-export-identity="team"')
        self.assertNotContains(response, 'type="checkbox" checked')
        self.assertContains(response, 'wrapped_export.js')
        self.assertContains(response, 'Download This Slide')

    def test_team_and_challenge_exports_follow_existing_recap_authorization(self):
        for perspective in ('team', 'challenge'):
            response = self.wrap(self.admin, perspective=perspective, team=self.team.pk if perspective == 'team' else '')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context['export_data']['perspective'], perspective)
            self.assertNotContains(response, 'data-export-identity="reader"')
            self.assertNotIn('Secret foreign', str(response.context['export_data']))

    def test_forged_cross_group_target_is_denied(self):
        response = self.wrap(self.reader, group=self.foreign.pk)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].errors)
        self.assertIsNone(response.context['export_data'])
        self.assertNotContains(response, 'wrapped-export-data')
