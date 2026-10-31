#!/usr/bin/env python3
"""Create private, workstation-only CSVs for the production operator runbook."""

import argparse
import csv
import json
import os
import re
import secrets
from pathlib import Path

HEADERS = ['username', 'password', 'name', 'school', 'email', 'organizations', 'password_rotation']


def create_smoke(directory, prefix):
    if not re.fullmatch(r'[a-z][a-z0-9_]{4,40}', prefix):
        raise ValueError('Use a unique lowercase prefix of 5-41 letters, digits or underscores.')
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    users = {kind: prefix + '_' + kind for kind in ('existing', 'off', 'on', 'invalid')}

    def password():
        return 'T9!' + secrets.token_urlsafe(24)

    def row(kind, secret='', name='', policy='false'):
        return [users[kind], secret, name, '', '', '', policy]

    valid = [row('existing', password(), 'Smoke updated'), row('off', password(), 'Smoke OFF'),
             row('on', password(), 'Smoke ON', 'true')]
    files = {
        '01-seed.csv': [row('existing', password(), 'Smoke before')],
        '02-invalid.csv': [*valid, row('invalid')],
        '03-import.csv': valid,
        '04-unmute.csv': [row('existing'), row('off'), row('on', policy='true')],
    }
    for name, rows in files.items():
        descriptor = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(HEADERS)
            writer.writerows(rows)
    manifest = {'prefix': prefix, 'users': users, 'files': list(files),
                'expected_import': {'created': 2, 'updated': 1, 'applied': 3}}
    descriptor = os.open(directory / 'manifest.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path, help='New private directory on your workstation, outside Git')
    parser.add_argument('--prefix', required=True, help='Unique test-account prefix; verify it is unused first')
    args = parser.parse_args()
    try:
        create_smoke(args.directory, args.prefix)
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + '\n')
    print('Created 4 private CSVs and a password-free manifest in ' + str(args.directory))
