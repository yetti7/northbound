# Reverse proxies and HTTPS

Canonical public-origin and proxy guidance. Northbound serves HTTP and media in
its application container; an external proxy can terminate HTTPS. No particular
proxy product or additional internal proxy container is required.
See [Self-hosting](SELF_HOSTING.md) for container ports and persistence.

## Public origin

Use the address Readers actually visit, without a path prefix:

```dotenv
NORTHBOUND_URL=https://northbound.example.org
NORTHBOUND_TRUST_PROXY_HEADERS=1
DJANGO_DEBUG=0
```

The public URL adds its hostname to allowed hosts and its origin to trusted CSRF
origins. With debug disabled, an HTTPS public origin defaults session and CSRF
cookies to Secure. Restart via container recreation after configuration changes.

## Trust boundary

Enable forwarded-header trust only when every external request reaches Northbound
through your trusted proxy/tunnel. Restrict the published application port with
binding/firewall rules; do not leave a public bypass around the proxy.

The proxy must:

- Forward to the configured Northbound HTTP port.
- Preserve the intended public Host, and overwrite client-supplied
  `X-Forwarded-Host` and `X-Forwarded-Proto`; for HTTPS use `https`.
- Keep request bodies and Authorization headers out of logs. Omit callback query
  strings and referrers so OAuth codes/state cannot leak through upstream logging.
- Allow required uploads within intentional limits; align proxy body-size limits
  with the application, particularly when using backup uploads.

Northbound enables both forwarded-host and forwarded-protocol handling when
`NORTHBOUND_TRUST_PROXY_HEADERS=1`. A proxy that passes arbitrary client-supplied
forwarded headers is not trusted safely.

For a same-host proxy, loopback binding is appropriate. A proxy/tunnel in another
container cannot reach Northbound through its own `localhost`; configure a reachable
upstream using your existing container network or restricted host address.
Remote proxies likewise require an explicitly reachable, restricted upstream.

## Extra hostnames and HTTPS options

Optional comma-separated overrides:

```dotenv
DJANGO_ALLOWED_HOSTS=northbound.example.org,books.example.net
DJANGO_CSRF_TRUSTED_ORIGINS=https://northbound.example.org,https://books.example.net
```

Extra hosts do not change the canonical OAuth callback, which still derives only
from `NORTHBOUND_URL`.

Production settings also support `DJANGO_SESSION_COOKIE_SECURE`,
`DJANGO_CSRF_COOKIE_SECURE`, `DJANGO_SECURE_SSL_REDIRECT`,
`DJANGO_SECURE_HSTS_SECONDS`, `DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS` and
`DJANGO_SECURE_HSTS_PRELOAD`. Redirect and HSTS settings are not enabled by default.
Enable them only after verifying HTTPS/proxy behavior; do not enable HSTS for names
you cannot consistently serve over HTTPS.

Secure cookies are not sent over plain HTTP. Prefer HTTPS for all authenticated
access rather than weakening cookies to support mixed public-HTTPS/LAN-HTTP access.
The exact `/health/` endpoint checks only the database; see
[health behavior](SELF_HOSTING.md#startup-and-health).
