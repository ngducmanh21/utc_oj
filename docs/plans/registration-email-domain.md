# Implementation plan: Giới hạn email đăng ký UTCOJ

Trạng thái: Đã duyệt và triển khai trong workspace. Hướng dẫn triển khai server:
[`docs/deployment/registration-email-domain.md`](../deployment/registration-email-domain.md).

## 1. Mục tiêu và phạm vi

Chỉ cho phép đăng ký tài khoản mới bằng email có domain chính xác là
`lms.utc.edu.vn`. Kiểm tra trên frontend để người dùng nhận phản hồi sớm và
kiểm tra bắt buộc trên backend để POST trực tiếp không thể bỏ qua quy tắc.

Phạm vi đề xuất gồm đăng ký bằng form và tạo tài khoản mới qua OAuth. Tài khoản
đã tồn tại vẫn đăng nhập được, kể cả khi email thuộc domain khác. Không cập nhật
hàng loạt dữ liệu cũ, không áp dụng giới hạn này cho reset mật khẩu, sửa hồ sơ,
Django admin hoặc lệnh quản trị `adduser` trong thay đổi này.

Giới hạn domain chỉ kiểm tra địa chỉ đăng ký; quyền sở hữu email vẫn được xác
nhận bằng luồng activation hiện có. Nếu production tắt activation thì giới hạn
domain không tự chứng minh người đăng ký sở hữu email đó.

## 2. Hiện trạng đã kiểm tra

- `dmoj/urls.py`: GET/POST `/accounts/register/` dùng `RegistrationView`.
- `judge/views/register.py`: `CustomRegistrationForm.clean_email()` kiểm tra
  email trùng và nhà cung cấp bị cấm, chưa giới hạn domain trường.
- `templates/registration/registration_form.html`: còn gợi ý dùng Gmail.
- `templates/registration/oauth.html`: trang đăng ký hiện ghi “Sign up with Gmail”.
- `dmoj/settings.py` và `judge/social_auth.py`: OAuth có pipeline tạo User riêng,
  không đi qua `CustomRegistrationForm`.
- Chưa tìm thấy API REST riêng cho đăng ký trong các route hiện tại. Endpoint
  POST của form chính là backend cần bảo vệ, kể cả khi gọi bằng curl hoặc script.
  Không thêm API đăng ký mới chỉ để thực hiện yêu cầu này.

## 3. Quy tắc validation

1. Dùng kiểm tra cú pháp email của Django trước khi kiểm tra domain.
2. Bỏ khoảng trắng đầu/cuối theo hành vi EmailField hiện có.
3. So sánh domain không phân biệt chữ hoa/thường; giữ nguyên phần trước `@`.
4. Chỉ nhận domain `lms.utc.edu.vn` chính xác. Không dùng kiểm tra suffix lỏng.
5. Không nhận subdomain, domain có hậu tố khác hoặc nhiều địa chỉ trong một ô.
6. Giữ kiểm tra email trùng và các validation đăng ký hiện có. Không thay đổi
   chính sách trùng email toàn hệ thống trong công việc này.
7. Thông báo: “Chỉ chấp nhận email có đuôi @lms.utc.edu.vn.”; có bản dịch Anh
   tương ứng theo cơ chế i18n của dự án.

Ví dụ:

| Địa chỉ | Kết quả |
| --- | --- |
| `student@lms.utc.edu.vn` | Hợp lệ |
| `student@LMS.UTC.EDU.VN` | Hợp lệ |
| ` student@lms.utc.edu.vn ` | Hợp lệ sau khi bỏ khoảng trắng ngoài |
| `student@gmail.com` | Từ chối |
| `student@utc.edu.vn` | Từ chối |
| `student@sub.lms.utc.edu.vn` | Từ chối |
| `student@lms.utc.edu.vn.evil.com` | Từ chối |
| `student@fakelms.utc.edu.vn` | Từ chối |
| `student@lms.utc.edu.vn.` | Từ chối |
| Email rỗng hoặc sai cú pháp | Từ chối |

## 4. Decompose công việc

### A. Validator dùng chung và backend form

- Tạo `judge/utils/registration.py` chứa domain cho phép và validator dùng chung.
  Quy tắc cố định theo yêu cầu; không thêm tùy chọn tắt hoặc quota.
- Gọi validator trong `CustomRegistrationForm.clean_email()` trước kiểm tra
  email trùng. Lỗi gắn vào trường `email`.
- Truyền domain/thông báo cần thiết sang frontend từ cùng nguồn backend.
- Giữ response contract hiện có của endpoint: form không hợp lệ được render với
  lỗi trường email; không tự đổi endpoint HTML thành API JSON hay đổi status code.
- Đảm bảo email bị từ chối không tạo User, Profile, RegistrationProfile hoặc gửi
  email kích hoạt.

### B. Frontend đăng ký

- Thay hướng dẫn Gmail bằng “Sử dụng email @lms.utc.edu.vn”.
- Thêm placeholder ví dụ, giữ input `type=email` và thông tin hỗ trợ truy cập.
- Dùng Constraint Validation API để kiểm tra domain khi nhập và trước submit;
  hiển thị lỗi tiếng Việt theo ngôn ngữ đang chọn, xóa lỗi khi sửa thành hợp lệ.
