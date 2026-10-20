from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class ManagedAccountEvent(models.Model):
    """Security events with structured metadata, never credentials or request payloads."""

    class Event(models.TextChoices):
        POLICY_ENABLED = 'policy_enabled', _('Policy enabled')
        POLICY_DISABLED = 'policy_disabled', _('Policy disabled')
        PASSWORD_ROTATED = 'password_rotated', _('Password rotated after login')
        CREDENTIAL_REISSUED = 'credential_reissued', _('Password changed by administrator')

    target = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                               related_name='managed_account_events')
    target_username = models.CharField(max_length=150, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='managed_account_actions')
    event = models.CharField(max_length=32, choices=Event.choices)
    created_at = models.DateTimeField(auto_now_add=True)
    reason = models.CharField(max_length=255, blank=True)
    previous_enabled = models.BooleanField()
    enabled = models.BooleanField()
    previous_epoch = models.PositiveBigIntegerField()
    epoch = models.PositiveBigIntegerField()

    class Meta:
        ordering = ['-created_at', '-pk']
        default_permissions = ('view',)
        indexes = [models.Index(fields=['target', '-created_at'], name='managed_evt_target_time')]
