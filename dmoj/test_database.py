"""Loopback-only connection overrides for disposable test databases."""

import os

from django.core.exceptions import ImproperlyConfigured


def configure_test_database(database):
    port = os.environ.get('UTCOJ_TEST_DB_PORT')
    if port is None:
        return
    if not port.isdecimal() or not 1024 <= int(port) <= 65535:
        raise ImproperlyConfigured('UTCOJ_TEST_DB_PORT must be a nonprivileged loopback port.')
    database.update(HOST='127.0.0.1', PORT=int(port),
                    USER=os.environ.get('UTCOJ_TEST_DB_USER', 'root'),
                    PASSWORD=os.environ.get('UTCOJ_TEST_DB_PASSWORD', ''))
