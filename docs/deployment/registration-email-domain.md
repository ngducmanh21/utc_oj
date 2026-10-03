# Triển khai giới hạn email đăng ký

Tài khoản mới đăng ký bằng form hoặc OAuth phải dùng email `@lms.utc.edu.vn`.
Domain được so sánh chính xác, không phân biệt hoa/thường. Tài khoản cũ vẫn đăng
nhập, reset mật khẩu và liên kết OAuth như trước. Không cần migration database.

## Local

Sau khi cập nhật code, dùng virtualenv hiện có:

```bash
python manage.py compilemessages -l vi
python manage.py runserver 127.0.0.1:8000
```

Mở `/accounts/register/`, tải lại trang nếu đang mở form cũ. Static file mới là
`registration-email.js`. Django runserver phục vụ file này khi DEBUG bật.

Frontend nhận domain và thông báo đã dịch từ backend qua thuộc tính HTML, nên
không cần thay đổi catalog `djangojs` hoặc chạy `compilejsi18n` cho tính năng này.

## Server Docker hiện tại

Cập nhật code bằng quy trình Git hiện có, giữ các chỉnh sửa template và
`dmoj/local_settings.py` riêng của server. Trong `/opt/utcoj/deploy`, dùng hàm
`dc` đã cấu hình cho các file Compose của UTCOJ:

```bash
dc run --rm --no-deps site python3 manage.py compilemessages -l vi
dc run --rm --no-deps site python3 manage.py collectstatic --noinput
dc restart site
dc exec -T site python3 manage.py check
dc exec -T site python3 manage.py shell -c \
'from judge.utils.registration import REGISTRATION_EMAIL_DOMAIN
from django.conf import settings
print("Registration domain:", REGISTRATION_EMAIL_DOMAIN)
print("OAuth domain check:", "judge.social_auth.verify_registration_email" in settings.SOCIAL_AUTH_PIPELINE)
print("OAuth partial check:", "judge.social_auth.get_username_password" in settings.SOCIAL_AUTH_PIPELINE)'
```

Domain cần in ra `lms.utc.edu.vn`; hai kiểm tra pipeline cần là `True`. Nếu
`local_settings.py` ghi đè `SOCIAL_AUTH_PIPELINE`, đồng bộ thứ tự với pipeline
trong `dmoj/settings.py`: validator sau `associate_by_email`, trước bước partial
`get_username_password` và trước `create_user`.

Kiểm tra production:

- Form hiển thị “Sử dụng email @lms.utc.edu.vn.” và lỗi tiếng Việt khi nhập Gmail.
- Email giả dạng `student@lms.utc.edu.vn.evil.com` bị từ chối.
- POST trực tiếp email sai vẫn bị từ chối và không tạo tài khoản/gửi activation.
  Endpoint hiện có trả HTML form với lỗi trường email, không phải API JSON.
- Domain chữ hoa được chấp nhận; email hợp lệ tiếp tục qua activation hiện có.
- Tạo mới qua OAuth ngoài domain bị từ chối; tài khoản OAuth cũ vẫn đăng nhập được.
- Chỉ thực hiện đăng ký hợp lệ bằng tài khoản test được cho phép; kiểm thử tự động
  dùng database test và backend email locmem, không gửi mail thật.

Nếu cần rollback, hoàn nguyên commit của tính năng rồi biên dịch bản dịch,
collect static và restart site như trên. Không có migration cần hoàn nguyên.

## Kiểm thử cho thay đổi tiếp theo

```bash
python manage.py compilemessages -l vi
python manage.py test judge.tests.test_registration
npm run test:registration
```

Django test runner cần quyền tạo database test trên database local/CI. Không chạy
bộ kiểm thử trên database production. CI biên dịch bản dịch Việt trước khi chạy
backend tests và chạy kiểm thử JS trong job styles.
