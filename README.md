# Shoptaikhoan Suite

Một launcher và giao diện trung tâm cho ba module: 2FA/Password, Codex Export và Browser Login/9Router.

## Chạy trên macOS

Double-click `start.command`, hoặc chạy:

```bash
./start.sh
```

Giao diện trung tâm mở tại `http://127.0.0.1:5050`.

## Cổng

- Suite: `5050`
- Ba engine dùng Unix socket nội bộ, không mở thêm TCP port.
- OAuth callback tạm thời dùng `1455` khi chạy Codex OAuth (bắt buộc theo redirect URI đã đăng ký).

Không chạy luồng OAuth của bản gốc và Suite cùng lúc vì cả hai phải dùng callback `1455`.

## Build ứng dụng macOS

```bash
./build-macos.sh
```

Kết quả: `dist/Shoptaikhoan Suite.app`.

## Dữ liệu

Dữ liệu mới nằm trong `runtime/` và không ghi vào ba thư mục project gốc. Source module là bản sao độc lập trong `modules/`.
