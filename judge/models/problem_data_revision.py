import uuid

from django.db import models


class ProblemDataRevision(models.Model):
    """Durable publication journal; referenced files are retained for running judges."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    problem = models.ForeignKey('judge.Problem', on_delete=models.PROTECT, related_name='data_revisions')
    session = models.OneToOneField('judge.ProblemDataEditSession', on_delete=models.PROTECT,
                                   related_name='revision')
    upload = models.ForeignKey('judge.TestDataUpload', null=True, blank=True, on_delete=models.PROTECT)
    status = models.CharField(max_length=16, default='PREPARED', db_index=True,
                              choices=[(s, s) for s in ('PREPARED', 'APPLYING', 'APPLIED', 'FAILED')])
    previous_data = models.JSONField(default=dict)
    data = models.JSONField(default=dict)
    cases = models.JSONField(default=list)
    previous_init = models.TextField(null=True)
    # An empty string journals removal of init.yml for a cleared dataset.
    init = models.TextField()
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
