# Triển khai upload test lớn

Các file này bổ sung tính năng cho hệ thống đang chạy tại `/opt/utcoj`: Nginx,
site/Celery dùng image `utcoj-app:local`, MariaDB/Redis và bốn judge dùng chung
`/opt/utcoj/data/problems`. Tính năng tắt mặc định. Các lệnh dưới đây dành cho
operator chạy trên server; việc thêm tài liệu không thay đổi production.

Để thử trên máy phát triển với Django `runserver`, dùng [hướng dẫn local](local.md).

## 1. Cấu hình được giữ và bổ sung

| Thành phần | Thay đổi |
| --- | --- |
| Site, Celery | Nhận mount `/test-uploads` và cùng secret nội bộ |
| tusd | Container mới `tusproject/tusd:v2.8.0`, không publish cổng host |
| Nginx | Route `/uploads/` qua kiểm tra quyền, rồi proxy HTTP tới `tusd:1080` |
| Kho chính thức và judge | Giữ mount `/problems` và các file `judge*.yml` hiện có |
| Cleanup/recovery | systemd timer 15 phút, gọi management command trong site |

Các giá trị mặc định trong `dmoj/settings.py`: chunk **16 MiB**, request tối đa
**20 MiB** tại Nginx, ZIP **1 GiB** (1.073.741.824 byte) mỗi file, hai lượt truyền đồng thời, một validator nặng,
khóa không hoạt động **15 phút**, tuổi tối đa phiên **24 giờ**, đĩa dự phòng
**5 GiB**. Không có quota user/organization cho luồng này. ZIP còn được giới hạn
10.000 entry, central directory 16 MiB, tổng byte giải nén 20 GiB và thời gian
kiểm tra 600 giây. Validator kiểm tra nội dung theo từng phần, không giải nén
toàn bộ ZIP ra đĩa.

