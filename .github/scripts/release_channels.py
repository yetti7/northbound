"""Select image tags and promote only an authoritative published stable release."""
import base64
import hashlib
import json
import os
import re
import subprocess
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

STABLE = re.compile(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)([a-z]+)?\Z')
VERSION = re.compile(r'v[A-Za-z0-9_][A-Za-z0-9_.-]{0,126}\Z')


def published(release):
    return bool(release and release.get('draft') is False and release.get('published_at'))


def stable(release):
    return bool(published(release) and release.get('prerelease') is False
                and STABLE.fullmatch(release.get('tag_name', '')))


def selection(event_name, event, ref):
    release = event.get('release', {})
    release_event = event_name == 'release' and event.get('action') in ('published', 'released')
    version = release.get('tag_name', '') if release_event and published(release) else ''
    if event_name in ('push', 'workflow_dispatch') and ref.startswith('refs/tags/'):
        version = ref.removeprefix('refs/tags/')
    if not VERSION.fullmatch(version):
        version = ''
    publish = bool(version or ref == 'refs/heads/main') if event_name in ('push', 'workflow_dispatch') else bool(release_event and version)
    return {'publish': publish, 'version': version,
            'stable': bool(release_event and version and stable(release))}


def output(**values):
    with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
        for key, value in values.items():
            stream.write(f'{key}={str(value).lower() if isinstance(value, bool) else value}\n')


class Registry:
    """Only an actual manifest 404 permits creating a previously absent version tag."""
    def __init__(self, repository):
        self.repository = repository.lower()
        credentials = base64.b64encode((os.environ['GITHUB_ACTOR'] + ':' + os.environ['GH_TOKEN']).encode()).decode()
        query = urlencode({'service': 'ghcr.io', 'scope': 'repository:' + self.repository + ':pull'})
        request = Request('https://ghcr.io/token?' + query, headers={'Authorization': 'Basic ' + credentials})
        with urlopen(request, timeout=30) as response:
            self.token = json.load(response)['token']

    def fetch(self, kind, reference, *, missing_ok=False):
        request = Request(f'https://ghcr.io/v2/{self.repository}/{kind}/{reference}', headers={
            'Authorization': 'Bearer ' + self.token,
            'Accept': 'application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json',
        })
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read()
                digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
                if reference.startswith('sha256:') and reference != digest:
                    raise RuntimeError('Registry content digest verification failed.')
                if kind == 'manifests' and response.headers.get('Docker-Content-Digest') != digest:
                    raise RuntimeError('Registry manifest digest verification failed.')
                return json.loads(raw), digest
        except HTTPError as error:
            if missing_ok and error.code == 404:
                return None, ''
            raise RuntimeError('Registry inspection failed; refusing to overwrite an existing version.') from None

    def version_digest(self, version, revision):
        index, digest = self.fetch('manifests', version, missing_ok=True)
        if index is None:
            return ''
        platforms = set()
        for descriptor in index.get('manifests', []):
            platform = descriptor.get('platform', {})
            if platform.get('os') != 'linux':
                continue  # Attestations are retained in the original index.
            manifest, _ = self.fetch('manifests', descriptor['digest'])
            config, _ = self.fetch('blobs', manifest['config']['digest'])
            labels = config['config'].get('Labels', {})
            if (labels.get('org.opencontainers.image.revision') != revision
                    or labels.get('org.opencontainers.image.version') != version):
                raise RuntimeError('Existing version image does not match the release; refusing to repoint it.')
            platforms.add(platform.get('architecture'))
        if platforms != {'amd64', 'arm64'}:
            raise RuntimeError('Existing version is not the expected multi-platform release; refusing to repoint it.')
        return digest


def newest_stable(repository):
    # Do not rely on API listing order or infer approval from a pushed tag.
    candidates = []
    page = 1
    while True:
        request = Request(f'https://api.github.com/repos/{repository}/releases?per_page=100&page={page}', headers={
            'Authorization': 'Bearer ' + os.environ['GH_TOKEN'], 'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        })
        with urlopen(request, timeout=30) as response:
            releases = json.load(response)
        candidates.extend(release for release in releases if stable(release))
        if len(releases) < 100:
            break
        page += 1
    return max(candidates, key=lambda release: (release['published_at'], release['id']), default=None)


def promote(event_name, event, ref, repository, revision, digest, registry):
    selected = selection(event_name, event, ref)
    if not selected['stable']:
        return False
    current = newest_stable(repository)
    if not current or current['id'] != event['release']['id'] or current['tag_name'] != selected['version']:
        return False  # An older delayed/rerun workflow cannot roll floating tags back.
    actual = registry.version_digest(selected['version'], revision)
    if not digest or actual != digest:
        raise RuntimeError('Release image digest mismatch; stable channels were not changed.')
    image = 'ghcr.io/' + repository.lower()
    subprocess.run(['docker', 'buildx', 'imagetools', 'create', '--tag', image + ':latest',
                    '--tag', image + ':stable', image + '@' + digest], check=True)
    for tag in ('latest', 'stable'):
        _, observed = registry.fetch('manifests', tag)
        if observed != digest:
            raise RuntimeError('Floating channel verification failed.')
    return True


def main():
    import sys
    event = json.load(open(os.environ['GITHUB_EVENT_PATH']))
    event_name = os.environ['GITHUB_EVENT_NAME']
    ref = os.environ['GITHUB_REF']
    selected = selection(event_name, event, ref)
    if sys.argv[1] == 'select':
        output(**selected)
        return
    repository = os.environ['GITHUB_REPOSITORY']
    revision = os.environ['GITHUB_SHA']
    if sys.argv[1] == 'existing':
        digest = Registry(repository).version_digest(selected['version'], revision) if selected['version'] else ''
        output(exists=bool(digest), digest=digest)
    elif sys.argv[1] == 'promote':
        if not selected['stable']:
            raise RuntimeError('This event does not authorize stable-channel publication.')
        promoted = promote(event_name, event, ref, repository, revision,
                           os.environ['IMAGE_DIGEST'], Registry(repository))
        print('Stable release channels verified.' if promoted else 'Superseded release: floating channels unchanged.')
    else:
        raise ValueError('Unknown release-channel command.')


if __name__ == '__main__':
    main()
