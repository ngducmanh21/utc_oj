#!/usr/bin/env python3
"""Provision the upload directory and shared secret without printing credentials."""
import argparse
import os
import re
import secrets
import subprocess
import tempfile
from pathlib import Path


def container_identity(name):
    command = ['docker', 'exec', name, 'python3', '-c',
               'import os; print(os.geteuid(), os.getegid())']
    return tuple(map(int, subprocess.check_output(command, text=True).strip().split()))


def private_write(path, content):
    fd, temporary = tempfile.mkstemp(prefix='.%s-' % path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deploy-dir', type=Path, default=Path('/opt/utcoj/deploy'))
    parser.add_argument('--site-container', default='utcoj-site-1')
    parser.add_argument('--celery-container', default='utcoj-celery-1')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run this provisioning command with sudo.')
    identity = container_identity(args.site_container)
    if container_identity(args.celery_container) != identity:
        parser.error('Site and Celery have different UID/GID. Configure a shared filesystem group first.')
    deploy = args.deploy_dir.resolve()
    if not (deploy / 'compose.yaml').is_file():
        parser.error('The deployment directory must contain the existing compose.yaml.')
    env_file = deploy / '.env'
    if env_file.is_symlink():
        parser.error('Resolve the deployment .env symbolic link before provisioning.')
    contents = env_file.read_text() if env_file.exists() else ''
    values = {}
    for name in ('TEST_UPLOAD_INTERNAL_SECRET', 'UPLOAD_UID', 'UPLOAD_GID'):
        matches = re.findall(r'^\s*' + name + r'\s*=\s*(.*?)\s*$', contents, re.MULTILINE)
        if len(matches) > 1:
            parser.error('Duplicate %s in .env; reconcile it first.' % name)
        if matches:
            values[name] = matches[0].strip('"\'')
    secret = values.get('TEST_UPLOAD_INTERNAL_SECRET') or secrets.token_hex(32)
    if not re.fullmatch(r'[0-9a-fA-F]{64}', secret):
        parser.error('Use a 64-character hexadecimal TEST_UPLOAD_INTERNAL_SECRET.')
    desired = dict(TEST_UPLOAD_INTERNAL_SECRET=secret, UPLOAD_UID=str(identity[0]), UPLOAD_GID=str(identity[1]))
    for name, value in desired.items():
        if name in values and values[name] != value:
            parser.error('%s differs from the container identity; reconcile it first.' % name)
    root = deploy.parent / 'data' / 'test-uploads'
    if root.is_symlink():
        parser.error('The staging directory must not be a symbolic link.')
    if root.exists() and (root.stat().st_uid, root.stat().st_gid) != identity:
        parser.error('Existing staging has different ownership; inspect it before changing permissions.')
    root.mkdir(mode=0o770, parents=True, exist_ok=True)
    root.chmod(0o770)
    os.chown(root, *identity)
    snippets = deploy / 'test-data-upload'
    snippets.mkdir(mode=0o755, exist_ok=True)
    for name, value in desired.items():
        if name not in values:
            contents = contents.rstrip('\n') + '\n%s=%s\n' % (name, value)
    # Both files live outside the repository. Never print their contents.
    private_write(env_file, contents)
    secret_file = snippets / 'upload-secret.conf'
    private_write(secret_file, 'proxy_set_header X-Upload-Secret "%s";\n' % secret)
    print('Staging provisioned for UID=%s GID=%s.' % identity)
    print('Shared secret saved to deployment .env and test-data-upload/upload-secret.conf.')
    print('Keep both files private. Restart site, Celery and Nginx after configuration changes.')


if __name__ == '__main__':
    main()
