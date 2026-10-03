# Chạy upload test trên máy local

Django `runserver` chỉ phục vụ trang và API phiên. Upload theo chunk cần tusd,
proxy kiểm tra quyền và Celery kiểm tra ZIP. Bộ chạy local dùng các cổng loopback:

| Dịch vụ | Cổng |
| --- | --- |
| Trang web qua Nginx | 8080 |
| Django đang chạy | 8000 |
| tusd | 1080 |
| Redis riêng cho Celery local | 6381 |

## Chuẩn bị

Cần `redis-server`, Nginx có module `auth_request`, tusd 2.8.0 và dependencies
Python trong `.venv`. Launcher dùng binary trong `PATH`, hoặc các tham số
`--nginx-binary` / `--tusd-binary`. Trên máy phát triển hiện tại, binary đã kiểm thử
trong `/tmp` được tự sao chép vào `.local-test-uploads/bin` cho các lần chạy sau.
Thư mục này được Git bỏ qua; không đưa staging hoặc secret vào repository.

Tạo thư mục staging và secret ổn định, không in secret:

```bash
.venv/bin/python - <<'PY'
import os
import secrets
from pathlib import Path
root = Path.cwd() / '.local-test-uploads'
root.mkdir(mode=0o700, exist_ok=True)
secret_file = root / 'internal-secret'
if not secret_file.exists():
    fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(secrets.token_hex(32))
PY
```

Thêm vào `dmoj/local_settings.py`, giữ các cấu hình database hiện có:

```python
DEBUG = True
DMOJ_TEST_UPLOAD_ENABLED = True
DMOJ_TEST_UPLOAD_MAX_SIZE = 1024 ** 3  # 1 GiB cho mỗi ZIP bộ test.
DMOJ_TEST_UPLOAD_ROOT = os.path.join(BASE_DIR, '.local-test-uploads')
with open(os.path.join(DMOJ_TEST_UPLOAD_ROOT, 'internal-secret')) as _upload_secret_file:
    DMOJ_TEST_UPLOAD_INTERNAL_SECRET = _upload_secret_file.read().strip()
CELERY_BROKER_URL = 'redis://127.0.0.1:6381/0'
DMOJ_TEST_UPLOAD_LOCAL_PROXY_URL = 'http://127.0.0.1:8080'
```

Kiểm tra migration và áp dụng các migration của tính năng nếu còn thiếu:

```bash
.venv/bin/python manage.py migrate --plan
.venv/bin/python manage.py migrate
```

## Chạy

Terminal thứ nhất:

```bash
.venv/bin/python manage.py runserver 127.0.0.1:8000
```

Terminal thứ hai:

```bash
.venv/bin/python scripts/run-test-upload-local.py
```

Mở **http://127.0.0.1:8080/** để sử dụng upload. Đi qua cổng 8080 để `/uploads/`
được chuyển tới tusd. Khi có `DMOJ_TEST_UPLOAD_LOCAL_PROXY_URL`, trang chỉnh sửa
test ở cổng 8000 tự chuyển tới proxy; điều này chỉ áp dụng khi `DEBUG=True`.
Nếu dùng cổng proxy khác, cập nhật URL local tương ứng.

Cookie đăng nhập dùng chung giữa hai cổng, nhưng `sessionStorage` của phiên sửa
phân biệt hai cổng. Trước khi chuyển một phiên đã mở ở 8000, kết thúc phiên tại tab
cũ. Với một phiên cũ đã mất token do lỗi, admin có thể bấm **End previous edit
session** để bỏ phiên đó và bắt đầu lại; thông báo phân biệt phiên của chính mình
với phiên của một người khác.

Launcher chỉ chạy khi `DEBUG=True`. Nó dùng Redis riêng, không dừng Django đang
chạy, không thay dữ liệu đã công bố. Ctrl+C trong terminal thứ hai dừng các dịch
vụ do launcher tạo; file staging và hàng đợi Redis vẫn còn để tiếp tục lần sau.
Logs nằm tại `.local-test-uploads/runtime/{nginx,redis,tusd,celery}.log`.
Cleanup/recovery khi phát triển có thể chạy thủ công:

```bash
.venv/bin/python manage.py recover_test_data_uploads
.venv/bin/python manage.py cleanup_test_data_uploads
```

## Khi upload lỗi

Nếu lần upload mới báo `Connection interrupted` ở 0 byte, kiểm tra header
`Location` của request `POST /uploads/` trong DevTools. URL phải giữ nguyên cổng
proxy, ví dụ `http://127.0.0.1:8080/uploads/<id>`. Bản cấu hình trước dùng `$host`
làm mất cổng; Retry lấy URL từ Django nên vẫn upload được. Snippet hiện dùng
`$http_host`; khởi động lại launcher để nhận snippet mới nếu dịch vụ đang chạy
từ bản cũ.

`File verification` là kiểm tra file tại trình duyệt. File chỉ đến server sau
khi API tạo upload thành công và tusd nhận chunk. Danh sách file trong ZIP được
đưa vào bảng testcase sau khi Celery kiểm tra ZIP xong.

Lỗi có thông báo rõ như thiếu staging, hết đĩa hoặc hết suất upload không thu hồi
phiên sửa đang hợp lệ. Bạn vẫn thêm/xóa/sửa testcase và chọn file khác được.
Bấm **Cancel ZIP upload** để bỏ ZIP chưa dùng và lưu chỉnh sửa với bộ test hiện tại;
bấm **Continue / Retry** để thử lại file đã chọn sau khi xử lý nguyên nhân lỗi.
Save chờ ZIP mới đạt `READY`, tránh lưu nhầm bộ test khi upload chưa hoàn tất.
Khi heartbeat không xác nhận được phiên hoặc phiên đã bị thu hồi, form mới khóa
cho tới khi xác nhận lại quyền sửa.

**Cancel ZIP upload** giữ phiên chỉnh sửa và chỉ hủy file. **End edit session**
kết thúc phiên, hủy các upload chưa áp dụng và nhả khóa bài. Lỗi HTTP từ tusd hoặc
proxy không tự động được xem là hết phiên; frontend xác nhận quyền qua API Django
trước khi bỏ token. Revoke chỉ xuất hiện khi cần thu hồi một phiên đang xung đột.

Để gỡ bộ test đang dùng mà không upload ZIP thay thế, bắt đầu phiên sửa rồi chọn
**Đặt lại** cạnh ZIP đã lưu hoặc đánh dấu xóa toàn bộ testcase, sau đó **Lưu**.
Khi không còn testcase và không có ZIP mới, lần lưu này gỡ liên kết ZIP, xóa các
testcase và gỡ `init.yml`; mở lại trang sẽ không tự điền test từ ZIP cũ. Revision
cũ vẫn được giữ cho các judge đang chấm và phục hồi theo cơ chế đã triển khai.
