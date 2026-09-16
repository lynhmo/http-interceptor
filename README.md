# Network Monitor

Công cụ debug HTTP/HTTPS kiểu **DevTools Network tab**, viết bằng Python + Tkinter.
Sinh ra cho trường hợp: **Tauri chỉ làm frontend/webview, mọi request đi thẳng tới backend Java** (không qua Rust `reqwest`) — nhưng dùng được cho bất kỳ frontend nào gọi HTTP API.

Tool đứng giữa, ghi lại toàn bộ request/response (method, URL, status, thời lượng, headers, body) rồi hiển thị theo thời gian thực trên GUI.

## Tính năng

- **2 chế độ bắt request**: `reverse` (khuyên dùng) và `forward` (mitmproxy).
- **Theo dõi nhiều backend cùng lúc** (mỗi backend 1 port, chung 1 GUI).
- GUI kiểu DevTools: bảng request + tab chi tiết Request/Response Headers/Body.
- Request đang bay hiển thị dòng xám `...` trước, có response mới cập nhật status/thời lượng.
- Request lỗi (status ≥ 400 hoặc `ERROR`) tô nền đỏ.
- **Lọc URL** theo thời gian thực, **Follow** tự cuộn theo request mới.
- **Copy as cURL** (chuột phải hoặc nút bấm) — paste ra terminal chạy lại y nguyên.
- Nút **Format JSON** cho tab body.
- **Dark mode**, nhớ lựa chọn lần sau.
- Chạy không cần terminal: double-click `.exe`/`.py` → hộp thoại nhập backend/port ngay trên GUI.
- **Nhớ cấu hình** lần cuối (`network_monitor_config.json`), lần sau mở lên chạy thẳng.
- **Đổi backend/port ngay khi app đang chạy** (nút `Đổi backend/port`), có kiểm tra port bị chiếm.
- Hỗ trợ backend HTTPS cert tự ký (`--insecure`).
- Chế độ `reverse` chỉ dùng thư viện chuẩn Python — không cần cài gì thêm.

## Yêu cầu

- Python **3.9+** (đã test trên 3.11), Tkinter đi kèm sẵn.
- Chế độ `reverse`: không cần cài thêm gì.
- Chế độ `forward`: cần `mitmproxy`:
  ```bash
  pip install -r requirements.txt
  ```

## Cài đặt

```bash
pip install -r requirements.txt   # chỉ bắt buộc nếu dùng --mode forward
```

## Cách dùng nhanh

### Cách 1: Double-click (không cần terminal)

1. Chạy `network_monitor.py` (hoặc `dist/NetworkMonitor.exe`).
2. Hộp thoại hiện ra → nhập backend (mỗi dòng 1 URL), port nghe đầu tiên, tick `--insecure` nếu backend là HTTPS cert tự ký.
3. Bấm **Bắt đầu**. Lần sau tool nhớ cấu hình cũ, mở lên là chạy thẳng.

### Cách 2: Dòng lệnh

```bash
# 1 backend
python network_monitor.py --mode reverse --port 9000 --target http://127.0.0.1:8081

# Nhiều backend (mỗi --target thêm 1 port kế tiếp: 9000, 9001, ...)
python network_monitor.py --mode reverse --port 9000 \
  --target http://127.0.0.1:8080 \
  --target http://192.168.1.110:8080

# Backend HTTPS cert tự ký
python network_monitor.py --mode reverse --port 9000 --target https://127.0.0.1:8443 --insecure

# Chế độ forward proxy
python network_monitor.py --mode forward --port 8080
```

Xem đầy đủ tham số:

```bash
python network_monitor.py --help
```

## Chế độ `reverse` (mặc định, khuyên dùng)

Tool mở 1 cổng (vd `9000`) giả làm backend. Frontend gọi vào tool thay vì gọi thẳng backend Java; tool forward request tới backend thật, log lại, rồi trả y nguyên response về.

| | |
|---|---|
| Khi nào dùng | Đổi được base URL API mà frontend gọi lúc dev |
| Cần cấu hình proxy/CA | **Không** — chạy ở tầng ứng dụng nên không phụ thuộc OS, kể cả backend HTTPS |

### Đấu nối frontend (lúc dev)

Đổi base URL từ địa chỉ backend thật:

```
http://127.0.0.1:8081
```

thành địa chỉ tool:

```
http://127.0.0.1:9000
```

Nếu base URL nằm trong biến môi trường (vd Vite), chỉ cần đổi biến đó, không sửa code:

```bash
VITE_API_BASE_URL=http://127.0.0.1:9000 npm run tauri dev
```

### Nhiều backend cùng lúc

Mỗi `--target` được gán 1 port riêng bắt đầu từ `--port`:

| Cổng tool | Forward tới |
|---|---|
| `127.0.0.1:9000` | `http://127.0.0.1:8080` |
| `127.0.0.1:9001` | `http://192.168.1.110:8080` |

Đổi base URL tương ứng trong frontend. Request từ cả 2 backend hiện **chung một GUI**, phân biệt qua cột URL (URL hiển thị là địa chỉ backend thật).

## Chế độ `forward` (khi không đổi được base URL)

Chạy 1 forward proxy dựa trên `mitmproxy`:

```bash
python network_monitor.py --mode forward --port 8080
```

