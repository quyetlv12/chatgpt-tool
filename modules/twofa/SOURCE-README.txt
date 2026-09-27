============================================
  Shoptaikhoan Tool - Change 2FA
  Source Edition v1.0.0
============================================

Yêu cầu: Python 3.11 trở lên.

WINDOWS
-------
1. Giải nén ZIP vào một thư mục bất kỳ.
2. Nhấp đúp  start-windows.bat
3. Chờ cài đặt dependencies (lần đầu mất 1-2 phút).
4. Trình duyệt tự mở http://127.0.0.1:5033
5. Nhấn Ctrl+C trong cửa sổ terminal để dừng server.

LINUX / macOS
-------------
1. Giải nén ZIP.
2. Mở terminal, cd vào thư mục vừa giải nén.
3. chmod +x start-unix.sh && ./start-unix.sh
4. Mở http://127.0.0.1:5033 trong trình duyệt.
5. Ctrl+C để dừng.

CẤU TRÚC DỮ LIỆU
------------------
Database SQLite được tạo tự động tại:
  Windows : %LOCALAPPDATA%\InfinityAIStore\Change2FA\twofa.db
  macOS   : ~/Library/Application Support/InfinityAIStore/Change2FA/twofa.db
  Linux   : ~/.local/share/InfinityAIStore/Change2FA/twofa.db

Xóa file twofa.db để reset toàn bộ dữ liệu.

SỬ DỤNG
--------
- Paste combo theo định dạng EMAIL|PASSWORD|TOTP_SECRET (mỗi dòng một combo).
- Chọn chế độ "Chỉ kiểm tra" hoặc "Đổi 2FA".
- Kết quả hiển thị trực tiếp trên giao diện web.
- Đổi mật khẩu: mở Settings, lưu "Mật khẩu mới dùng chung" (tối thiểu
  12 ký tự), sau đó bấm "Đổi mật khẩu" để dùng workspace/queue riêng.
- Password output chỉ xuất hiện sau khi tool đăng nhập lại thành công bằng
  mật khẩu mới; input vẫn là EMAIL|PASSWORD_HIỆN_TẠI|TOTP_SECRET.

GỬI LOG LỖI
------------
Nếu gặp lỗi, copy toàn bộ nội dung terminal và gửi lại.
Không gửi file twofa.db vì có thể chứa thông tin tài khoản.

============================================
