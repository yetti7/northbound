"""Credential-bearing provider requests must never follow redirects."""
from urllib.request import HTTPRedirectHandler, build_opener


class NoCredentialRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def urlopen(request, *, timeout):
    return build_opener(NoCredentialRedirect()).open(request, timeout=timeout)