Rồi trỏ app đi qua proxy `127.0.0.1:8080`. Vì request do webview của Tauri phát ra, bắt được hay không **phụ thuộc webview từng OS**:

- **Linux (WebKitGTK):** thường tôn trọng biến môi trường:
  ```bash
  http_proxy=http://127.0.0.1:8080 https_proxy=http://127.0.0.1:8080 npm run tauri dev
  ```
- **Windows (WebView2) / macOS (WKWebView):** thường chỉ theo **proxy hệ thống**. Vào Settings đổi proxy hệ thống về `127.0.0.1:8080` lúc debug, xong nhớ tắt.

### Xem nội dung HTTPS (giải mã TLS)

1. Chạy tool 1 lần để `mitmproxy` sinh CA tại `~/.mitmproxy/mitmproxy-ca-cert.pem`.
2. Cài file này làm chứng chỉ gốc tin cậy:
   - Windows: import vào *Trusted Root Certification Authorities*.
   - macOS: Keychain Access → *Always Trust*.
   - Linux: copy vào `/usr/local/share/ca-certificates/` (đổi đuôi `.crt`), chạy `sudo update-ca-certificates`.

Bỏ qua bước này vẫn thấy request (URL, status, thời gian) nhưng không đọc được headers/body của HTTPS.

> **Kết luận:** trừ khi base URL bị hardcode không đổi được, luôn nên dùng `reverse`.

## Sử dụng GUI

- **Bảng trên:** danh sách request theo thời gian thực (thời gian, method, status, thời lượng ms, URL).
- **Ô Lọc URL:** gõ để lọc nhanh theo URL.
- **Click 1 dòng:** xem chi tiết ở 4 tab dưới — Request Headers / Request Body / Response Headers / Response Body. Nút **Format JSON** để pretty-print body.
- **Follow:** bật thì list tự cuộn theo request mới; cuộn tay lên xem request cũ (hoặc click dòng cũ) sẽ tự tắt.
- **Copy as cURL:** chọn 1 request → bấm nút hoặc chuột phải → lệnh curl kiểu Chrome DevTools được copy vào clipboard (body quá 200k ký tự sẽ bị cắt kèm cảnh báo).
- **Xóa danh sách:** xóa log hiện tại, proxy vẫn chạy.
- **Đổi backend/port** (chỉ `reverse`): đổi cấu hình ngay khi đang chạy, không cần restart app.
- **Dark mode:** bật/tắt trên toolbar, được lưu lại.
- Giới hạn để khỏi treo GUI: tối đa **3000** request (cũ nhất tự xóa), body hiển thị cắt ở **50k** ký tự.

## File cấu hình

Tool tự lưu backend/port/`insecure`/dark mode sau mỗi lần chạy (CLI hoặc dialog) để lần sau khỏi nhập lại. Thứ tự tìm/ ghi:

1. Cạnh file `.py` / `.exe` (`network_monitor_config.json`).
2. Fallback `%APPDATA%\NetworkMonitor\config.json` (phòng khi exe đặt chỗ không có quyền ghi như `Program Files`).

Ví dụ:

```json
{
  "port": 9000,
  "targets": ["http://localhost:8388"],
  "insecure": false,
  "dark_mode": true
}
```

## Build file `.exe` (Windows)

Project đã có sẵn `NetworkMonitor.spec` (loại `mitmproxy`, `console=False` — bản exe chỉ chạy chế độ `reverse`):

```bash
pip install pyinstaller
pyinstaller NetworkMonitor.spec
```

File ra tại `dist/NetworkMonitor.exe`. Sau khi copy thư mục app đi nơi khác, chạy `tao_shortcut_desktop.bat` 1 lần để tạo shortcut ngoài Desktop.

## Cấu trúc project

```
py_devtool/
├── network_monitor.py            # toàn bộ tool (reverse proxy + forward proxy + GUI)
├── requirements.txt              # mitmproxy (chỉ cần cho --mode forward)
├── NetworkMonitor.spec           # cấu hình PyInstaller
├── network_monitor_config.json   # cấu hình đã lưu (tự sinh)
├── tao_shortcut_desktop.bat      # tạo shortcut Desktop cho bản .exe
├── dist/NetworkMonitor.exe       # bản build sẵn (Windows)
└── README.md
```

## Xử lý sự cố

| Hiện tượng | Cách xử lý |
|---|---|
| `Port đã bị app khác chiếm` | Đổi `--port` khác. Tool có kiểm tra chủ động vì trên Windows 2 app có thể bind trùng port mà không báo lỗi. |
| Backend HTTPS báo lỗi TLS | Chạy thêm `--insecure` (chỉ dùng lúc dev). |
| `forward` không bắt được request (Win/macOS) | Webview không đọc biến môi trường — phải set proxy ở cấp hệ thống + cài CA cert (xem trên). |
| `forward` báo thiếu mitmproxy | `pip install -r requirements.txt` hoặc chuyển sang `--mode reverse`. |
| GUI hiện `...` mãi không có response | Backend chưa trả lời (treo/timeout) — kiểm tra backend thật có sống không. |
| Click request cũ list đứng yên | Đúng thiết kế: Follow tự tắt khi xem request cũ; muốn bám request mới thì bật lại Follow hoặc click dòng mới nhất. |

## Ghi chú

- Đây là công cụ dev/debug, không bật ở production.
- Nếu port bị chiếm, đổi bằng `--port <port khác>`.
