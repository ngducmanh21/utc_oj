from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils.translation import gettext_lazy as _

REGISTRATION_EMAIL_DOMAIN = 'lms.utc.edu.vn'
REGISTRATION_EMAIL_ERROR = _('Only email addresses ending in @%(domain)s are accepted.')


def validate_registration_email(value):
    email = (value or '').strip()
    validate_email(email)
    local, domain = email.rsplit('@', 1)
    if domain.lower() != REGISTRATION_EMAIL_DOMAIN:
        raise ValidationError(REGISTRATION_EMAIL_ERROR, code='email_domain',
                              params={'domain': REGISTRATION_EMAIL_DOMAIN})
    return '%s@%s' % (local, domain.lower())
