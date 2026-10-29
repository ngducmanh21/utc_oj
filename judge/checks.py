from django.core.checks import Error, Tags, register
from django.core.exceptions import ImproperlyConfigured

from judge.utils.registration import get_registration_email_domains
from judge.utils.user_import_config import get_user_import_config


@register(Tags.security)
def check_registration_email_domains(app_configs, **kwargs):
    try:
        get_registration_email_domains()
    except ImproperlyConfigured as exc:
        return [Error(str(exc), id='judge.E001')]
    return []


@register(Tags.security)
def check_user_import_config(app_configs, **kwargs):
    try:
        get_user_import_config()
    except ImproperlyConfigured as exc:
        return [Error(str(exc), id='judge.E002')]
    return []
