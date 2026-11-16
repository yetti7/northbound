"""Keep reset bearer tokens out of application and Gunicorn access logs."""
import logging
import re


class ResetLinkRedaction(logging.Filter):
    pattern = re.compile(r'(/accounts/password/reset/)[^/\s?]+/[^/\s?]+/?')

    def filter(self, record):
        record.msg = self.pattern.sub(r'\1[redacted]/', record.getMessage())
        record.args = ()
        return True


def install_reset_log_redaction():
    for name in ('gunicorn.access', 'django.server', 'django.request'):
        logger = logging.getLogger(name)
        if not any(isinstance(f, ResetLinkRedaction) for f in logger.filters):
            logger.addFilter(ResetLinkRedaction())