- Đồng nhất trim và so sánh domain không phân biệt hoa/thường với backend.
- Tránh regex HTML pattern phân biệt hoa/thường làm lệch quy tắc backend.
- Khi JavaScript bị tắt, backend vẫn kiểm tra đầy đủ và hiển thị lỗi.
- Giữ nguyên các trường khác, CAPTCHA, CSRF và kiểm tra mật khẩu.

### C. Chặn tạo tài khoản mới qua OAuth

- Thêm bước dùng validator chung vào pipeline sau khi xác định tài khoản hiện có
  và liên kết email, nhưng trước form chọn username và `create_user`.
- Từ chối tài khoản mới có email ngoài domain hoặc thiếu email, hiển thị thông
  báo dễ hiểu qua cơ chế lỗi social auth hiện có.
- Giữ đăng nhập tài khoản đã liên kết và hành vi liên kết tài khoản hiện có;
  không thay đổi chính sách xác minh/liên kết OAuth trong phạm vi này.
- Kiểm tra luồng partial/resume để không bỏ qua validator khi tiếp tục đăng ký.
- Thay “Sign up with Gmail” bằng nhãn phù hợp và thêm ghi chú giới hạn domain
  trên trang đăng ký; không đổi lời hướng dẫn đăng nhập của tài khoản cũ.

### D. Bản dịch

- Cập nhật `locale/vi/LC_MESSAGES/django.po` và `djangojs.po` nếu dùng gettext JS.
- Biên dịch và kiểm tra thông báo domain, lỗi form và lỗi OAuth bằng tiếng Việt.
- Không đưa khóa domain riêng biệt vào bản dịch để tránh dịch sai quy tắc.

### E. Kiểm thử

- Unit test validator theo bảng địa chỉ ở trên.
- Test form với email đúng, sai domain, sai cú pháp và email đã tồn tại.
- Test POST trực tiếp `/accounts/register/` bỏ qua JavaScript: email sai phải
  bị từ chối, số User/Profile/RegistrationProfile không tăng, không gửi mail.
- Test email hợp lệ vẫn đi qua đăng ký/kích hoạt hiện có với dữ liệu form hợp lệ.
- Test OAuth: tài khoản mới sai domain bị chặn trước tạo User, domain đúng được
  đi tiếp, tài khoản cũ ngoài domain vẫn đăng nhập được; kiểm tra partial/resume.
- Kiểm tra giao diện thực tế: nhập sai, sửa lại, Enter để submit, email chữ hoa,
  khoảng trắng ngoài, lỗi backend và JavaScript tắt.
- Kiểm tra hồi quy đăng nhập/reset mật khẩu tài khoản cũ ngoài domain.
- Chạy lint các file sửa và Django system check; không tạo migration cho thay đổi này.

### F. Review và triển khai

- Review diff theo frontend, validator, form, OAuth và kiểm thử.
- Triển khai code, chạy `compilemessages -l vi`, `compilejsi18n -l vi` nếu cần,
  rồi `collectstatic --noinput` và restart service ứng dụng liên quan.
- Smoke test trên production bằng email sai domain, xác nhận bị từ chối; kiểm
  tra luồng đăng ký hợp lệ bằng tài khoản test được cho phép.
- Rollback bằng hoàn nguyên commit và triển khai lại; không có migration dữ liệu.

## 5. Thứ tự và tiêu chí nghiệm thu

Thứ tự: A → B → C → D → E → review → F. Kiểm thử validator/backend được bổ sung
cùng các phần triển khai tương ứng, sau đó kiểm tra toàn luồng.

- Form và POST trực tiếp chỉ nhận domain chính xác `lms.utc.edu.vn`.
- Tạo tài khoản OAuth mới không thể bỏ qua giới hạn này.
- Không có tài khoản hoặc email kích hoạt phát sinh cho yêu cầu bị từ chối.
- Hướng dẫn và lỗi frontend/backend nhất quán, có tiếng Việt.
- Tài khoản cũ vẫn đăng nhập và reset mật khẩu được.
- Không cần migration và không thay đổi dữ liệu tài khoản hiện có.

## 6. File dự kiến thay đổi

- Mới: `judge/utils/registration.py`, `judge/tests/test_registration.py`.
- Sửa: `judge/views/register.py`, `judge/social_auth.py`, `dmoj/settings.py`.
- Sửa: `templates/registration/registration_form.html`, `templates/registration/oauth.html`.
- Sửa: catalog dịch Việt; thêm file JS hoặc test JS riêng nếu cần theo cách tổ
  chức kiểm thử hiện tại.

Kế hoạch được duyệt bao gồm OAuth mới để đáp ứng quy tắc cho mọi luồng tự đăng ký.
Validator được kiểm tra lại trong bước partial chọn username/mật khẩu khi resume.
Frontend lấy domain và thông báo đã dịch qua thuộc tính HTML từ backend; không
cần thêm catalog JavaScript hay chạy `compilejsi18n` cho thay đổi này.

Kiểm chứng trong workspace: 18 kiểm thử backend và 4 kiểm thử JavaScript đều
qua; lint và Django system check qua. Trình duyệt thực đã xác nhận hướng dẫn/lỗi
tiếng Việt, chặn Gmail/domain giả dạng, nhận domain chữ hoa và bỏ khoảng trắng
ngoài. Database test riêng đã được test runner dọn sau khi chạy.
