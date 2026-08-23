# Reverse proxies and HTTPS

Northbound can run directly, behind a traditional reverse proxy, or behind Cloudflare Tunnel. The application does not require a particular proxy product.

## HTTPS deployment

Set the public address and enable trusted forwarded headers:

```dotenv
NORTHBOUND_URL=https://northbound.example.com
NORTHBOUND_TRUST_PROXY_HEADERS=1
```

The public URL adds its hostname to Django's allowed hosts and its origin to CSRF's trusted origins. An HTTPS public URL also makes session and CSRF cookies secure by default.

Your proxy must:

- send the original `Host` header;
- set `X-Forwarded-Proto` to `https` for HTTPS requests;
- overwrite client-supplied forwarded headers;
- forward to Northbound's published HTTP port.

Only enable `NORTHBOUND_TRUST_PROXY_HEADERS` when every request reaches Northbound through a trusted proxy or tunnel.

## Cloudflare Tunnel

Point the tunnel service at Northbound's local address, such as `http://localhost:8000`, and set `NORTHBOUND_URL` to the public `https://` hostname. Enable trusted proxy headers as shown above.

Cloudflare Access is optional and separate from Northbound's own accounts and permissions.

## Multiple hostnames

Advanced deployments can add comma-separated overrides:

```dotenv
DJANGO_ALLOWED_HOSTS=northbound.example.com,books.example.net
DJANGO_CSRF_TRUSTED_ORIGINS=https://northbound.example.com,https://books.example.net
```

## HTTPS and direct LAN HTTP

Secure cookies issued for an HTTPS deployment are intentionally not sent over plain HTTP. Consequently, authenticated access through both `https://northbound.example.com` and `http://192.168.x.x` cannot retain the strongest cookie policy.

Prefer HTTPS for all authenticated access. Disabling secure cookies for mixed HTTP/HTTPS use is an explicit security tradeoff, not a recommended default.
