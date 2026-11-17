"""No registry or GitHub requests: test publication policy and immutable tag handling."""
import importlib.util
import io
import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('release_channels', ROOT / '.github/scripts/release_channels.py')
channels = importlib.util.module_from_spec(spec)
spec.loader.exec_module(channels)


def release(tag='v1.1.3', **changes):
    return {'id':13,'tag_name':tag,'draft':False,'prerelease':False,'published_at':'2026-11-17T12:00:00Z',**changes}


def event(tag='v1.1.3', **changes):
    return {'action':'published','release':release(tag,**changes)}


class ReleaseSelectionTests(TestCase):
    def test_stable_and_letter_suffix_hotfix(self):
        for tag in ('v1.1.3','v1.1.3a','v1.1.3b'):
            self.assertEqual(channels.selection('release',event(tag),'refs/tags/'+tag), {'publish':True,'version':tag,'stable':True})

    def test_prerelease_draft_and_unpublished_release_never_advance_channels(self):
        for changes in ({'prerelease':True},{'draft':True},{'published_at':None}):
            self.assertFalse(channels.selection('release',event(**changes),'refs/tags/v1.1.3')['stable'])
        for action in ('created','edited','unpublished','deleted'):
            payload=event();payload['action']=action
            self.assertFalse(channels.selection('release',payload,'refs/tags/v1.1.3')['stable'])

    def test_tag_push_keeps_version_tag_but_not_floating_channels(self):
        self.assertEqual(channels.selection('push',{},'refs/tags/v1.1.3a'), {'publish':True,'version':'v1.1.3a','stable':False})

    def test_main_push_and_manual_main_are_sha_only(self):
        for name in ('push','workflow_dispatch'):
            self.assertEqual(channels.selection(name,{},'refs/heads/main'), {'publish':True,'version':'','stable':False})

    def test_development_and_pull_requests_do_not_publish_stable_channels(self):
        for name,ref in [('push','refs/heads/dev'),('workflow_dispatch','refs/heads/dev'),('pull_request','refs/pull/20/merge')]:
            self.assertEqual(channels.selection(name,{},ref), {'publish':False,'version':'','stable':False})

    def test_release_from_draft_and_prerelease_to_stable_use_publication_metadata(self):
        payload=event('v1.1.3a');payload['action']='released'
        self.assertTrue(channels.selection('release',payload,'refs/tags/v1.1.3a')['stable'])

    def test_invalid_stable_tags_are_not_promoted(self):
        for tag in ('latest','stable','v01.1.3','v1.1.3-rc.1','v1.1.3+build','v1.1.3\nlatest'):
            self.assertFalse(channels.selection('release',event(tag),'refs/tags/'+tag)['stable'])


class PromotionTests(TestCase):
    def setUp(self):
        self.registry=Mock()
        self.digest='sha256:'+'a'*64
        self.registry.version_digest.return_value=self.digest
        self.registry.fetch.return_value=({},self.digest)

    def promote(self,payload=None,name='release',digest=None):
        return channels.promote(name,payload or event(),'refs/tags/v1.1.3','owner/northbound','commit',digest or self.digest,self.registry)

    @patch.object(channels,'newest_stable',return_value=release())
    @patch.object(channels.subprocess,'run')
    def test_promotes_only_latest_and_stable_from_existing_digest(self,run,latest):
        self.assertTrue(self.promote())
        self.assertEqual(run.call_args.args[0],['docker','buildx','imagetools','create','--tag','ghcr.io/owner/northbound:latest','--tag','ghcr.io/owner/northbound:stable','ghcr.io/owner/northbound@'+self.digest])
        self.assertTrue(run.call_args.kwargs['check'])
        self.assertEqual(self.registry.fetch.call_count,2)

    @patch.object(channels,'newest_stable',return_value=release('v1.1.3a',id=14))
    @patch.object(channels.subprocess,'run')
    def test_letter_suffix_release_promotes_both_channels(self,run,latest):
        payload=event('v1.1.3a');payload['release']['id']=14
        self.assertTrue(self.promote(payload=payload))
        self.registry.version_digest.assert_called_once_with('v1.1.3a','commit')
        command=run.call_args.args[0]
        self.assertIn('ghcr.io/owner/northbound:latest',command)
        self.assertIn('ghcr.io/owner/northbound:stable',command)
        self.assertNotIn('ghcr.io/owner/northbound:v1.1.3a',command)

    @patch.object(channels,'newest_stable',return_value=release('v1.1.3a',id=14))
    @patch.object(channels.subprocess,'run')
    def test_old_delayed_release_does_not_move_channels_backwards(self,run,latest):
        self.assertFalse(self.promote());run.assert_not_called()

    @patch.object(channels,'newest_stable',return_value=release())
    @patch.object(channels.subprocess,'run')
    def test_revision_or_digest_mismatch_does_not_publish(self,run,latest):
        self.registry.version_digest.return_value='sha256:'+'b'*64
        with self.assertRaisesRegex(RuntimeError,'digest mismatch'):self.promote()
        run.assert_not_called()

    @patch.object(channels.subprocess,'run')
    def test_prerelease_or_tag_push_never_runs_promotion(self,run):
        self.assertFalse(self.promote(payload=event(prerelease=True)))
        self.assertFalse(self.promote(name='push'));run.assert_not_called()

    @patch.object(channels,'newest_stable',side_effect=RuntimeError('API unavailable'))
    @patch.object(channels.subprocess,'run')
    def test_failed_authoritative_check_cannot_promote(self,run,latest):
        with self.assertRaises(RuntimeError):self.promote()
        run.assert_not_called()

    @patch.object(channels,'newest_stable',return_value=release())
    @patch.object(channels.subprocess,'run')
    def test_floating_tag_verification_detects_partial_failure(self,run,latest):
        self.registry.fetch.return_value=({},'sha256:'+'b'*64)
        with self.assertRaisesRegex(RuntimeError,'verification failed'):self.promote()


