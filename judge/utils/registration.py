from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.validators import validate_email
from django.utils.translation import gettext_lazy as _

REGISTRATION_EMAIL_ERROR = _('Only email addresses ending in %(domains)s are accepted.')
REGISTRATION_CLOSED_ERROR = _('New account registration is temporarily closed.')


def get_registration_email_domains():
    configured = settings.DMOJ_REGISTRATION_EMAIL_DOMAINS
    if not isinstance(configured, (list, tuple)):
        raise ImproperlyConfigured('DMOJ_REGISTRATION_EMAIL_DOMAINS must be a list or tuple of domain strings.')
    domains = []
    for value in configured:
        if not isinstance(value, str):
            raise ImproperlyConfigured('DMOJ_REGISTRATION_EMAIL_DOMAINS must contain only domain strings.')
        domain = value.strip().lower()
        if (not domain or '.' not in domain or domain.endswith('.') or
                any(character in domain for character in '@/\\:*[]')):
            raise ImproperlyConfigured('DMOJ_REGISTRATION_EMAIL_DOMAINS contains an invalid domain: %r.' % value)
        try:
            validate_email('whitelist@' + domain)
        except ValidationError as exc:
            raise ImproperlyConfigured('DMOJ_REGISTRATION_EMAIL_DOMAINS contains an invalid domain: %r.' % value) \
                from exc
        if domain not in domains:
            domains.append(domain)
    return domains


def registration_email_message(domains):
    if not domains:
        return str(REGISTRATION_CLOSED_ERROR)
    return REGISTRATION_EMAIL_ERROR % {'domains': ', '.join('@' + domain for domain in domains)}


def validate_registration_email(value):
    domains = get_registration_email_domains()
    if not domains:
        raise ValidationError(REGISTRATION_CLOSED_ERROR, code='registration_closed')
    email = (value or '').strip()
    validate_email(email)
    local, domain = email.rsplit('@', 1)
    if domain.lower() not in domains:
        raise ValidationError(registration_email_message(domains), code='email_domain')
    return '%s@%s' % (local, domain.lower())
