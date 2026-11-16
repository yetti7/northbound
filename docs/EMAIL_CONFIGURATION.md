# Outgoing email and local password recovery

## Administrator-managed SMTP

Platform Administrators open **Platform Administration → System Management → Email Settings**. Email and password recovery are disabled by default for an installation without saved configuration. Normal login and account management remain available without SMTP.

Configure the master Enable Email switch, SMTP host, port (default 587), one encryption mode (STARTTLS, SSL/TLS or None), optional SMTP authentication, sender name/email and optional Reply-To. Authenticated SMTP requires a username and password; an unauthenticated relay may leave both blank. No provider or personal sender is hardcoded. Gmail is one supported provider, not a deployment requirement. Saving settings does not connect to the provider, and provider changes do not require rebuilding Northbound.

SMTP passwords use Northbound's existing Fernet credential storage. Saved passwords are never redisplayed. Leaving the password field blank preserves its encrypted value; entering a new password replaces it deliberately. Passwords are excluded from audit and diagnostic output.

## Test email and status

The saved SMTP configuration is read for each send. The administrator can edit the test recipient; its initial value is the current administrator's email, when available. A successful test means the SMTP server accepted the message, not that inbox delivery was confirmed. Failures use sanitized categories rather than sensitive SMTP responses. Changing configuration invalidates the previous test status.

A ten-second connection timeout bounds network operations, although a synchronous SMTP exchange can involve multiple timeout periods. Tests use POST and CSRF protection, require Platform Administrator authority, and are limited to five attempts per administrator per hour. No background queue or worker is required.

## Password recovery

Enable both outgoing email and **Enable Password Resets** before requesting delivery. The existing login pages link to the branded recovery form. Known, unknown, inactive, disabled, rate-limited and delivery-failure requests receive the same generic confirmation. There is no console-email fallback.

Reset links use the configured `NORTHBOUND_URL` public origin, never an incoming Host or forwarded header. HTTPS is required except for loopback development. Django's established one-use reset tokens expire after **43200 seconds (12 hours)** and become invalid after password changes. Existing password policies apply. Duplicate email addresses follow Django's eligible-user behavior: each active account with a usable password receives its own link, without account names in the email body.

Requests allow three attempts per normalized email address and ten per direct client IP per fixed hour. Only keyed hashes, windows and counters are stored; expired identifiers are removed during subsequent requests. Forwarded IP headers are not trusted by this limiter, so users behind the same reverse proxy share its direct-IP allowance.

## Upgrade, backup and keys

No new environment variables, dependencies or services are required. Apply migration `0083_challenge_discord_moderator_opt_in` followed by `0084_platform_email_settings` before serving the new code. Production SMTP and recovery should remain disabled until the administrator configures them independently. Do not copy Development Gmail credentials into Production.

Preserve the existing `NORTHBOUND_TOKEN_ENCRYPTION_KEY`. Installations using the existing fallback must retain their original `DJANGO_SECRET_KEY`. Database backups contain encrypted credentials, preferences and test status, but not encryption keys. Keep the original active key separately. A different key causes credential decryption to fail closed with a sanitized recovery instruction; re-enter the SMTP password through Email Settings if needed. Review restored destinations and enable switches before using restored configuration elsewhere.

## Accepted validation and deployment decisions

Development acceptance completed on November 15, 2026: manually configured Gmail SMTP delivered real email, and the administrator received Jack's reset email, opened its trusted HTTPS link, saved a new password and successfully signed in. Automated tests mock transport and do not send real email. See [Development acceptance](V1_1_2_DEV_ACCEPTANCE.md).

The temporary private HTTPS relay initially set `Referrer-Policy: no-referrer`, causing the browser to submit `Origin: null` and fail CSRF. Changing only that relay header to `strict-origin` resolved the failure while withholding paths and query strings from referrers. That relay and its HTTPS listener were subsequently removed. Application-wide security behavior was not changed. Development returned to its original HTTP origin with Gmail SMTP retained and password-reset delivery disabled.

Production's existing HTTPS routing and internal HTTP forwarding are compatible with recovery and remain unchanged. NPM logging was investigated: local proxy logs can contain reset-token paths, query strings and referrers. The administrator explicitly accepted that local logging risk for this deployment. It was **not technically corrected**. No NPM configuration, Force SSL change or additional proxy infrastructure is required by the approved release plan. Application/Django and Gunicorn reset-path redaction does not sanitize upstream logs. Do not paste passwords or reset links into diagnostics.