class ImmutableVersionTests(TestCase):
    def registry(self):
        return object.__new__(channels.Registry)

    def test_absent_version_permits_first_publication(self):
        r=self.registry();r.fetch=Mock(return_value=(None,''))
        self.assertEqual(r.version_digest('v1.1.3','commit'),'')

    def test_existing_version_reused_without_rebuild(self):
        r=self.registry();r.fetch=Mock(side_effect=[
            ({'manifests':[{'platform':{'os':'linux','architecture':arch},'digest':arch} for arch in ('amd64','arm64')]},'sha256:index'),
            ({'config':{'digest':'config-a'}},'sha256:a'),
            ({'config':{'Labels':{'org.opencontainers.image.revision':'commit','org.opencontainers.image.version':'v1.1.3a'}}},'sha256:c'),
            ({'config':{'digest':'config-b'}},'sha256:b'),
            ({'config':{'Labels':{'org.opencontainers.image.revision':'commit','org.opencontainers.image.version':'v1.1.3a'}}},'sha256:d'),
        ])
        self.assertEqual(r.version_digest('v1.1.3a','commit'),'sha256:index')

    def test_existing_version_with_different_revision_fails_without_repointing(self):
        r=self.registry();r.fetch=Mock(side_effect=[
            ({'manifests':[{'platform':{'os':'linux','architecture':'amd64'},'digest':'a'}]},'sha256:index'),
            ({'config':{'digest':'c'}},'sha256:a'),
            ({'config':{'Labels':{'org.opencontainers.image.revision':'different'}}},'sha256:c'),
        ])
        with self.assertRaisesRegex(RuntimeError,'refusing to repoint'):r.version_digest('v1.1.3','commit')

    @patch.object(channels,'urlopen')
    def test_only_404_is_treated_as_an_absent_version(self,open_url):
        r=self.registry();r.repository='owner/repo';r.token='synthetic-token'
        open_url.side_effect=HTTPError('https://ghcr.io',404,'missing',{},None)
        self.assertEqual(r.fetch('manifests','v1.1.3',missing_ok=True),(None,''))
        for code in (401,403,429,500):
            open_url.side_effect=HTTPError('https://ghcr.io',code,'blocked',{},None)
            with self.assertRaisesRegex(RuntimeError,'refusing to overwrite'):r.fetch('manifests','v1.1.3',missing_ok=True)

    @patch.object(channels,'urlopen')
    def test_newest_stable_uses_publication_order_not_listing_order(self,open_url):
        releases=[release(id=11,published_at='2026-11-16T12:00:00Z'),release('v1.1.3a',id=12),release('v2.0.0',id=15,prerelease=True)]
        open_url.return_value.__enter__.return_value=io.StringIO(json.dumps(releases))
        with patch.dict(channels.os.environ,{'GH_TOKEN':'synthetic-token'}):
            self.assertEqual(channels.newest_stable('owner/repo')['id'],12)


class WorkflowWiringTests(TestCase):
    def test_workflow_has_publication_events_guarded_builds_and_separate_promotion(self):
        workflow=(ROOT/'.github/workflows/container.yml').read_text()
        self.assertIn('types: [published, released]',workflow)
        self.assertIn('flavor: latest=false',workflow)
        self.assertIn("steps.existing.outputs.exists != 'true'",workflow)
        self.assertIn("needs.build-and-publish.outputs.stable_release == 'true'",workflow)
        self.assertIn("github.event_name == 'release' &&",workflow)
        self.assertIn("github.event.release.draft == false && github.event.release.prerelease == false",workflow)
        self.assertIn("if: github.event_name != 'pull_request' && steps.image-tags.outputs.publish == 'true'",workflow)
        self.assertIn('group: northbound-stable-release-channels',workflow)
        self.assertNotIn('type=raw,value=latest',workflow)
        self.assertNotIn('type=raw,value=stable',workflow)
        development=(ROOT/'.github/workflows/development.yml').read_text()
        self.assertIn('github.ref == \'refs/heads/dev\'',development)
        self.assertNotIn('northbound:stable',development)