`compose.uploads.yaml` phải là file override **thứ ba**, sau hai file hiện có.
Docker Compose hợp nhất mount theo đường dẫn đích; file này giữ các mount,
biến database/email và cấu hình cũ. [Quy tắc merge Compose](https://docs.docker.com/reference/compose-file/merge/)

## 2. Chuẩn bị bản phát hành và staging

Đưa bản code đã kiểm thử vào `/opt/utcoj/site` theo quy trình release hiện tại;
giữ nguyên `dmoj/local_settings.py` và các bí mật trên server. Sao lưu database,
`/opt/utcoj/deploy`, cấu hình local và bộ test sẽ thử theo quy trình backup hiện
có. Xác nhận backup phục hồi được trước khi bật luồng mới.

Ghi nhận image đang dùng và tài nguyên:

```bash
sudo docker inspect utcoj-site-1 --format 'Image={{.Image}}'
sudo docker inspect utcoj-judge-1 --format 'Image={{.Image}}'
df -hT /opt/utcoj/data/problems
free -h
```

Cài các mẫu deployment vào thư mục hiện có:

```bash
sudo install -d -m 0755 /opt/utcoj/deploy/test-data-upload
sudo install -m 0644 /opt/utcoj/site/docs/deployment/test-data-upload/compose.uploads.yaml /opt/utcoj/deploy/compose.uploads.yaml
sudo install -m 0644 /opt/utcoj/site/docs/deployment/test-data-upload/nginx-uploads.conf /opt/utcoj/deploy/test-data-upload/nginx-uploads.conf
sudo python3 /opt/utcoj/site/docs/deployment/test-data-upload/provision-storage.py
```

Helper cuối kiểm tra site và Celery có cùng UID/GID, tạo
`/opt/utcoj/data/test-uploads` với quyền `0770`, thêm `UPLOAD_UID`, `UPLOAD_GID`,
`TEST_UPLOAD_INTERNAL_SECRET` vào `.env` deployment và tạo
`test-data-upload/upload-secret.conf` với cùng secret. Hai file chứa secret được
ghi nguyên tử với quyền `0600`; không in secret. Helper chạy lại giữ nguyên secret
đã tạo. Nó từ chối owner hoặc giá trị khác nhau để operator xử lý rõ ràng, thay
vì thay quyền file đang upload. Không dùng `chmod 777`.

UID/GID kiểm tra là user được cấu hình cho container. Compose được cung cấp không
chạy uWSGI/Celery với tùy chọn hạ quyền; nếu cấu hình image thực tế có hạ quyền,
đối chiếu UID/GID của process trước khi provision. Nếu app đang chạy root, helper
sẽ giữ UID/GID đó cho tusd; đổi user app là thay đổi vận hành riêng cần làm đồng
bộ cho cả site và Celery.

## 3. Settings Django

Merge nội dung `settings.py.example` vào cuối
`/opt/utcoj/site/dmoj/local_settings.py`. Giữ database, email, cache, broker hiện
có. Ban đầu để:

```python
import os

DMOJ_TEST_UPLOAD_ROOT = '/test-uploads'
DMOJ_TEST_UPLOAD_INTERNAL_SECRET = os.environ.get('TEST_UPLOAD_INTERNAL_SECRET', '')
DMOJ_TEST_UPLOAD_ENABLED = False
DMOJ_TEST_UPLOAD_MAX_SIZE = 1024 ** 3
DMOJ_TEST_UPLOAD_ACCEPT_NEW = True
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
ALLOWED_HOSTS = [*ALLOWED_HOSTS, 'site']
```

Nginx phải ghi đè `X-Forwarded-Proto`; tusd chuyển header này tới hook. Đây là yêu
cầu nếu production bật `SECURE_SSL_REDIRECT`: hook HTTP nội bộ không được redirect
sang `https://site`. `site:8000` chỉ là HTTP listener nội bộ đã có, không publish
cổng mới. Cookie đăng nhập phải có path `/` để được gửi tới `/uploads/`.

Kiểm tra Celery dùng Redis hiện có. `dmoj/celery.py` ưu tiên
`CELERY_BROKER_URL_SECRET` nếu được khai báo. Không đổi broker của worker đang chạy
mà chưa đối chiếu các tác vụ khác. Không cần Celery result backend riêng để hiển
thị tiến trình; trạng thái nằm trong database.

Dùng `.get()` cho secret vì bridge cũng đọc cùng `local_settings.py` nhưng không
nhận env/mount upload. Site/Celery được Compose bắt buộc cấp secret; phần kiểm
tra bên dưới xác nhận site có giá trị. Internal API từ chối khi thiếu secret.

Kiểm tra và migrate bằng container một lần, chưa bật tính năng:

```bash
cd /opt/utcoj/deploy
sudo docker compose -f compose.yaml -f compose.override.yaml -f compose.uploads.yaml config --quiet
sudo docker compose -f compose.yaml -f compose.override.yaml -f compose.uploads.yaml run --rm --no-deps site python3 manage.py check
sudo docker compose -f compose.yaml -f compose.override.yaml -f compose.uploads.yaml run --rm --no-deps site python3 manage.py migrate --plan
sudo docker compose -f compose.yaml -f compose.override.yaml -f compose.uploads.yaml run --rm --no-deps site python3 manage.py migrate
```

Không gửi toàn bộ `docker compose config` cho người khác vì output có thể chứa
bí mật đã nội suy. Nếu production lệch revision local, đối chiếu migration hiện
có trước khi migrate; không tự chạy migration ngược.

## 4. Static assets

Release chứa sẵn `resources/vendor/tus/tus.min.js` từ `tus-js-client@4.3.1` và
license đi kèm, cùng `test-data-upload.js`/`.css`. Không dùng CDN trong luồng này.
Nếu cần tạo lại asset trong môi trường build Node của project:

```bash
npm ci
npm run vendor:test-upload
npm run test:test-upload
```

Sau khi đồng bộ code, collectstatic vào volume `/assets` bằng settings production:

```bash
cd /opt/utcoj/deploy
sudo docker compose -f compose.yaml -f compose.override.yaml -f compose.uploads.yaml run --rm --no-deps site python3 manage.py collectstatic --noinput
```

Giữ các bước build CSS/compressor/compilemessages vốn có của quy trình release.
Ba asset mới dùng link static trực tiếp; `collectstatic` phải tìm được chúng.

## 5. Nginx và tusd

Trong **hai** `server {}` HTTPS đang phục vụ app (server mặc định `_` và server
`utcoj.info`), thêm dòng sau ở cấp server, bên ngoài `location /`:

```nginx
include /etc/nginx/snippets/test-data-uploads.conf;
```

Trong `location /` hiện có của cả hai server, thêm header protocol tin cậy:

```nginx
location / {
    include /etc/nginx/uwsgi_params;
    uwsgi_param HTTP_X_FORWARDED_PROTO $scheme;
    uwsgi_pass site:8001;
    uwsgi_read_timeout 120s;
}
```

Header được ghi đè này cần thiết khi Django tin `SECURE_PROXY_SSL_HEADER` ở phần
3; không để app tin header protocol tự khai từ client. Giữ các route
static/WebSocket và giới hạn `48m` chung. Server `www.utcoj.info` chỉ redirect không cần include. Mọi hostname phục
vụ app trong tương lai đều phải có snippet để không đưa internal API ra public.

Snippet thực hiện `auth_request` trước **mọi** method `/uploads/`, chuyển cookie
và `X-Test-Upload-Token` tới Django. Backend chỉ cho phép POST tạo file rỗng, PATCH
và HEAD trên đúng upload. GET tải ZIP, DELETE, OPTIONS và method khác bị từ chối.
Nút Hủy gọi API Django; tusd termination được tắt để cleanup giữ quyền xóa file.
Không dùng header `Authorization` cho upload vì middleware API hiện có dùng nó
cho token tài khoản. Header secret từ client luôn bị ghi đè tại Nginx.

Các endpoint `/internal/test-data-uploads/` trả 404 qua public Nginx. Tusd gửi hook
trực tiếp tới site; secret và token được forward trong header. `pre-create` cấp
ID file do Django chỉ định, các hook sau cập nhật/đối soát trạng thái. Hook HTTP
tusd yêu cầu response 2xx chứa JSON; endpoint hoàn tất chỉ xếp task Celery.
[HTTP hooks tusd v2.8.0](https://github.com/tus/tusd/blob/v2.8.0/pkg/hooks/http/http.go)

Khởi động site/Celery với mount và env mới, rồi tusd:

```bash
cd /opt/utcoj/deploy
sudo docker compose -f compose.yaml -f compose.override.yaml -f compose.uploads.yaml up -d --no-deps site celery
sudo docker compose -f compose.yaml -f compose.override.yaml -f compose.uploads.yaml pull tusd
sudo docker compose -f compose.yaml -f compose.override.yaml -f compose.uploads.yaml up -d --no-deps tusd
sudo docker compose -f compose.yaml -f compose.override.yaml -f compose.uploads.yaml exec -T tusd /usr/local/bin/tusd -version
sudo docker image inspect tusproject/tusd:v2.8.0 --format 'Digests={{json .RepoDigests}}'
```

Ghi digest vào hồ sơ release; có thể pin lại `image:` thành digest đã kiểm chứng
trước rollout rộng. Tất cả flag trong mẫu đã đối chiếu với
[CLI v2.8.0](https://github.com/tus/tusd/blob/v2.8.0/cmd/tusd/cli/flags.go).
Grace period Docker 40 giây cho phép tusd dùng shutdown timeout 30 giây.

Syntax test bằng container Nginx tạm dùng đúng mounts và mạng Compose, không mở
cổng host:

```bash
sudo docker compose -f compose.yaml -f compose.override.yaml -f compose.uploads.yaml run --rm --no-deps nginx nginx -t
```

Chỉ khi lệnh đó thành công, tạo lại Nginx để nhận **hai mount snippet mới**:

```bash
sudo docker compose -f compose.yaml -f compose.override.yaml -f compose.uploads.yaml up -d --no-deps nginx
sudo docker compose -f compose.yaml -f compose.override.yaml -f compose.uploads.yaml exec -T nginx nginx -t
sudo docker compose -f compose.yaml -f compose.override.yaml -f compose.uploads.yaml logs --tail=100 tusd site celery nginx
```

`nginx -s reload` đơn thuần không thêm được mount mới vào container cũ. Không dùng
`docker compose down`; database, Redis, bridge/WebSocket và judge tiếp tục chạy.
Với lần chỉnh Nginx sau, syntax test rồi reload là đủ nếu không đổi mounts.

## 6. Kiểm tra cấu hình trước khi bật

Không in secret/broker URL ra màn hình. Chạy các lệnh chỉ hiển thị thông tin an
toàn và kiểm tra kết nối broker:

```bash
sudo docker compose -f compose.yaml -f compose.override.yaml -f compose.uploads.yaml exec -T site python3 manage.py shell -c 'from django.conf import settings; from dmoj.celery import app; from pathlib import Path; assert settings.DMOJ_TEST_UPLOAD_INTERNAL_SECRET; assert settings.SESSION_COOKIE_PATH == "/"; print("staging:", Path(settings.DMOJ_TEST_UPLOAD_ROOT).is_dir()); print("reserve bytes:", settings.DMOJ_TEST_UPLOAD_DISK_RESERVE); print("broker:", app.connection().transport.driver_name); app.connection().ensure_connection(max_retries=1); print("broker connected")'
sudo docker compose -f compose.yaml -f compose.override.yaml -f compose.uploads.yaml exec -T celery celery -A dmoj_celery inspect registered
curl -sS -o /dev/null -w '%{http_code}\n' https://utcoj.info/internal/test-data-uploads/hooks/
curl -sS -o /dev/null -w '%{http_code}\n' https://utcoj.info/_test_upload_auth
curl -sS -o /dev/null -w '%{http_code}\n' https://utcoj.info/static/test-data-upload.js
```

Task `judge.tasks.test_data_upload.validate_test_data_upload` phải được đăng ký,
hai internal URL trả **404**, asset trả **200**. Khi flag tắt, `/uploads/` sẽ bị
từ chối, có thể là 500 do Nginx auth_request chuyển response 503 của backend;
không được upload byte nào. Sau khi bật, truy cập không đăng nhập/token phải 403.

Nếu site hoặc tusd vừa được tạo lại và Nginx giữ IP Docker cũ, chạy `nginx -t` rồi
reload/recreate Nginx. Nếu site thay IP và tusd báo hook lỗi kết nối, khởi động lại
tusd. Mạng nội bộ và DNS name không tự đảm bảo mọi upstream đã refresh IP.

## 7. Bật trên bài thử và nghiệm thu

Chỉ bật trên môi trường staging trước. Đổi
`DMOJ_TEST_UPLOAD_ENABLED = True` trong settings, rồi restart site và Celery:

```bash
sudo docker compose -f compose.yaml -f compose.override.yaml -f compose.uploads.yaml restart site celery
```

Flag hiện áp dụng toàn bộ người có quyền sửa dữ liệu test; **chưa có allowlist
theo user/bài**. Trên production cần chọn cửa sổ thử có kiểm soát, thông báo cho
người ra đề và giữ bài thử riêng tư. Người thứ hai vào bài đang có phiên sẽ bị
từ chối cấp khóa; người khác vẫn xem được trang.

Trong trình duyệt đăng nhập tại `https://utcoj.info`, mở DevTools Network và trang
dữ liệu test của bài thử. Kiểm tra:

1. Bấm bắt đầu chỉnh sửa; user hoặc tab thứ hai bị báo người đang giữ khóa.
2. ZIP nhỏ và ZIP thực tế **306 MB** upload thành nhiều PATCH tối đa **16 MiB**;
   Location phải trỏ tới cùng host, giao thức và cổng public với trang upload,
   theo đường dẫn `/uploads/<id>`, không có `site`/`tusd`. Nginx giữ cổng qua
   `$http_host`; cả `/uploads/` và subrequest kiểm tra quyền đều cho body 20 MiB.
3. Thanh tiến trình cập nhật trong chunk, có byte/phần trăm/tốc độ/ETA. Sau 100%
   upload phải chuyển sang kiểm tra ZIP, rồi sẵn sàng; chưa báo đã lưu.
4. Ngắt mạng giữa chunk, thử lại; reload và chọn lại đúng file. Offset không giảm
   về 0; file khác cùng tên/kích thước bị từ chối theo fingerprint.
5. Đợi READY, ghép testcase rồi Lưu. Form sai phải giữ ZIP và phần form đã nhập.
   Lưu đúng mới tạo revision và nhả khóa. Nhấn Lưu lại không công bố hai lần.
6. ZIP hỏng hoặc phiên bị thu hồi không thay test cũ; Hủy không xóa revision đã áp
   dụng. Thử checker-only edit để xác nhận không buộc upload ZIP mới.
7. Truy cập upload từ tài khoản khác, không cookie, token sai, GET hoặc DELETE
   phải 403. DevTools không được hiển thị secret nội bộ; không xuất HAR có cookie
   hoặc token để chia sẻ.
8. Thử restart tusd khi đang upload; retry/resume tiếp tục dùng file staging cũ.
9. Làm bài kiểm chứng bốn judge theo [judge-verification.md](judge-verification.md).

Không tạo ZIP bomb/hết đĩa thật trên production. Thử giới hạn file, đĩa dự phòng,
worker restart và lỗi DB trong staging. Nếu mạng chậm bị timeout qua Cloudflare,
giảm `DMOJ_TEST_UPLOAD_CHUNK_SIZE`, luôn giữ dưới `client_max_body_size 20m`;
timeout Nginx không thay thế giới hạn Cloudflare.

## 8. Timer, quan sát và phục hồi

Sau migration và kiểm tra staging mounts, cài timer:

```bash
sudo install -m 0644 /opt/utcoj/site/docs/deployment/test-data-upload/utcoj-test-uploads.service /etc/systemd/system/utcoj-test-uploads.service
sudo install -m 0644 /opt/utcoj/site/docs/deployment/test-data-upload/utcoj-test-uploads.timer /etc/systemd/system/utcoj-test-uploads.timer
sudo systemd-analyze verify /etc/systemd/system/utcoj-test-uploads.service /etc/systemd/system/utcoj-test-uploads.timer
sudo systemctl daemon-reload
sudo systemctl enable --now utcoj-test-uploads.timer
sudo systemctl list-timers utcoj-test-uploads.timer
```

Timer gọi recovery trước cleanup. Recovery hoàn tất journal PREPARED/APPLYING,
đối soát preparation bị chết; cleanup có khóa chống chạy chồng và bỏ qua tusd/
validator đang giữ file. Nếu recovery thất bại, systemd không tiếp tục cleanup
trong lượt đó; operator kiểm tra lỗi rồi chạy lại.

```bash
sudo journalctl -u utcoj-test-uploads.service -n 100 --no-pager
sudo docker compose -f compose.yaml -f compose.override.yaml -f compose.uploads.yaml exec -T site python3 manage.py cleanup_test_data_uploads --dry-run
sudo docker compose -f compose.yaml -f compose.override.yaml -f compose.uploads.yaml exec -T site python3 manage.py recover_test_data_uploads
sudo docker compose -f compose.yaml -f compose.override.yaml -f compose.uploads.yaml exec -T site python3 manage.py cleanup_test_data_uploads
sudo du -sh /opt/utcoj/data/test-uploads /opt/utcoj/data/problems
df -h /opt/utcoj/data/test-uploads /opt/utcoj/data/problems
```

Staging của upload kết thúc chỉ dọn sau grace period một giờ; file UUID mồ côi
không có DB row chỉ dọn sau 24 giờ. `.lock`/`.stop` tusd còn sót sau crash khiến
cleanup bỏ qua file: kiểm tra request và process đã dừng trước khi xử lý; không
xóa lock của validator đang hoạt động. Recovery có thể retry hoàn tất revision
sau lỗi DB; command không có chế độ dry-run và không phải thao tác rollback.

Revision dưới `/problems/<code>/_revisions/` và file trước revision đầu **được giữ
lại**, cleanup staging không xóa chúng. Phiên bản này chưa tự garbage-collect
revision chính thức: theo dõi đĩa và chỉ dọn khi chứng minh không judge/lượt chấm
nào còn tham chiếu. Mức dự phòng 5 GiB vẫn tính cả các upload đã đặt chỗ; giữ nhiều
revision lâu sẽ tăng dung lượng thực dùng.

## 9. Dừng nhận upload và rollback

Khi có sự cố, đổi `DMOJ_TEST_UPLOAD_ACCEPT_NEW = False`, giữ
`DMOJ_TEST_UPLOAD_ENABLED = True`, rồi restart site/Celery. Phiên đang hoạt động
có thể hoàn tất/resume và các guard vẫn bảo vệ bộ test. Đợi publication ổn định
hoặc chạy recovery; không kill process đang công bố file.

Trước khi tắt toàn bộ flag hoặc quay về code cũ, kiểm tra không còn phiên hoạt
động hoặc journal chưa hoàn tất:

```bash
sudo docker compose -f compose.yaml -f compose.override.yaml -f compose.uploads.yaml exec -T site python3 manage.py shell -c 'from judge.models import ProblemDataEditSession, ProblemDataRevision; print("live sessions:", ProblemDataEditSession.objects.filter(status__in=("ACTIVE", "APPLYING")).count()); print("unfinished publications:", ProblemDataRevision.objects.filter(status__in=("PREPARED", "APPLYING")).count())'
```

Kết thúc/thu hồi phiên bằng giao diện khi an toàn, đối soát rồi mới đổi
`DMOJ_TEST_UPLOAD_ENABLED = False`. Có thể stop tusd sau khi không còn request:

```bash
sudo docker compose -f compose.yaml -f compose.override.yaml -f compose.uploads.yaml stop tusd
```

Nếu dừng tusd lâu, route `/uploads/` cần trả 503 hoặc phục hồi cấu hình Nginx cũ;
Nginx syntax test/restart có thể thất bại nếu vẫn phân giải upstream không còn
container. Giữ schema, dữ liệu staging, secret và revision đã công bố; không
`down -v`, không migrate ngược, không xóa `_revisions` để rollback.

Không cho luồng form cũ ghi lại bài đã có revision. Guard model hiện từ chối
đường ghi ngoài editor cho bài versioned ngay cả khi không có khóa; tắt flag hoặc
rollback code làm mất guard đó. Vì vậy rollback frontend/backend cũ chỉ an toàn
sau đối soát đầy đủ và trong cửa sổ không có người chỉnh dữ liệu test. Bộ test
đã áp dụng hợp lệ vẫn chấm được vì `init.yml` trỏ vào file revision còn giữ.

Trong phiên bản đầu, đổi mã bài/storage hoặc chuyển sang quản lý test thủ công
của bài đã có revision cần quy trình chuyển dữ liệu riêng và bị guard từ chối.
Soft-delete/archive được phép khi không còn phiên sửa hoặc công bố; xóa vĩnh
viễn bị ràng buộc bởi journal giữ lại. Không dùng SQL/bulk update để vượt guard.

Nếu revision có dữ liệu sai về nội dung, áp dụng bộ test đúng qua phiên mới để
có journal mới; recovery chỉ hoàn tất journal, không tự chọn revision cũ.
Trường hợp mất kho test/DB cần phục hồi từ backup đã diễn tập và đối soát cả hai,
không chỉ copy một `init.yml` làm database lệch với file đang chấm.

## 10. Trạng thái kiểm chứng

- Compose v5.5.1: kiểm tra schema và merge với base/override tương đương thông
  tin server; xác nhận giữ mounts/email, không mount staging vào bridge và không
  publish cổng tusd.
- Các flags/hooks tusd đối chiếu source v2.8.0; frontend pin `tus-js-client@4.3.1`.
- 238 test Django đã chạy thành công với Sync API bật như CI, bao gồm tusd thật
  nhận ZIP 306 MiB theo request 16 MiB, đối soát offset, từ chối request lặp và
  kiểm tra ZIP. Suite riêng upload hiện có 37 ca, gồm route qua middleware, lỗi
  staging không kết thúc phiên sửa và gỡ bộ test không cần ZIP thay thế, kể cả
  phục hồi sau khi gỡ `init.yml`; test dùng database riêng và staging tạm.
  Ca Nginx + tusd dùng snippet triển khai thật, kiểm tra Location giữ đúng cổng
  và upload ZIP 306 MiB ngay từ lần tạo đầu tiên. Có thể chạy lại bằng:

  ```bash
  TUSD_BINARY=/path/to/tusd NGINX_BINARY=/path/to/nginx TEST_UPLOAD_MIB=306 .venv/bin/python manage.py test judge.tests.test_test_data_upload --noinput
  ```
- Fixture Chrome và test fingerprint xác nhận thanh tiến trình, resume sau reload/
  mất response, từ chối file khác cùng tên/kích thước, hủy/thu hồi phiên và giữ
  nội dung form. Fixture này dùng protocol/API kiểm soát, chưa thay thế phép thử
  qua Cloudflare production.
- Đã chạy syntax test và integration qua Nginx Ubuntu 1.28.3 được giải nén riêng
  trong `/tmp`, với mock upstream localhost: mọi method qua auth; cookie/token,
  method/URI/Content-Length gốc được giữ; secret client bị ghi đè; Authorization
  bị bỏ; internal URL bị chặn. Không cài Nginx vào hệ thống. Binary trong
  `nginx:alpine` production và cấu hình/cert đầy đủ vẫn phải chạy `nginx -t`
  trước rollout.
- Qua Cloudflare thật, quyền filesystem trên production và hành vi image judge
  thực tế là tiêu chí nghiệm thu còn phải chạy trên server. Các lệnh deployment
  trong tài liệu chưa được thực thi trên production.
