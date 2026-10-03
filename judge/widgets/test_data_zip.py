import os

from django.forms import ClearableFileInput, FileInput
from django.template.loader import get_template
from django.utils.safestring import mark_safe


class TestDataZipWidget(ClearableFileInput):
    filename = None

    def render(self, name, value, attrs=None, renderer=None):
        context = self.get_context(name, value, attrs)
        widget = context['widget']
        widget['filename'] = self.filename or os.path.basename(getattr(value, 'name', '') or '')
        widget['input'] = FileInput().render(name, value, widget['attrs'], renderer)
        return mark_safe(get_template('widgets/test_data_zip.html').render(context))
