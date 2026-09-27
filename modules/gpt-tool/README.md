# GPT-Tool

Công cụ Python: đăng nhập ChatGPT bằng `email|password|totp`, lấy Codex OAuth **không Veriphone / không SMS**, rồi xuất **một file JSON mỗi tài khoản**. Cũng convert qua lại các format của [GPTSession2CPAandSub2API](https://github.com/gtxx3600/GPTSession2CPAandSub2API).

Không chạy Rust. Không đăng ký tài khoản mới. Không thuê SIM.

## 1-click (không cần biết code)

Cài [Python 3.11+](https://www.python.org/downloads/) trước (Windows: tick “Add Python to PATH”).

| Máy | Làm gì |
| --- | --- |
| macOS | Double-click `start.command` (lần đầu: chuột phải → Open) |
| Windows | Double-click `start.bat` |
| Linux | `chmod +x start.sh && ./start.sh` |

Cửa sổ sẽ tự tạo `.venv`, cài thư viện, mở trình duyệt `http://127.0.0.1:8765/`.

1. Chọn format (CPA / Codex / sub2api / …).
2. Tab **Export Codex**: dán `email|pass|2fa`, bấm Chạy.
3. Tab **Convert JSON**: dán auth/session JSON có sẵn.
4. File nằm trong thư mục `out/`.

## CLI

```bash
python -m gpt_tool.cli export --format cpa --lines accounts.txt --out out
python -m gpt_tool.cli convert --format codex --in session.json --out out
```

Format: `cpa` `sub2api` `cockpit` `9router` `codex` `axonhub` `codexmanager`.

## Nếu báo add-phone

OpenAI vẫn có thể bắt bind phone lúc OAuth Codex dù web đã ver phone. Tool **cố ý không** thuê SIM. Bỏ qua tài khoản đó; lỗi ghi vào `out/failed.txt`.

## Bảo mật

File JSON chứa `access_token` / `refresh_token` = mật khẩu. Không gửi cho người khác, không commit vào git (`out/` đã gitignore).

## Dev

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest
```

## Desktop builds

### macOS

```bash
PYTHON_BIN=.venv/bin/python bash scripts/build_macos.sh
```

Kết quả: `dist/GPT-Tool.app` và `dist/GPT-Tool-macOS.zip`.

### Windows

Chạy trong PowerShell trên Windows:

```powershell
./scripts/build_windows.ps1
```

Kết quả: `dist/GPT-Tool.exe`. Đây là một file duy nhất; có thể chép sang máy Windows khác và chạy trực tiếp, không cần cài Python hoặc giữ thư mục DLL bên cạnh.

Nếu thông báo lỗi nhắc tới `_internal\\python311.dll`, đó là bản PyInstaller cũ. Xóa file/thư mục cũ và dùng `GPT-Tool.exe` mới.

GitHub Actions `Build desktop apps` tự build cả Windows và macOS khi push nhánh `develop`. Bản desktop lưu JSON tại `~/GPT-Tool/out` để không ghi dữ liệu vào bên trong app bundle.
