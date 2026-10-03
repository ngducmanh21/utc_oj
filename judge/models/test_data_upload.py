import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class ProblemDataEditSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    token = models.UUIDField(default=uuid.uuid4, editable=False)
    problem = models.ForeignKey('Problem', on_delete=models.PROTECT, related_name='data_edit_sessions')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    status = models.CharField(max_length=12, default='ACTIVE', db_index=True)
    created_at = models.DateTimeField(default=timezone.now)
    last_activity = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()


class TestDataUpload(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    session = models.ForeignKey(ProblemDataEditSession, on_delete=models.PROTECT, related_name='uploads')
    filename = models.CharField(max_length=255)
    fingerprint = models.CharField(max_length=128, blank=True)
    size = models.PositiveBigIntegerField()
    actual_size = models.PositiveBigIntegerField(default=0)
    reserved_bytes = models.PositiveBigIntegerField(default=0)
    status = models.CharField(max_length=12, default='CREATED', db_index=True)
    entries = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    last_activity = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
