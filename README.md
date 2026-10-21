# Shoptaikhoan Suite

Một launcher và giao diện trung tâm cho bốn module độc lập: 2FA/Password, Codex Export, Browser Login/9Router và Chat Backup.

## Chạy trên macOS

Double-click `start.command`, hoặc chạy:

```bash
./start.sh
```

Giao diện trung tâm mở tại `http://127.0.0.1:5050`.

## Cổng

- Suite: `5050`
- Bốn engine dùng Unix socket nội bộ, không mở thêm TCP port.
- OAuth callback tạm thời dùng `1455` khi chạy Codex OAuth (bắt buộc theo redirect URI đã đăng ký).

Không chạy luồng OAuth của bản gốc và Suite cùng lúc vì cả hai phải dùng callback `1455`.

## Build ứng dụng macOS

```bash
./build-macos.sh
```

Kết quả: `dist/Shoptaikhoan Suite.app`.

## Dữ liệu

Dữ liệu mới nằm trong `runtime/` và không ghi vào ba thư mục project gốc. Source module là bản sao độc lập trong `modules/`.

Chat Backup dùng `runtime/chat-backup/` riêng. Thông tin `email|mật khẩu|2fa` chỉ được giữ trong bộ nhớ khi job đang chạy; SQLite chỉ lưu checkpoint bằng hash email, ID hội thoại và trạng thái sao chép.

Nhập hai tài khoản rồi bấm **Tải danh sách chat**. Sau khi đọc danh sách nguồn, tích chọn từng hội thoại hoặc dùng **Chọn tất cả / Bỏ chọn tất cả**, rồi bấm **Backup các chat đã chọn**. Mặc định chưa chọn chat nào; tool giữ phiên nguồn và chưa tạo link Share trong lúc chờ. Giới hạn số chat chỉ áp dụng trên các mục đã chọn (0 = tất cả mục đã chọn).

API nội bộ: `POST /api/jobs/scan` nhận cùng payload với `/api/jobs/start` nhưng dừng ở trạng thái `choosing`; `GET /api/conversations` trả `{run_id, items: [{id, title}]}`; `POST /api/jobs/selection` nhận `{run_id, conversation_ids}` để tiếp tục. Danh sách rỗng hoặc ID ngoài danh sách bị từ chối (400); phiên cũ hoặc job không còn chờ chọn trả 409. `/api/jobs/start` vẫn hỗ trợ luồng chạy trực tiếp trước đây.

Mỗi cuộc trò chuyện được Share, lưu ở tài khoản đích và cleanup xong trước khi xử lý cuộc tiếp theo. Tool nghỉ ngẫu nhiên 20–35 giây sau lần thành công, tăng dần khi lỗi và nghỉ 120–180 giây khi gặp HTTP 429; có thể dừng ngay trong thời gian chờ.

Luồng Share phù hợp với hai tài khoản ChatGPT cá nhân. Link từ workspace được quản lý có thể bị giới hạn trong chính workspace đó. Tool chỉ xóa link mới do nó tạo; link đã tồn tại trước khi chạy được giữ nguyên.
