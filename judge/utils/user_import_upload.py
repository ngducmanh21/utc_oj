"""Bounded memory-only multipart uploads for CSVs containing credentials."""

from io import BytesIO

from django.core.files.uploadedfile import InMemoryUploadedFile
from django.core.files.uploadhandler import FileUploadHandler, StopUpload


class UserImportUploadHandler(FileUploadHandler):
    def __init__(self, request, limit):
        super().__init__(request)
        self.limit = limit
        self.received = 0
        self.files = 0

    def new_file(self, *args, **kwargs):
        super().new_file(*args, **kwargs)
        self.files += 1
        if self.files > 1 or self.field_name != 'csv_file':
            self.request.user_import_upload_invalid = True
            raise StopUpload()
        self.file = BytesIO()

    def receive_data_chunk(self, raw_data, start):
        self.received += len(raw_data)
        if self.received > self.limit:
            self.request.user_import_upload_too_large = True
            self.file.close()
            raise StopUpload()
        self.file.write(raw_data)

    def file_complete(self, file_size):
        self.file.seek(0)
        return InMemoryUploadedFile(self.file, self.field_name, self.file_name,
                                    self.content_type, file_size, self.charset)

    def upload_interrupted(self):
        if hasattr(self, 'file'):
            self.file.close()
