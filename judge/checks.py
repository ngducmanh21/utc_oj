from django.core.checks import Error, Tags, register
from django.core.exceptions import ImproperlyConfigured

from judge.utils.registration import get_registration_email_domains


@register(Tags.security)
def check_registration_email_domains(app_configs, **kwargs):
    try:
        get_registration_email_domains()
    except ImproperlyConfigured as exc:
        return [Error(str(exc), id='judge.E001')]
    return []
