# Kiểm chứng revision trên bốn judge

## Bằng chứng source và giới hạn kết luận

Đã đọc source chính thức VNOI tại commit
`a396506f7a3406cc989b96657f0816fea3aac7d0` (commit ngày 30/09/2026). Chưa biết
digest/source của image `vnoj/judge-tiervnoj:amd64-latest` trên production.
Source tham khảo không thay thế phép thử trên container thực tế.

- `JudgeWorker._grade_cases()` tạo `Problem` mới cho mỗi submission, truyền cả
  namespace storage. Updater gọi scan với `force_update=True`; nó cập nhật danh
  sách hỗ trợ, không thay `Problem` của lượt đang chấm.
  [judge.py](https://github.com/VNOI-Admin/judge-server/blob/a396506f7a3406cc989b96657f0816fea3aac7d0/dmoj/judge.py#L463)
- `Problem` đọc `init.yml` qua `ProblemConfig`, mở ZIP được cấu hình một lần và
  giữ `ZipFile` trong `ProblemDataManager`. File test trong archive được đọc từ
  handle đó. Checker module cache theo instance; generator/checker phụ đọc source
  theo đường dẫn được cấu hình.
  [problem.py](https://github.com/VNOI-Admin/judge-server/blob/a396506f7a3406cc989b96657f0816fea3aac7d0/dmoj/problem.py#L63)
- Monitor dùng watchdog đệ quy, nhận các event moved/deleted/modified/created rồi
  gọi callback cập nhật. Khi không có watchdog hoặc chạy `--no-watchdog`, monitor
  không hoạt động.
  [monitor.py](https://github.com/VNOI-Admin/judge-server/blob/a396506f7a3406cc989b96657f0816fea3aac7d0/dmoj/monitor.py)
- Judge v2 đăng ký storage `id/glob`; cache đường dẫn chỉ giữ thư mục bài.
  `get_supported_problems_and_mtimes()` trả danh sách rỗng ở v2 vì giao thức này
  không cần enumerate từng bài. Không dùng số lượng supported problems/mtime
  làm bằng chứng test mới đã được đọc.
  [judgeenv.py](https://github.com/VNOI-Admin/judge-server/blob/a396506f7a3406cc989b96657f0816fea3aac7d0/dmoj/judgeenv.py#L314)

**Suy luận thiết kế:** nếu image đang chạy giữ các hành vi trên, một lượt đã tạo
`Problem` dùng config và archive cũ; lượt tạo sau khi thay nguyên tử `init.yml`
dùng config mới và archive có tên mới. Vì ZIP/checker/generator của từng revision
bất biến và bản cũ không bị xóa, lượt đang chạy không cần đọc file đang bị ghi đè.
Thử cả checker/generator lazy-load để kiểm chứng toàn bộ tham chiếu, không chỉ ZIP.

Không giả định mọi submission đã vào queue trước lúc Lưu dùng revision cũ.
Revision được xác định lúc judge tạo `Problem`, sau khi lấy submission từ queue.
Submission ở ranh giới có thể dùng trọn bản cũ hoặc trọn bản mới; không được trộn.

## 1. Ghi nhận image và source đang chạy

Trên server, không in `.Config.Env`, key trong `judge*.yml` hoặc toàn bộ inspect:

```bash
for container in utcoj-judge-1 utcoj-judge-2 utcoj-judge-3 utcoj-judge-4
do
  sudo docker inspect "$container" --format 'Name={{.Name}} Image={{.Image}}'
done
sudo docker image inspect vnoj/judge-tiervnoj:amd64-latest --format 'Id={{.Id}} Digests={{json .RepoDigests}}'
sudo docker exec utcoj-judge-1 git -C /judge rev-parse HEAD
```

Nếu image không chứa `.git`, lấy SHA-256 các file đang import. Chạy cho **cả bốn**
container, thay tên container trong lệnh:

```bash
sudo docker exec -i utcoj-judge-1 python3 - <<'PY'
import hashlib
import importlib.util
from pathlib import Path

root = Path(importlib.util.find_spec('dmoj').origin).parent
print('dmoj source directory:', root)
for relative in ('judge.py', 'problem.py', 'judgeenv.py', 'monitor.py', 'control.py', 'utils/helper_files.py'):
    path = root / relative
    print(relative, hashlib.sha256(path.read_bytes()).hexdigest())
PY
```

Hash tham khảo của commit đã đọc:

| File | SHA-256 |
| --- | --- |
| `judge.py` | `ca605c8d1550a154ac56cab292096a57ff554a76c6ecbbc7d8f174a828091645` |
| `problem.py` | `6cdcb3d649fba59816eeb4470a916cf419ffebde095dfa5c25dd345d04dba2fd` |
| `judgeenv.py` | `2f1bb2fc2346e18ebb39491b7120d1ff2cbb53607f964dff5005b09363b0b751` |
| `monitor.py` | `be972375e82ec8bddf65a89c4d6473fd65e869322bb3df27af3df95b7cf253f3` |
| `control.py` | `d9baabb1f5722c278a230a1bb0dd9560e978979e8d1baa86db2b78d1b6d8d22b` |
| `utils/helper_files.py` | `ab9a4d37efa3011f0420221002061c52d5e579216c0fa64e0f6419b5cf528fa0` |

Hash khác không tự động là lỗi; cần đọc source thực tế và đối chiếu các đường
trên. Có thể dùng `docker cp` lấy riêng file Python từ đường dẫn source vừa in,
không copy thư mục `/problems` hoặc file có key. Lưu digest, hash và kết quả thử
vào hồ sơ release. Không nâng image judge chỉ để ép hash khớp.

## 2. Bài thử có kết quả phân biệt

Tạo bài **riêng tư** mã `uploadprobe`, storage `local`, PY3 được phép, checker
standard, hai testcase không batch, mỗi case 50 điểm, time limit 15 giây/case,
short-circuit tắt. Không sử dụng bài đang thi hoặc dữ liệu thật.

Tạo hai ZIP nhỏ trong thư mục làm việc local rồi upload qua UI (có thể dùng chúng
cho smoke test trước file 306 MB):

```bash
python3 - <<'PY'
from zipfile import ZipFile, ZIP_DEFLATED

for version, answer in (('v1', 'OLD'), ('v2', 'NEW')):
    with ZipFile('uploadprobe-' + version + '.zip', 'w', compression=ZIP_DEFLATED) as archive:
        for case in (1, 2):
            archive.writestr(str(case) + '.in', str(case) + '\n')
            archive.writestr(str(case) + '.out', answer + '\n')
PY
```

Áp dụng v1 bằng editor rồi gửi một submission PY3 để tạo template, source:

```python
import time
input()
time.sleep(10)
print('OLD')
```

Kết quả v1 phải AC cả hai case. V2 đổi expected output thành NEW, source trên
phải WA cả hai case; source `print('NEW')` phải AC sau khi áp dụng v2.

## 3. Gửi đúng một lượt thử tới từng judge

Lệnh sau **tạo bốn submission thật**, chỉ cho bài riêng tư `uploadprobe`. Nó dùng
user/language từ submission PY3 mẫu vừa tạo, source OLD cố định, kiểm tra các
judge online rồi yêu cầu tên judge cụ thể qua API hiện có của project. Nó không
disable hoặc restart judge, không sửa bộ test bằng SQL.

```bash
sudo docker exec -i utcoj-site-1 python3 manage.py shell <<'PY'
from judge.models import Judge, Submission, SubmissionSource

names = ['utcoj-hanoi-judge-' + str(number) for number in range(1, 5)]
assert set(Judge.objects.filter(name__in=names, online=True).values_list('name', flat=True)) == set(names), 'All four judges must be online.'
template = Submission.objects.filter(problem__code='uploadprobe', language__key='PY3').order_by('-id').first()
assert template and not template.problem.is_public, 'Create the private uploadprobe problem and a PY3 submission first.'
assert template.problem.effective_storage == 'local'
source = "import time\ninput()\ntime.sleep(10)\nprint('OLD')\n"
for name in names:
    submission = Submission.objects.create(user_id=template.user_id, problem_id=template.problem_id, language_id=template.language_id)
    SubmissionSource.objects.create(submission=submission, source=source)
    submission.judge(judge_id=name)
    print(name, submission.pk)
PY
```

Quan sát kết quả và judge đã nhận từng lượt:

```bash
sudo docker exec utcoj-site-1 python3 manage.py shell -c 'from judge.models import Submission; print(list(Submission.objects.filter(problem__code="uploadprobe").order_by("-id").values("id", "status", "result", "judged_on__name", "current_testcase", "case_points", "case_total")[:16]))'
```

Lượt chờ judge bận có thể chưa bắt đầu; không tính nó là bằng chứng đọc v1/v2.
Xem testcase detail trên website để xác nhận cả hai case và số điểm, không chỉ
verdict tổng. Không lặp vô hạn để làm đầy queue.

## 4. Thử đổi test giữa lúc đang chấm

1. Với v1 đang active, bắt đầu phiên edit và upload v2 đến **READY**, ghép đủ hai
   testcase nhưng chưa bấm Lưu.
2. Gửi bốn lượt OLD bằng lệnh phần 3. Đợi cả bốn đã ở trạng thái G; xem log hoặc
   current testcase để biết `Problem` đã được tạo trước khi công bố.
3. Trong khoảng source đang sleep, bấm Lưu v2. Ghi thời điểm áp dụng/revision ID.
4. Bốn lượt đã bắt đầu với v1 phải AC cả hai case. Không được case đầu OLD/case
   sau NEW, mất file hoặc IE do ZIP/config.
5. Gửi lại bốn lượt OLD sau Lưu: mỗi judge phải WA cả hai case trên v2. Gửi source
   NEW để xác nhận AC. Kiểm tra `judged_on__name` đủ bốn tên.
6. Thử thay custom checker/generator trên bài thử riêng, gồm đường chỉ sửa
   checker không có ZIP mới; lượt đã chạy vẫn dùng file revision cũ, lượt mới
   dùng file mới.
7. Lặp một lượt mới sau khi queue hết để loại trừ khác biệt do submission đã
   đang được khởi tạo ở ranh giới. Không restart judge giữa phép thử vì restart
   sẽ che lỗi cache/watch.

Khi bốn judge không cùng rảnh, chạy lần lượt; bảo đảm từng lần có một lượt đang
chấm và một lần công bố có thể phân biệt. Không ép dừng bài thật để lấy slot.

Ghi vào checklist: thời điểm tạo `Problem`/trạng thái G, revision trước/sau,
submission ID, judge name, hai testcase verdict và lỗi nếu có. Không ghi test
content, token, cookie hoặc judge key vào log release.

## 5. Điều kiện bật production và trường hợp thất bại

Chỉ bật rộng khi source thực tế phù hợp, cả bốn judge dùng v2 mới ở lượt sau,
lượt đang chạy nhất quán và cả checker/generator đã được thử nếu sử dụng. Giữ
toàn bộ file cũ đến khi có chính sách retention đã kiểm chứng; staging cleanup
không dọn revision.

Nếu image giữ config/test data qua nhiều lượt hoặc không xử lý nguyên tử như dự
kiến, giữ feature tắt/dừng nhận phiên mới, lưu bằng chứng và bổ sung cơ chế drain
cho bài đang cập nhật trước rollout. Restart judge không thay thế cơ chế drain:
có thể cắt ngang lượt thật. Không tự đổi image hoặc mở rộng cổng API judge.

Nếu cần kiểm tra watcher/update API, source tham khảo có POST
`/update/problems` trên các port host **12345–12348**; chỉ gọi từ localhost sau
khi xác nhận source image. Nó refresh danh sách, không bảo đảm đổi dữ liệu của
lượt đang chấm. Không cần gọi API này trong luồng upload bình thường.
[control.py](https://github.com/VNOI-Admin/judge-server/blob/a396506f7a3406cc989b96657f0816fea3aac7d0/dmoj/control.py)
