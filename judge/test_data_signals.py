from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models.signals import pre_delete, pre_save
from django.dispatch import receiver

from judge.models import Problem, ProblemData, ProblemTestCase


@receiver(pre_delete, sender=ProblemData)
@receiver(pre_delete, sender=ProblemTestCase)
@receiver(pre_delete, sender=Problem)
def guard_test_data_delete(sender, instance, **kwargs):
    if not getattr(settings, 'DMOJ_TEST_UPLOAD_ENABLED', False):
        return
    from judge.utils.problem_data_publication import guard_test_data_write
    problem_id = instance.pk if sender is Problem else (
        instance.problem_id if sender is ProblemData else instance.dataset_id)
    guard_test_data_write(problem_id)


@receiver(pre_save, sender=Problem)
def guard_problem_location(sender, instance, **kwargs):
    if not instance.pk or not getattr(settings, 'DMOJ_TEST_UPLOAD_ENABLED', False):
        return
    old = Problem.objects.filter(pk=instance.pk).values(
        'code', 'storage', 'is_manually_managed', 'archived_at', 'deleted_at',
    ).first()
    if old and any(getattr(instance, name) != value for name, value in old.items()):
        if any(getattr(instance, name) != old[name] for name in ('code', 'storage', 'is_manually_managed')):
            from judge.utils.problem_data_publication import guard_test_data_write
            guard_test_data_write(instance.pk)
        else:
            # Archiving/soft-deleting after a finished edit is legitimate. Only
            # block these operations while a live editor/publication owns the row.
            from judge.utils.test_data_upload import UploadError, assert_problem_unlocked
            try:
                assert_problem_unlocked(Problem.objects.select_for_update().get(pk=instance.pk))
            except UploadError as error:
                raise ValidationError(str(error))
