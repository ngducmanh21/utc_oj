#!/usr/bin/env python3
"""Run the upload services in front of an existing local Django runserver."""
import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def binary(name, supplied, cache, previous):
    path = supplied or shutil.which(name)
    if path:
        return str(Path(path).resolve())
    cached = cache / name
    if not cached.exists() and Path(previous).is_file():
        cache.mkdir(parents=True, exist_ok=True)
        shutil.copy2(previous, cached)
    if cached.is_file():
        return str(cached)
    raise RuntimeError('Missing %s; install it or pass --%s-binary /path/to/%s.' % (name, name, name))


def nginx_path(path):
    return '"%s"' % str(path).replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--site-port', type=int, default=8000)
    parser.add_argument('--tus-port', type=int, default=1080)
    parser.add_argument('--redis-port', type=int, default=6381)
    parser.add_argument('--nginx-binary')
    parser.add_argument('--tusd-binary')
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'dmoj.settings')
    import django
    django.setup()
    from django.conf import settings
    from dmoj.celery import app

    if not settings.DEBUG or not settings.DMOJ_TEST_UPLOAD_ENABLED:
        raise RuntimeError('This launcher requires DEBUG=True and DMOJ_TEST_UPLOAD_ENABLED=True.')
    if not settings.DMOJ_TEST_UPLOAD_ROOT or not settings.DMOJ_TEST_UPLOAD_INTERNAL_SECRET:
        raise RuntimeError('Configure DMOJ_TEST_UPLOAD_ROOT and DMOJ_TEST_UPLOAD_INTERNAL_SECRET first; see local.md.')
    expected_broker = 'redis://127.0.0.1:%s/0' % args.redis_port
    if app.conf.broker_url != expected_broker:
        raise RuntimeError('Set the local Celery broker to %s before starting these services.' % expected_broker)
    staging = Path(settings.DMOJ_TEST_UPLOAD_ROOT)
    if not staging.is_absolute():
        raise RuntimeError('DMOJ_TEST_UPLOAD_ROOT must be an absolute directory.')
    staging.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime = staging / 'runtime'
    runtime.mkdir(mode=0o700, exist_ok=True)
    tusd = binary('tusd', args.tusd_binary, staging / 'bin', '/tmp/tusd_linux_amd64/tusd')
    nginx = binary('nginx', args.nginx_binary, staging / 'bin', '/tmp/utcoj-nginx-validation/usr/sbin/nginx')
    redis = shutil.which('redis-server')
    if not redis:
        raise RuntimeError('Install redis-server before starting the local upload services.')
    ports = [args.port, args.tus_port, args.redis_port]
    if len(set(ports + [args.site_port])) != 4:
        raise RuntimeError('The proxy, Django, tusd and Redis ports must be distinct.')
    for port in ports:
        with socket.socket() as probe:
            try:
                probe.bind(('127.0.0.1', port))
            except OSError:
                raise RuntimeError('Port %s is already occupied; stop the previous local upload launcher.' % port)
    with socket.create_connection(('127.0.0.1', args.site_port), timeout=3):
        pass

    snippet = (ROOT / 'docs/deployment/test-data-upload/nginx-uploads.conf').read_text()
    secret = runtime / 'upload-secret.conf'
    with secret.open('w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write('proxy_set_header X-Upload-Secret %s;\n' % nginx_path(settings.DMOJ_TEST_UPLOAD_INTERNAL_SECRET))
    snippet = snippet.replace('/etc/nginx/snippets/upload-secret.conf', nginx_path(secret))
    snippet = snippet.replace('http://site:8000', 'http://127.0.0.1:%s' % args.site_port)
    snippet = snippet.replace('http://tusd:1080', 'http://127.0.0.1:%s' % args.tus_port)
    (runtime / 'uploads.conf').write_text(snippet)
    config = runtime / 'nginx.conf'
    config.write_text("""pid %(pid)s;
error_log %(error)s;
events {}
http {
    access_log off;
    client_max_body_size 48m;
    client_body_temp_path %(body)s;
    proxy_temp_path %(proxy)s;
    fastcgi_temp_path %(fastcgi)s;
    uwsgi_temp_path %(uwsgi)s;
    scgi_temp_path %(scgi)s;
    server {
        listen 127.0.0.1:%(port)s;
        server_name localhost;
        include %(uploads)s;
        location / {
            proxy_pass http://127.0.0.1:%(site)s;
            proxy_set_header Host $http_host;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_read_timeout 120s;
        }
    }
}
""" % {**{name: nginx_path(runtime / name) for name in ('pid', 'error', 'body', 'proxy', 'fastcgi', 'uwsgi', 'scgi')},
       'port': args.port, 'site': args.site_port, 'uploads': nginx_path(runtime / 'uploads.conf')})
    subprocess.run([nginx, '-t', '-p', str(runtime), '-c', str(config)], check=True)
    redis_dir = runtime / 'redis'
    redis_dir.mkdir(exist_ok=True)
    children, logs = [], []

    def start(name, command):
        log = (runtime / (name + '.log')).open('ab')
        logs.append(log)
        child = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        children.append((name, child))
        return child

    def wait_port(port):
        for _ in range(100):
            for name, child in children:
                if child.poll() is not None:
                    raise RuntimeError('%s stopped; see %s.' % (name, runtime / (name + '.log')))
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError('Port %s did not become ready; see logs in %s.' % (port, runtime))

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        start('redis', [redis, '--bind', '127.0.0.1', '--port', str(args.redis_port),
                        '--dir', str(redis_dir), '--appendonly', 'yes'])
        wait_port(args.redis_port)
        start('celery', [sys.executable, '-m', 'celery', '-A', 'dmoj_celery', 'worker',
                         '--pool=solo', '--concurrency=1', '-l', 'info', '-n', 'utcoj-upload-local@%h'])
        start('tusd', [tusd, '-host=127.0.0.1', '-port=%s' % args.tus_port, '-base-path=/uploads/',
                       '-upload-dir=%s' % staging, '-max-size=%s' % settings.DMOJ_TEST_UPLOAD_MAX_SIZE,
                       '-behind-proxy', '-disable-download', '-disable-termination', '-disable-cors',
                       '-hooks-http=http://127.0.0.1:%s/internal/test-data-uploads/hooks/' % args.site_port,
                       '-hooks-http-forward-headers=X-Test-Upload-Token,X-Upload-Secret,X-Forwarded-Proto',
                       '-hooks-enabled-events=pre-create,post-create,post-receive,post-finish',
                       '-progress-hooks-interval=30s', '-hooks-http-retry=5', '-verbose=false'])
        wait_port(args.tus_port)
        start('nginx', [nginx, '-g', 'daemon off;', '-p', str(runtime), '-c', str(config)])
        wait_port(args.port)
        print('Open http://127.0.0.1:%s/ (Django backend remains on %s).' % (args.port, args.site_port), flush=True)
        print('Logs: %s. Ctrl+C stops these upload services.' % runtime, flush=True)
        while True:
            for name, child in children:
                if child.poll() is not None:
                    raise RuntimeError('%s stopped; see %s.' % (name, runtime / (name + '.log')))
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for name, child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        for log in logs:
            log.close()


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
