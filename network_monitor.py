#!/usr/bin/env python3
"""
Network Monitor - theo doi HTTP/HTTPS request cua mot ung dung (vi du: Tauri app dev,
frontend goi thang toi 1 backend Java rieng).

GUI kieu DevTools Network tab, ho tro 2 CHE DO bat request:

1) reverse  (khuyen nghi khi frontend goi thang Java backend, khong qua Rust/reqwest)
   Tool dong vai tro 1 reverse proxy: frontend goi vao tool, tool forward toi
   Java backend that va tra response ve. Khong can cau hinh proxy he thong,
   khong can cai chung chi CA. Chi can doi base URL frontend dung trong luc dev.

   Vi du:
        python network_monitor.py --mode reverse --port 9000 --target http://127.0.0.1:8081

   Frontend dev: doi API base URL tu http://127.0.0.1:8081 -> http://127.0.0.1:9000

   Ho tro NHIEU backend: lap lai --target, moi target duoc gan 1 port lang nghe
   rieng bat dau tu --port (--port, --port+1, ...), request tu tat ca backend
   van hien chung 1 GUI:

        python network_monitor.py --mode reverse --port 9000 ^
            --target http://127.0.0.1:8080 ^
            --target http://192.168.1.110:8080

        -> 127.0.0.1:9000 forward toi http://127.0.0.1:8080
        -> 127.0.0.1:9001 forward toi http://192.168.1.110:8080

2) forward  (dung khi khong doi duoc base URL cua frontend)
   Tool chay 1 forward proxy (dua tren mitmproxy). Can cau hinh app/OS de di
   qua proxy nay (xem README.md). Yeu cau: pip install mitmproxy

        python network_monitor.py --mode forward --port 8080

Neu chay khong truyen --target (vi du double-click file .exe), tool se mo hop
thoai de nhap backend/port/tuy chon TLS ngay tren giao dien, khong can terminal.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import queue
import socket
import ssl
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tkinter import messagebox, ttk

MAX_BODY_CHARS = 50_000  # gioi han hien thi de tranh GUI bi treo voi body qua lon
MAX_RAW_BODY_CHARS = 200_000  # body giu lai de copy cURL (qua muc nay thi cat + canh bao)
MAX_ITEMS = 3_000  # chan log vo han: vuot qua se xoa request cu nhat
POLL_BATCH = 200  # so item toi da xu ly moi lan poll de tranh treo GUI

# Bo dem id don dieu, khong bao gio trung (khac voi ban cu dung timestamp % 10M
# de lam id -> de trung iid cua Treeview -> TclError lam chet vong poll GUI).
_id_counter = itertools.count(1)

CONFIG_FILENAME = "network_monitor_config.json"


# --------------------------------------------------------------------------
# Luu / tai cau hinh (nho backend/port lan cuoi de lan sau khoi nhap lai)
# --------------------------------------------------------------------------
def _config_candidates() -> "list[str]":
    """Thu tu uu tien: canh file .py / .exe truoc, fallback ve APPDATA
    (phong truong hop exe dat o cho khong co quyen ghi nhu Program Files)."""
    cands = []
    if getattr(sys, "frozen", False):
        cands.append(os.path.join(os.path.dirname(sys.executable), CONFIG_FILENAME))
    else:
        cands.append(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_FILENAME)
        )
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    cands.append(os.path.join(appdata, "NetworkMonitor", "config.json"))
    return cands


def load_saved_config(path: str | None = None) -> dict | None:
    """Doc cau hinh da luu, validate qua _parse_setup. Tra ve None neu
    khong co / hong / khong hop le."""
    paths = [path] if path else _config_candidates()
    for p in paths:
        try:
            if not os.path.isfile(p):
                continue
            with open(p, "r", encoding="utf-8") as f:
                raw = json.load(f)
            targets = raw.get("targets") or []
            err, cfg = _parse_setup(
                "\n".join(targets) if isinstance(targets, list) else str(targets),
                str(raw.get("port", "")),
                bool(raw.get("insecure", False)),
            )
            if err:
                continue
            cfg["dark_mode"] = bool(raw.get("dark_mode", False))
            return cfg
        except Exception:
            continue
    return None


def save_config(cfg: dict, path: str | None = None) -> str | None:
    """Luu cau hinh, tra ve duong dan file da ghi (None neu that bai)."""
    data = {
        "port": cfg["port"],
        "targets": list(cfg["targets"]),
        "insecure": bool(cfg.get("insecure", False)),
        "dark_mode": bool(cfg.get("dark_mode", False)),
    }
    paths = [path] if path else _config_candidates()
    for p in paths:
        try:
            parent = os.path.dirname(p)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return p
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------
# Tien ich dung chung
# --------------------------------------------------------------------------
def safe_text(data: bytes, limit: int = MAX_BODY_CHARS) -> str:
    if not data:
        return ""
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return f"<binary data, {len(data)} bytes>"
    if len(text) > limit:
        return text[:limit] + f"\n\n...[da cat bot, tong {len(text)} ky tu]"
    return text


def full_text(data: bytes, limit: int = MAX_RAW_BODY_CHARS) -> "tuple[str, bool]":
    """Giu body day du (khong cat hien thi) de copy cURL. Tra ve
    (text, bi_cat_hay_khong)."""
    if not data:
        return "", False
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return f"<binary data, {len(data)} bytes>", True
    if len(text) > limit:
        return text[:limit], True
    return text, False


def _bash_quote(s: str) -> str:
    """Quote chuoi theo kieu bash single-quote (giong Chrome copy as cURL)."""
    return "'" + s.replace("'", "'\\''") + "'"


def to_curl(item: dict) -> str:
    """Dung lenh curl (bash) tu 1 request da bat, kieu Chrome DevTools."""
    parts = ["curl", _bash_quote(item.get("url", ""))]
    method = item.get("method", "GET")
    if method != "GET":
        parts += ["-X", method]
    for k, v in (item.get("req_headers") or {}).items():
        if k.lower() in ("content-length", "connection"):
            continue  # curl tu tinh Content-Length
        parts += ["-H", _bash_quote(f"{k}: {v}")]
    body = item.get("req_body_raw") or ""
    if body:
        parts += ["--data-raw", _bash_quote(body)]
    cmd = " ".join(parts)
    if body and item.get("req_body_cut"):
        cmd = "# WARNING: body qua lon nen da bi cat bot\n" + cmd
    return cmd


# --------------------------------------------------------------------------
# CHE DO 1: REVERSE PROXY - chi dung thu vien chuan, khong can cai gi them
# --------------------------------------------------------------------------
class _ReverseProxyHandler(BaseHTTPRequestHandler):
    target_base_url: str = ""
    out_queue: "queue.Queue" = None
    insecure: bool = False
    protocol_version = "HTTP/1.1"
    # Tra loi xong la dong ket noi luon: tranh giu thread treo voi keep-alive
    # idle, tranh socket bi lech khi client gui Transfer-Encoding: chunked
    # (truong hop nay doc Content-Length khong du -> request sau tren cung
    # socket se hong). Dev tool thi dong-mo ket noi moi request la on.
    close_connection = True
    timeout = 30

    def log_message(self, fmt, *args):
        pass  # tat log mac dinh in ra console

    def _handle(self, method: str):
        start = time.time()
        req_id = next(_id_counter)
        req_time = time.strftime("%H:%M:%S")
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length < 0:
                length = 0
        except (TypeError, ValueError):
            length = 0
        try:
            body = self.rfile.read(length) if length else b""
        except Exception:
            body = b""
        # Dam bao khong giu keep-alive: doc xong la se dong socket o cuoi.
        self.close_connection = True

        target_url = self.target_base_url.rstrip("/") + self.path

        fwd_headers = {}
        for k, v in self.headers.items():
            if k.lower() in ("host", "content-length", "connection"):
                continue
            fwd_headers[k] = v

        # Bao GUI ngay: request dang goi (hien dong mau xam truoc khi co response)
        if self.out_queue is not None:
            try:
                self.out_queue.put_nowait({
                    "__start__": True,
                    "id": req_id,
                    "time": req_time,
                    "method": method,
                    "url": target_url,
                    "req_headers": dict(fwd_headers),
                    "req_body": safe_text(body),
                })
            except queue.Full:
                pass

        req = urllib.request.Request(
            target_url, data=body if body else None, headers=fwd_headers, method=method
        )

        ctx = None
        if self.insecure and target_url.startswith("https://"):
            ctx = ssl._create_unverified_context()

        status = 502
        res_headers = {}
        res_body = b""
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                status = resp.status
                res_headers = dict(resp.headers.items())
                res_body = resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            res_headers = dict(e.headers.items()) if e.headers else {}
            res_body = e.read() or b""
        except Exception as e:
            status = "ERROR"
            res_body = str(e).encode("utf-8", errors="replace")

        duration_ms = round((time.time() - start) * 1000, 1)

        raw_body, body_cut = full_text(body)
        item = {
            "__finish__": True,
            "id": req_id,
            "time": req_time,
            "method": method,
            "url": target_url,
            "status": status,
            "duration_ms": duration_ms,
            "req_headers": fwd_headers,
            "req_body": safe_text(body),
            "req_body_raw": raw_body,  # de copy as cURL (khong cat hien thi)
            "req_body_cut": body_cut,
            "res_headers": res_headers,
            "res_body": safe_text(res_body),
        }
        if self.out_queue is not None:
            try:
                self.out_queue.put_nowait(item)
            except queue.Full:
                pass

        # tra response that ve cho frontend
        try:
            self.send_response(status if isinstance(status, int) else 502)
            for k, v in res_headers.items():
                if k.lower() in ("content-length", "transfer-encoding", "connection"):
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(res_body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if method != "HEAD" and res_body:
                self.wfile.write(res_body)
        except Exception:
            pass  # client co the da dong ket noi

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_OPTIONS(self):
        self._handle("OPTIONS")

    def do_HEAD(self):
        self._handle("HEAD")


def find_occupied_ports(ports: "list[int]", own_ports: "set[int]") -> "list[int]":
    """Kiem tra port da co app KHAC dang nghe chua. Can thiet vi tren Windows
    SO_REUSEADDR cho phep bind trung port ma khong bao loi (2 app chia nhau
    traffic rat nguy hiem), nen khong the trong cho OSError khi bind."""
    occupied = []
    for port in ports:
        if port in own_ports:
            continue  # port cua chinh backend cu, restart se tat no truoc
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                occupied.append(port)
        except OSError:
            pass
    return occupied


def create_reverse_server(out_queue: "queue.Queue", port: int, target: str, insecure: bool):
    """Tao reverse-proxy server (da bind port, chua serve). Raise OSError neu
    port bi chiem -> de ben goi bat loi truoc khi dung backend cu."""
    handler_cls = type(
        "BoundReverseProxyHandler",
        (_ReverseProxyHandler,),
        {"target_base_url": target, "out_queue": out_queue, "insecure": insecure},
    )
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    # Thread xu ly request de daemon, tranh doi thread cu dong het moi tat
    # duoc server / khong bind lai duoc.
    server.daemon_threads = True
    return server


def run_reverse_proxy_thread(out_queue: "queue.Queue", port: int, target: str, insecure: bool):
    try:
        server = create_reverse_server(out_queue, port, target, insecure)
        server.serve_forever(poll_interval=0.5)
    except Exception as e:
        try:
            out_queue.put_nowait({"__error__": str(e)})
        except queue.Full:
            pass


# --------------------------------------------------------------------------
# CHE DO 2: FORWARD PROXY - dua tren mitmproxy (import lazy, chi can khi dung mode nay)
# --------------------------------------------------------------------------
def run_forward_proxy_thread(out_queue: "queue.Queue", port: int):
    try:
        import asyncio

        from mitmproxy import http, options
        from mitmproxy.tools.dump import DumpMaster
    except ImportError:
        out_queue.put(
            {"__error__": "Chua cai mitmproxy. Chay: pip install mitmproxy (hoac dung --mode reverse)."}
        )
        return

    class CaptureAddon:
        def _next_id(self) -> int:
            return next(_id_counter)

        @staticmethod
        def _flow_key(flow: "http.HTTPFlow") -> str:
            return f"fw-{id(flow)}"

        def request(self, flow: "http.HTTPFlow") -> None:
            req = flow.request
            try:
                out_queue.put_nowait({
                    "__start__": True,
                    "id": self._flow_key(flow),
                    "time": time.strftime("%H:%M:%S"),
                    "method": req.method,
                    "url": req.pretty_url,
                    "req_headers": dict(req.headers),
                    "req_body": safe_text(req.raw_content),
                })
            except queue.Full:
                pass

        def response(self, flow: "http.HTTPFlow") -> None:
            req = flow.request
            res = flow.response
            duration_ms = None
            try:
                if res and req.timestamp_start and res.timestamp_end:
                    duration_ms = round((res.timestamp_end - req.timestamp_start) * 1000, 1)
            except Exception:
                pass
            raw_body, body_cut = full_text(req.raw_content or b"")
            out_queue.put(
                {
                    "__finish__": True,
                    "id": self._flow_key(flow),
                    "time": time.strftime("%H:%M:%S"),
                    "method": req.method,
                    "url": req.pretty_url,
                    "status": res.status_code if res else "-",
                    "duration_ms": duration_ms,
                    "req_headers": dict(req.headers),
                    "req_body": safe_text(req.raw_content),
                    "req_body_raw": raw_body,
                    "req_body_cut": body_cut,
                    "res_headers": dict(res.headers) if res else {},
                    "res_body": safe_text(res.raw_content) if res else "",
                }
            )

        def error(self, flow: "http.HTTPFlow") -> None:
            req = flow.request
            out_queue.put(
                {
                    "__finish__": True,
                    "id": self._flow_key(flow),
                    "time": time.strftime("%H:%M:%S"),
                    "method": req.method if req else "?",
                    "url": req.pretty_url if req else "?",
                    "status": "ERROR",
                    "duration_ms": None,
                    "req_headers": dict(req.headers) if req else {},
                    "req_body": "",
                    "req_body_raw": "",
                    "req_body_cut": False,
                    "res_headers": {},
                    "res_body": str(flow.error) if flow.error else "",
                }
            )

    async def _main():
        opts = options.Options(listen_host="127.0.0.1", listen_port=port)
        master = DumpMaster(opts, with_termlog=False, with_dumper=False)
        master.addons.add(CaptureAddon())
        try:
            await master.run()
        finally:
            master.shutdown()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_main())
    except Exception as e:
        out_queue.put({"__error__": str(e)})


# --------------------------------------------------------------------------
# GUI: tkinter, kieu DevTools Network tab (dung chung cho ca 2 che do)
# --------------------------------------------------------------------------
class NetworkMonitorApp:
    def __init__(self, root: tk.Tk, mode: str, listeners: "list[tuple[int, str | None]]", insecure: bool,
                 dark_mode: bool = False):
        self.root = root
        self.mode = mode
        # listeners: list cap (port_lang_nghe, target); forward mode target = None
        self.listeners = listeners
        self.insecure = insecure
        self.dark_mode = bool(dark_mode)

        if mode == "reverse":
            mapping_txt = " | ".join(f"127.0.0.1:{p} -> {t}" for p, t in listeners)
            self.base_status = f"Reverse proxy: {mapping_txt}"
            subtitle = "reverse -> " + "; ".join(f"{p} -> {t}" for p, t in listeners)
        else:
            self.base_status = f"Forward proxy dang lang nghe 127.0.0.1:{listeners[0][0]} ..."
            subtitle = "forward proxy"
        self.root.title(f"Network Monitor [{subtitle}]")
        self.root.geometry("1200x700")
        set_titlebar_dark(self.root, self.dark_mode)
        # Re-apply khi window hien that (DWM co the bo qua luc chua map)
        self.root.after(500, lambda: set_titlebar_dark(self.root, self.dark_mode))

        self.data_queue: "queue.Queue" = queue.Queue(maxsize=5000)
        self.all_items = []
        self.item_by_iid = {}
        self.pending_by_iid = {}  # req id -> tree iid (dang cho response)
        self.pending_item_by_id = {}  # req id -> item (dang cho response)
        self._dropped_ids = set()  # id da xoa, finish muon se bi bo qua
        self.servers: list = []  # giu server reverse dang chay de restart doi port luc dang chay

        self._build_ui()
        # Ghi nho mau root de doi theme (phong truong hop ttk bo qua)
        self._root_bg = self.root.cget("background")
        self._apply_theme_mode()
        self._start_backend()
        self._poll_queue()

    # ---------------- Mapping / title / status ----------------
    def _refresh_title_status(self):
        for w in self.pill_frame.winfo_children():
            w.destroy()
        dot_color = "#16a34a" if not self.dark_mode else "#4ade80"
        if self.mode == "reverse":
            for p, t in self.listeners:
                ttk.Label(self.pill_frame, text="●", foreground=dot_color).pack(side=tk.LEFT)
                ttk.Label(self.pill_frame, text=f"  {p} → {t}  ",
                          style="Pill.TLabel").pack(side=tk.LEFT, padx=(0, 8))
            subtitle = "reverse -> " + "; ".join(f"{p} -> {t}" for p, t in self.listeners)
        else:
            ttk.Label(self.pill_frame, text="●", foreground=dot_color).pack(side=tk.LEFT)
            ttk.Label(self.pill_frame, text=f"  forward proxy :{self.listeners[0][0]}  ",
                      style="Pill.TLabel").pack(side=tk.LEFT)
            subtitle = "forward proxy"
        self.root.title(f"Network Monitor [{subtitle}]")
        self.mode_var.set("reverse" if self.mode == "reverse" else "forward")
        self._update_count()

    # ---------------- UI layout ----------------
    def _build_ui(self):
        # Header: tieu de + pills ket noi + actions
        header = ttk.Frame(self.root)
        header.pack(side=tk.TOP, fill=tk.X, padx=12, pady=(10, 2))
        ttk.Label(header, text="Network Monitor",
                  font=("Segoe UI", 13, "bold")).pack(side=tk.LEFT)
        self.pill_frame = ttk.Frame(header)
        self.pill_frame.pack(side=tk.LEFT, padx=(16, 0))
        actions = ttk.Frame(header)
        actions.pack(side=tk.RIGHT)
        if self.mode == "reverse":
            ttk.Button(actions, text="Backend / Port",
                       command=self._open_settings).pack(side=tk.LEFT, padx=(0, 8))
        self.dark_var = tk.BooleanVar(value=self.dark_mode)
        ttk.Checkbutton(actions, text="Dark", variable=self.dark_var,
                        command=self._on_dark_toggle).pack(side=tk.LEFT)

        # Filter bar: search + chips + actions list
        fbar = ttk.Frame(self.root)
        fbar.pack(side=tk.TOP, fill=tk.X, padx=12, pady=(6, 6))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._apply_filter())
        self.filter_entry = ttk.Entry(fbar, textvariable=self.filter_var, width=34)
        self.filter_entry.pack(side=tk.LEFT)
        self._ph_text = "Filter by URL or method..."
        self._ph_shown = True
        self.filter_entry.insert(0, self._ph_text)
        try:
            self.filter_entry.configure(foreground=self._muted_color_static(self.dark_mode))
        except Exception:
            pass
        self.filter_entry.bind("<FocusIn>", self._on_search_focus_in)
        self.filter_entry.bind("<FocusOut>", self._on_search_focus_out)
        chips = ttk.Frame(fbar)
        chips.pack(side=tk.LEFT, padx=(10, 0))
        self.chip_buttons = {}
        for name in ("All", "GET", "POST", "PUT", "DELETE", "Errors"):
            btn = ttk.Button(chips, text=name, width=7,
                             command=lambda n=name: self._set_chip(n))
            btn.pack(side=tk.LEFT, padx=(0, 4))
            self.chip_buttons[name] = btn
        self._set_chip("All", refresh=False)
        list_actions = ttk.Frame(fbar)
        list_actions.pack(side=tk.RIGHT)
        self.follow_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(list_actions, text="Follow", variable=self.follow_var,
                        command=self._on_follow_toggle).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(list_actions, text="Copy cURL",
                   command=self._copy_curl).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(list_actions, text="Clear", command=self._clear_all).pack(side=tk.LEFT)

        self.status_var = tk.StringVar(value="Dang khoi dong...")
        self.mode_var = tk.StringVar(value="")
        self.method_filter = "ALL"

        statusbar = ttk.Frame(self.root)
        statusbar.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(0, 8))
        ttk.Label(statusbar, textvariable=self.status_var,
                  style="Muted.TLabel").pack(side=tk.LEFT)
        ttk.Label(statusbar, textvariable=self.mode_var,
                  style="Muted.TLabel").pack(side=tk.RIGHT)

        main_pane = ttk.PanedWindow(self.root, orient=tk.VERTICAL)
        main_pane.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)

        list_frame = ttk.Frame(main_pane)
        columns = ("time", "method", "status", "duration", "url")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("time", text="Thoi gian")
        self.tree.heading("method", text="Method")
        self.tree.heading("status", text="Status")
        self.tree.heading("duration", text="Thoi luong (ms)")
        self.tree.heading("url", text="URL")
        self.tree.column("time", width=90, anchor=tk.CENTER)
        self.tree.column("method", width=70, anchor=tk.CENTER)
        self.tree.column("status", width=70, anchor=tk.CENTER)
        self.tree.column("duration", width=110, anchor=tk.CENTER)
        self.tree.column("url", width=700, anchor=tk.W)

        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self._on_user_scroll)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.tag_configure("error", background="#ffd9d9")  # request loi: nen do nhat
        self.tree.tag_configure("pending", foreground="#888888")  # request dang goi: chu xam
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Button-3>", self._on_right_click)
        # Cuon tay (chuot/phim) -> neu dong moi nhat khong con hien thi thi tat Follow
        self.tree.bind("<MouseWheel>", self._on_wheel_or_key_scroll)
        self.tree.bind("<Button-4>", self._on_wheel_or_key_scroll)  # Linux cuon len
        self.tree.bind("<Button-5>", self._on_wheel_or_key_scroll)  # Linux cuon xuong
        self.tree.bind("<Prior>", self._on_wheel_or_key_scroll)  # PageUp
        self.tree.bind("<Next>", self._on_wheel_or_key_scroll)  # PageDown
        main_pane.add(list_frame, weight=2)

        detail_frame = ttk.Frame(main_pane)
        self.notebook = ttk.Notebook(detail_frame)
        self.req_headers_tree = self._make_headers_tab("Request Headers")
        self.req_body_text = self._make_text_tab("Request Body", with_format=True)
        self.res_headers_tree = self._make_headers_tab("Response Headers")
        self.res_body_text = self._make_text_tab("Response Body", with_format=True)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        main_pane.add(detail_frame, weight=2)

    def _make_headers_tab(self, title: str) -> ttk.Treeview:
        """Tab hien thi headers dang bang 2 cot: Ten header | Gia tri."""
        frame = ttk.Frame(self.notebook)
        tree = ttk.Treeview(frame, columns=("name", "value"), show="headings", selectmode="extended")
        tree.heading("name", text="Ten header")
        tree.heading("value", text="Gia tri")
        tree.column("name", width=220, anchor=tk.W, stretch=False)
        tree.column("value", width=600, anchor=tk.W, stretch=True)
        vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.notebook.add(frame, text=title)
        return tree

    @staticmethod
    def _set_headers(tree: ttk.Treeview, headers: dict):
        tree.delete(*tree.get_children())
        for k, v in (headers or {}).items():
            tree.insert("", tk.END, values=(str(k), str(v)))

    def _make_text_tab(self, title: str, with_format: bool = False) -> tk.Text:
        frame = ttk.Frame(self.notebook)
        if with_format:
            bar = ttk.Frame(frame)
            bar.pack(side=tk.TOP, fill=tk.X)
            btn = ttk.Button(bar, text="Format JSON")
            btn.pack(side=tk.LEFT, padx=(2, 4), pady=2)
            copy_btn = ttk.Button(bar, text="Copy")
            copy_btn.pack(side=tk.LEFT, padx=(0, 2), pady=2)
        text = tk.Text(frame, wrap=tk.WORD, font=("Consolas", 10))
        vsb = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=vsb.set, state=tk.DISABLED)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        if with_format:
            btn.configure(command=lambda: self._format_json_text(text))
            copy_btn.configure(command=lambda: self._copy_widget_text(text))
        self.notebook.add(frame, text=title)
        return text

    def _copy_widget_text(self, widget: tk.Text):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(widget.get("1.0", tk.END).strip())
            self.root.update()
        except Exception as e:
            messagebox.showerror("Copy", f"Khong copy duoc: {e}")

    @staticmethod
    def _format_json_text(widget: tk.Text):
        """Format noi dung JSON trong tab body dang xem. Khong phai JSON thi bao loi."""
        raw = widget.get("1.0", tk.END).strip()
        if not raw or raw.startswith("("):
            return  # khong co body -> im lang, khong lam phien
        try:
            obj = json.loads(raw)
        except Exception:
            messagebox.showwarning(
                "Format JSON", "Noi dung khong phai JSON hop le, giu nguyen."
            )
            return
        pretty = json.dumps(obj, indent=2, ensure_ascii=False)
        NetworkMonitorApp._set_text(widget, pretty)

    # ---------------- Theme: shadcn-minimal (zinc), thuan stdlib ----------------
    LIGHT = {
        "bg": "#ffffff", "fg": "#09090b", "muted_fg": "#71717a",
        "field": "#ffffff", "border": "#e4e4e7",
        "btn": "#ffffff", "btn_fg": "#09090b", "btn_hover": "#f4f4f5",
        "tree": "#ffffff", "head": "#fafafa", "head_hover": "#f4f4f5",
        "sel_bg": "#e4e4e7", "sel_fg": "#09090b",
        "tab_sel": "#ffffff",
        "scroll": "#d4d4d8",
        "text_bg": "#ffffff", "text_fg": "#09090b",
        "text_sel_bg": "#e4e4e7", "text_sel_fg": "#09090b",
        "error_bg": "#fef2f2", "pending_fg": "#a1a1aa",
        "primary": "#18181b", "primary_fg": "#fafafa", "primary_hover": "#27272a",
    }
    DARK = {
        "bg": "#27272a", "fg": "#fafafa", "muted_fg": "#b9b9c0",
        "field": "#27272a", "border": "#3f3f46",
        "btn": "#27272a", "btn_fg": "#fafafa", "btn_hover": "#3a3a40",
        "tree": "#27272a", "head": "#2e2e33", "head_hover": "#3a3a40",
        "sel_bg": "#3f3f46", "sel_fg": "#fafafa",
        "tab_sel": "#323237",
        "scroll": "#71717a",
        "text_bg": "#27272a", "text_fg": "#fafafa",
        "text_sel_bg": "#3f3f46", "text_sel_fg": "#fafafa",
        "error_bg": "#4c2323", "error_fg": "#fecaca", "pending_fg": "#a1a1aa",
        "primary": "#fafafa", "primary_fg": "#18181b", "primary_hover": "#e4e4e7",
    }

    def _on_dark_toggle(self):
        self.dark_mode = bool(self.dark_var.get())
        self._apply_theme_mode()
        set_titlebar_dark(self.root, self.dark_mode)
        try:
            save_config({
                "port": self.listeners[0][0],
                "targets": [t for _, t in self.listeners if t],
                "insecure": self.insecure,
                "dark_mode": self.dark_mode,
            })
        except Exception:
            pass

    def _apply_theme_mode(self):
        t = self.DARK if self.dark_mode else self.LIGHT
        style = ttk.Style(master=self.root)
        try:
            self.root.configure(background=t["bg"])
        except Exception:
            pass
        try:
            style.configure(".", font=("Segoe UI", 10))
            style.configure("TFrame", background=t["bg"])
            style.configure("TLabel", background=t["bg"], foreground=t["fg"])
            style.configure("Muted.TLabel", background=t["bg"], foreground=t["muted_fg"])
            style.configure("Pill.TLabel", background=t["btn_hover"], foreground=t["fg"],
                            padding=(4, 3), font=("Segoe UI", 9))
            style.configure("TButton", background=t["btn"], foreground=t["btn_fg"],
                            bordercolor=t["border"], lightcolor=t["btn"], darkcolor=t["border"])
            style.map("TButton",
                      background=[("active", t["btn_hover"]), ("disabled", t["bg"])],
                      foreground=[("disabled", t["muted_fg"])])
            style.configure("Primary.TButton", background=t["primary"],
                            foreground=t["primary_fg"], bordercolor=t["primary"])
            style.map("Primary.TButton", background=[("active", t["primary_hover"])])
            style.configure("TCheckbutton", background=t["bg"], foreground=t["fg"])
            style.map("TCheckbutton", background=[("active", t["bg"])])
            style.configure("TEntry", fieldbackground=t["field"], foreground=t["fg"],
                            insertcolor=t["fg"], bordercolor=t["border"])
            style.configure("TSpinbox", fieldbackground=t["field"], foreground=t["fg"],
                            insertcolor=t["fg"], background=t["btn"],
                            arrowcolor=t["fg"], bordercolor=t["border"])
            style.configure("Treeview", background=t["tree"], foreground=t["fg"],
                            fieldbackground=t["tree"], rowheight=26)
            style.map("Treeview",
                      background=[("selected", t["sel_bg"])],
                      foreground=[("selected", t["sel_fg"])])
            style.configure("Treeview.Heading", background=t["head"],
                            foreground=t["muted_fg"], relief="flat",
                            font=("Segoe UI", 9, "bold"))
            style.map("Treeview.Heading", background=[("active", t["head_hover"])])
            style.configure("TNotebook", background=t["bg"], borderwidth=0)
            # Tab phang, mem: bo vien 3D bang cach dong mau vien theo nen,
            # tab active noi len 1 tone thay vi phong to/bien dang
            style.configure("TNotebook.Tab", background=t["bg"],
                            foreground=t["muted_fg"], padding=(10, 4),
                            bordercolor=t["bg"], lightcolor=t["bg"],
                            darkcolor=t["bg"], focuscolor=t["bg"])
            style.map("TNotebook.Tab",
                      background=[("selected", t["tab_sel"])],
                      foreground=[("selected", t["fg"])],
                      bordercolor=[("selected", t["tab_sel"])],
                      lightcolor=[("selected", t["tab_sel"])],
                      darkcolor=[("selected", t["tab_sel"])])
            style.configure("TPanedwindow", background=t["bg"])
            for sc in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
                style.configure(sc, background=t["scroll"], troughcolor=t["bg"],
                                bordercolor=t["bg"], arrowcolor=t["muted_fg"])
        except Exception:
            pass
        for w in (self.req_body_text, self.res_body_text):
            try:
                w.configure(background=t["text_bg"], foreground=t["text_fg"],
                            insertbackground=t["fg"],
                            selectbackground=t["text_sel_bg"],
                            selectforeground=t["text_sel_fg"])
            except Exception:
                pass
        err_fg = t.get("error_fg", "")
        for tree in (self.tree, self.req_headers_tree, self.res_headers_tree):
            try:
                tree.tag_configure("error", background=t["error_bg"], foreground=err_fg)
            except Exception:
                pass
        try:
            self.tree.tag_configure("pending", foreground=t["pending_fg"])
        except Exception:
            pass
        try:
            if getattr(self, "_ph_shown", False):
                self.filter_entry.configure(foreground=t["muted_fg"])
        except Exception:
            pass

    # ---------------- Backend control ----------------
    def _serve_in_thread(self, server):
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True).start()

    def _stop_backend(self):
        for server in self.servers:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
        self.servers = []

    def _start_backend(self):
        self._refresh_title_status()
        if self.mode == "reverse":
            busy = find_occupied_ports([p for p, _ in self.listeners], set())
            if busy:
                messagebox.showerror("Loi", f"Port da bi app khac chiem: {busy}\nDoi port khac.")
            for port, target in self.listeners:
                if port in busy:
                    continue
                try:
                    server = create_reverse_server(self.data_queue, port, target, self.insecure)
                except OSError as e:
                    messagebox.showerror("Loi", f"Khong mo duoc port {port}: {e}")
                    continue
                self.servers.append(server)
                self._serve_in_thread(server)
        else:
            threading.Thread(
                target=run_forward_proxy_thread,
                args=(self.data_queue, self.listeners[0][0]),
                daemon=True,
            ).start()

    def _restart_reverse(self, new_listeners: "list[tuple[int, str]]", insecure: bool) -> bool:
        """Doi backend/port luc app dang chay. Kiem tra port bi chiem truoc;
        neu loi thi giu nguyen backend cu. Tra ve True neu restart thanh cong."""
        own_ports = {p for p, _ in self.listeners}
        new_ports = [p for p, _ in new_listeners]
        busy = find_occupied_ports(new_ports, own_ports)
        if busy:
            messagebox.showerror(
                "Loi",
                f"Port da bi app khac chiem: {busy}\nGiu nguyen cau hinh cu.",
                parent=self.root,
            )
            return False
        new_servers = []
        try:
            for port, target in new_listeners:
                new_servers.append(create_reverse_server(self.data_queue, port, target, insecure))
        except OSError as e:
            for s in new_servers:
                try:
                    s.server_close()
                except Exception:
                    pass
            messagebox.showerror("Loi", f"Khong mo duoc port: {e}\nGiu nguyen cau hinh cu.", parent=self.root)
            return False
        self._stop_backend()
        self.listeners = new_listeners
        self.insecure = insecure
        self.servers = new_servers
        for server in new_servers:
            self._serve_in_thread(server)
        self._refresh_title_status()
        return True

    def _open_settings(self):
        """Dialog doi backend/port/insecure ngay luc app dang chay."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Doi backend / port")
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()

        frm = ttk.Frame(dlg, padding=14)
        frm.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frm, text="Backend can theo doi (moi dong 1 URL):").grid(
            row=0, column=0, columnspan=3, sticky="w"
        )
        targets_txt = tk.Text(frm, width=54, height=4, font=("Consolas", 10))
        targets_txt.grid(row=1, column=0, columnspan=3, sticky="we", pady=(2, 2))
        targets_txt.insert("1.0", "\n".join(t for _, t in self.listeners))

        ttk.Label(
            frm,
            text="Target thu 2 tro di tu dong dung port ke tiep (+1, +2, ...)",
            foreground="gray",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 8))

        ttk.Label(frm, text="Port lang nghe dau tien:").grid(row=3, column=0, sticky="w")
        port_var = tk.StringVar(value=str(self.listeners[0][0]))
        spin = ttk.Spinbox(frm, from_=1, to=65535, textvariable=port_var, width=8)
        spin.grid(row=3, column=1, sticky="w", padx=(4, 0))

        insecure_var = tk.BooleanVar(value=self.insecure)
        ttk.Checkbutton(
            frm,
            text="Khong kiem tra chung chi TLS (backend https cert tu ky)",
            variable=insecure_var,
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 12))

        def _apply():
            err, cfg = _parse_setup(
                targets_txt.get("1.0", "end"), port_var.get(), bool(insecure_var.get())
            )
            if err:
                messagebox.showerror("Loi", err, parent=dlg)
                return
            new_listeners = [(cfg["port"] + i, t) for i, t in enumerate(cfg["targets"])]
            if self._restart_reverse(new_listeners, cfg["insecure"]):
                cfg["dark_mode"] = self.dark_mode  # doi backend khong lam mat dark mode
                save_config(cfg)  # nho de lan sau mo app khoi nhap lai
                dlg.destroy()

        btns = ttk.Frame(frm)
        btns.grid(row=5, column=0, columnspan=3, sticky="e")
        ttk.Button(btns, text="Huy", command=dlg.destroy).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(btns, text="Ap dung", command=_apply).pack(side=tk.RIGHT)

        dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
        dlg.bind("<Return>", lambda _e: _apply())
        dlg.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - dlg.winfo_reqwidth()) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - dlg.winfo_reqheight()) // 3
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
        set_titlebar_dark(dlg, self.dark_mode)
        spin.focus_set()
        spin.select_range(0, "end")

    # ---------------- Queue polling ----------------
    def _poll_queue(self):
        # Vong poll nay KHONG duoc chet bao gio: moi loi cua 1 item phai duoc
        # catch rieng, va root.after() phai chay trong finally. Ban cu de
        # ngoai le (vd trung iid) lam dung vong poll -> GUI "khong nhan req
        # nua" du proxy van chay.
        try:
            for _ in range(POLL_BATCH):
                try:
                    item = self.data_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    if "__error__" in item:
                        messagebox.showerror("Loi", item["__error__"])
                        continue
                    self._add_item(item)
                except Exception:
                    # Bo qua item loi, giu GUI song de nhan req tiep theo
                    continue
        finally:
            self.root.after(200, self._poll_queue)

    def _add_item(self, item: dict):
        if item.get("__finish__"):
            self._finish_item(item)
            return
        if item.get("__start__"):
            # Request moi bat dau goi -> hien dong xam truoc, cho response
            item["_pending"] = True
            self.pending_item_by_id[item["id"]] = item
        self._append_row(item)

    def _finish_item(self, fin: dict):
        if fin["id"] in self._dropped_ids:
            self._dropped_ids.discard(fin["id"])
            self.pending_item_by_id.pop(fin["id"], None)
            return  # da bi xoa truoc do -> bo qua finish muon
        item = self.pending_item_by_id.pop(fin["id"], None)
        if item is None:
            # Chua tung thay start (hiem, vd forward mode) -> them nhu row binh thuong
            self._append_row({k: v for k, v in fin.items() if not k.startswith("__")})
            return
        # Gop ket qua vao item dang cho
        for k in ("status", "duration_ms", "req_headers", "req_body",
                  "req_body_raw", "req_body_cut",
                  "res_headers", "res_body", "time", "method", "url"):
            if k in fin:
                item[k] = fin[k]
        item["_pending"] = False
        iid = self.pending_by_iid.pop(fin["id"], None)
        self._update_count()
        if iid is not None and self.tree.exists(iid):
            try:
                dur = f"{item['duration_ms']}" if item["duration_ms"] is not None else "-"
                self.tree.item(
                    iid,
                    values=(item["time"], item["method"], item["status"], dur, item["url"]),
                    tags=("error",) if self._is_error(item) else (),
                )
                if iid in self.tree.selection():
                    self._on_select()  # dang xem dong nay -> refresh chi tiet
                if self.follow_var.get():
                    self.tree.see(iid)
            except Exception:
                pass
        elif self._passes_filter(item):
            try:
                self._insert_row(item)
            except Exception:
                pass
            self._update_count()

    def _append_row(self, item: dict):
        self.all_items.append(item)
        # Chan list phong vo han -> an RAM, treo GUI sau 1 luc su dung
        if len(self.all_items) > MAX_ITEMS:
            overflow = len(self.all_items) - MAX_ITEMS
            doomed = {id(it) for it in self.all_items[:overflow]}
            # Bo map pending cua req cu bi xoa (finish muon cua no se bi bo qua)
            for pid in [k for k, it in self.pending_item_by_id.items() if id(it) in doomed]:
                self.pending_item_by_id.pop(pid, None)
                self.pending_by_iid.pop(pid, None)
                self._dropped_ids.add(pid)
            self._trim_dropped()
            del self.all_items[:overflow]
            # Dung lai bang theo du lieu con lai de iid/row khong lech
            try:
                self._apply_filter(silent=True)
            except Exception:
                pass
            self._update_count(extra=f"(da xoa {overflow} cu)")
            return
        if self._passes_filter(item):
            try:
                iid = self._insert_row(item)
                if item.get("_pending"):
                    self.pending_by_iid[item["id"]] = iid
            except Exception:
                pass
        self._update_count()

    def _update_count(self, extra: str = ""):
        pending = len(self.pending_item_by_id)
        count_txt = f"{len(self.all_items)} requests"
        if pending:
            count_txt += f"  •  {pending} in flight..."
        if extra:
            count_txt += f"  {extra}"
        self.status_var.set(count_txt)

    @staticmethod
    def _is_error(item: dict) -> bool:
        """Request loi: khong co response (ERROR/-) hoac HTTP status >= 400."""
        status = item.get("status")
        if isinstance(status, int):
            return status >= 400
        return status in ("ERROR", "-")

    def _insert_row(self, item: dict):
        iid = str(item["id"])
        # Phong thu: neu iid da ton tai (vi du log cu tu forward mode dung
        # counter rieng) thi tao iid duy nhat, khong de TclError lan ra ngoai.
        if iid in self.item_by_iid or self.tree.exists(iid):
            iid = f"{iid}-{len(self.all_items)}-{time.time_ns() % 1_000_000}"
            item["id"] = iid
        pending = bool(item.get("_pending"))
        status = "..." if pending else item.get("status")
        if pending or item.get("duration_ms") is None:
            dur = "-"
        else:
            dur = f"{item['duration_ms']}"
        if pending:
            tags: tuple = ("pending",)
        else:
            tags = ("error",) if self._is_error(item) else ()
        self.tree.insert(
            "", tk.END, iid=iid,
            values=(item["time"], item["method"], status, dur, item["url"]),
            tags=tags,
        )
        self.item_by_iid[iid] = item
        if self.follow_var.get():
            try:
                self.tree.see(iid)
            except Exception:
                pass
        return iid

    # ---------------- Follow (tu cuon theo req moi) ----------------
    def _on_follow_toggle(self):
        # Bat Follow -> nhay ngay xuong dong moi nhat
        if self.follow_var.get():
            children = self.tree.get_children()
            if children:
                try:
                    self.tree.see(children[-1])
                except Exception:
                    pass

    def _on_user_scroll(self, *args):
        """Scrollbar bi keo tay -> forward cho tree roi kiem tra vi tri."""
        try:
            self.tree.yview(*args)
        except Exception:
            pass
        self._pause_follow_if_not_at_bottom()

    def _on_wheel_or_key_scroll(self, _event=None):
        # De su kien cuon/phim chay xong roi moi kiem tra (sau 1 vong idle)
        self.root.after_idle(self._pause_follow_if_not_at_bottom)

    def _pause_follow_if_not_at_bottom(self):
        if not self.follow_var.get():
            return
        children = self.tree.get_children()
        if not children:
            return
        try:
            visible = self.tree.bbox(children[-1])
        except Exception:
            return
        if not visible:  # dong moi nhat khong con hien -> user dang xem req cu
            self.follow_var.set(False)

    # ---------------- Filter ----------------
    def _set_chip(self, name: str, refresh: bool = True):
        self.method_filter = "ALL" if name == "All" else ("ERRORS" if name == "Errors" else name)
        for n, btn in self.chip_buttons.items():
            try:
                btn.configure(style="Primary.TButton" if n == name else "TButton")
            except Exception:
                pass
        if refresh:
            self._apply_filter()

    def _on_search_focus_in(self, _event=None):
        if self._ph_shown:
            self._ph_shown = False
            self.filter_entry.delete(0, tk.END)
            try:
                self.filter_entry.configure(foreground="")
            except Exception:
                pass

    def _on_search_focus_out(self, _event=None):
        if not self.filter_var.get():
            self._ph_shown = True
            self.filter_entry.insert(0, self._ph_text)
            try:
                self.filter_entry.configure(foreground=self._muted_color())
            except Exception:
                pass

    @staticmethod
    def _muted_color_static(dark: bool) -> str:
        return "#a1a1aa" if dark else "#71717a"

    def _muted_color(self) -> str:
        return self._muted_color_static(self.dark_mode)

    def _passes_filter(self, item: dict) -> bool:
        mf = self.method_filter
        if mf == "ERRORS":
            if not self._is_error(item):
                return False
        elif mf != "ALL":
            if item.get("method") != mf:
                return False
        if self._ph_shown:
            return True
        needle = self.filter_var.get().strip().lower()
        if not needle:
            return True
        return needle in item["url"].lower() or needle in item.get("method", "").lower()

    def _apply_filter(self, silent: bool = False):
        if not hasattr(self, "tree"):
            return  # UI chua dung xong (trace cua search luc khoi tao)
        self.tree.delete(*self.tree.get_children())
        self.item_by_iid.clear()
        self.pending_by_iid.clear()
        for item in self.all_items:
            if self._passes_filter(item):
                try:
                    iid = self._insert_row(item)
                    if item.get("_pending"):
                        self.pending_by_iid[item["id"]] = iid
                except Exception:
                    if not silent:
                        raise
                    continue

    def _clear_all(self):
        # Ghi nho id dang cho de finish muon khong hien lai sau khi xoa
        self._dropped_ids.update(self.pending_item_by_id.keys())
        self._trim_dropped()
        self.all_items.clear()
        self.tree.delete(*self.tree.get_children())
        self.item_by_iid.clear()
        self.pending_by_iid.clear()
        self.pending_item_by_id.clear()
        self.req_headers_tree.delete(*self.req_headers_tree.get_children())
        self.res_headers_tree.delete(*self.res_headers_tree.get_children())
        for widget in (self.req_body_text, self.res_body_text):
            self._set_text(widget, "")
        self._update_count()

    def _trim_dropped(self, limit: int = 5000):
        while len(self._dropped_ids) > limit:
            self._dropped_ids.pop()

    # ---------------- Detail view ----------------
    def _on_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        item = self.item_by_iid.get(sel[0])
        if not item:
            return
        # Click vao req cu -> tat Follow de list dung yen cho xem;
        # click dung dong moi nhat -> bat Follow lai
        try:
            children = self.tree.get_children()
            self.follow_var.set(bool(children) and sel[0] == children[-1])
        except Exception:
            pass
        self._set_headers(self.req_headers_tree, item.get("req_headers") or {})
        self._set_text(self.req_body_text, item.get("req_body") or "(khong co body)")
        self._set_headers(self.res_headers_tree, item.get("res_headers") or {})
        res_body = item.get("res_body")
        if res_body:
            self._set_text(self.res_body_text, res_body)
        elif item.get("_pending"):
            self._set_text(self.res_body_text, "(dang cho response...)")
        else:
            self._set_text(self.res_body_text, "(khong co body)")

    def _selected_item(self) -> dict | None:
        sel = self.tree.selection()
        if not sel:
            return None
        return self.item_by_iid.get(sel[0])

    def _copy_curl(self):
        """Copy request dang chon sang lenh curl (bash), kieu Chrome DevTools."""
        item = self._selected_item()
        if not item:
            messagebox.showinfo("Copy as cURL", "Chon 1 request trong danh sach truoc.")
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(to_curl(item))
            self.root.update()  # giu clipboard sau khi dong app (Windows/X11)
        except Exception as e:
            messagebox.showerror("Copy as cURL", f"Khong copy duoc: {e}")
            return
        self.status_var.set(f"Da copy cURL: {item['method']} {item['url']}")

    def _on_right_click(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Copy as cURL (bash)", command=self._copy_curl)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    @staticmethod
    def _set_text(widget: tk.Text, content: str):
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.insert(tk.END, content)
        widget.configure(state=tk.DISABLED)


# --------------------------------------------------------------------------
# Man hinh cau hinh: nhap backend/port ngay tren GUI, khong can terminal
# --------------------------------------------------------------------------
def _apply_theme(root: tk.Tk):
    try:
        style = ttk.Style(master=root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
    except Exception:
        pass


def set_titlebar_dark(window: tk.Tk | tk.Toplevel, dark: bool) -> bool:
    """Doi mau title bar Windows theo theme app (den khi dark mode).
    Tra ve True neu ap dung duoc. Chi co tac dung tren Windows 10/11
    (can quyen goi DWM - Python Store ban sandbox co the bi chan)."""
    try:
        if sys.platform != "win32":
            return False
        import ctypes
        from ctypes import wintypes
        window.update_idletasks()
        user32 = ctypes.windll.user32
        # winfo_id() co the tra ve cua so con ben trong Tk -> lay top-level
        # that de DWM chap nhan (goi truc tiep hay bi E_HANDLE)
        hwnd = user32.GetParent(window.winfo_id()) or window.winfo_id()
        val = ctypes.c_int(1 if dark else 0)
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                              wintypes.LPVOID, wintypes.DWORD]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE (19 tren ban Win 10 cu)
        for attr in (20, 19):
            try:
                if dwm.DwmSetWindowAttribute(hwnd, attr,
                                             ctypes.byref(val),
                                             ctypes.sizeof(val)) == 0:
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _parse_setup(targets_raw: str, port_raw: str, insecure: bool):
    """Tra ve (None, config) neu hop le, hoac (thong bao loi, None)."""
    targets = [ln.strip() for ln in targets_raw.splitlines() if ln.strip()]
    if not targets:
        return "Can nhap it nhat 1 dia chi backend.", None
    bad = [t for t in targets if not t.startswith(("http://", "https://"))]
    if bad:
        return "URL phai bat dau bang http:// hoac https://:\n" + "\n".join(bad), None
    try:
        port = int(port_raw)
        if not (1 <= port <= 65535):
            raise ValueError
    except ValueError:
        return f"Port khong hop le: {port_raw!r}", None
    return None, {"port": port, "targets": targets, "insecure": bool(insecure)}


def run_setup_dialog(port: int, defaults: dict | None = None):
    """Cua so cau hinh dung chinh lam cua so chinh, tranh loi an cua so cua
    Toplevel+transient tren root da withdraw. Tra ve dict config hoac None.
    `defaults` (neu co) dung de dien san gia tri da luu lan truoc."""
    result = None
    init_targets = "\n".join(defaults["targets"]) + "\n" if defaults else "http://127.0.0.1:8081\n"
    init_port = str(defaults["port"]) if defaults else str(port)
    init_insecure = bool(defaults["insecure"]) if defaults else False

    root = tk.Tk()
    root.title("Cau hinh Network Monitor")
    root.resizable(False, False)
    _apply_theme(root)

    def _start():
        nonlocal result
        err, cfg = _parse_setup(
            targets_txt.get("1.0", "end"), port_var.get(), bool(insecure_var.get())
        )
        if err:
            messagebox.showerror("Loi", err, parent=root)
            return
        result = cfg
        root.destroy()

    frm = ttk.Frame(root, padding=14)
    frm.pack(fill=tk.BOTH, expand=True)

    ttk.Label(frm, text="Backend can theo doi (moi dong 1 URL):").grid(
        row=0, column=0, columnspan=3, sticky="w"
    )
    targets_txt = tk.Text(frm, width=54, height=4, font=("Consolas", 10))
    targets_txt.grid(row=1, column=0, columnspan=3, sticky="we", pady=(2, 2))
    targets_txt.insert("1.0", init_targets)

    ttk.Label(
        frm,
        text="Target thu 2 tro di tu dong dung port ke tiep (+1, +2, ...)",
        foreground="gray",
    ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 8))

    ttk.Label(frm, text="Port lang nghe dau tien:").grid(row=3, column=0, sticky="w")
    port_var = tk.StringVar(value=init_port)
    spin = ttk.Spinbox(frm, from_=1, to=65535, textvariable=port_var, width=8)
    spin.grid(row=3, column=1, sticky="w", padx=(4, 0))
    spin.select_range(0, "end")
    spin.focus_set()

    insecure_var = tk.BooleanVar(value=init_insecure)
    ttk.Checkbutton(
        frm,
        text="Khong kiem tra chung chi TLS (backend https cert tu ky)",
        variable=insecure_var,
    ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 12))

    btns = ttk.Frame(frm)
    btns.grid(row=5, column=0, columnspan=3, sticky="e")
    ttk.Button(btns, text="Thoat", command=root.destroy).pack(side=tk.RIGHT, padx=(8, 0))
    ttk.Button(btns, text="Bat dau", command=_start).pack(side=tk.RIGHT)

    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.bind("<Return>", lambda _e: _start())
    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
    try:
        root.lift()
        root.focus_force()
    except Exception:
        pass
    root.mainloop()
    return result


def main():
    parser = argparse.ArgumentParser(description="Network Monitor cho ung dung Tauri (dev)")
    parser.add_argument(
        "--mode", choices=["reverse", "forward"], default="reverse",
        help="reverse: frontend goi thang toi tool, tool forward toi backend that (khuyen nghi). "
             "forward: proxy truyen thong, can cau hinh HTTP_PROXY/HTTPS_PROXY.",
    )
    parser.add_argument(
        "--port", type=int, default=9000,
        help="Port dau tien de tool lang nghe (target thu 2 tro di dung port+1, port+2, ...)",
    )
    parser.add_argument(
        "--target", action="append", metavar="URL",
        help="[reverse mode] Dia chi backend that, vi du http://127.0.0.1:8081. "
             "Lap lai tuy y de theo doi nhieu backend cung luc, moi target 1 port rieng.",
    )
    parser.add_argument(
        "--insecure", action="store_true",
        help="[reverse mode] Bo qua kiem tra chung chi TLS khi target la https (backend dung cert tu ky)",
    )
    args = parser.parse_args()

    insecure = args.insecure
    dark_mode = False
    if args.mode == "reverse":
        if args.target:
            prev = load_saved_config()  # giu lai dark_mode da luu (neu co)
            cfg = {"port": args.port, "targets": args.target, "insecure": insecure,
                   "dark_mode": bool(prev.get("dark_mode", False)) if prev else False}
            save_config(cfg)  # nho lan chay bang CLI de lan sau double-click khoi nhap
            listeners = [(args.port + i, t) for i, t in enumerate(args.target)]
            dark_mode = cfg["dark_mode"]
        else:
            saved = load_saved_config()
            if saved is not None:
                # Co cau hinh cu -> chay thang, khoi hoi lai
                cfg = saved
            else:
                cfg = run_setup_dialog(args.port)
                if cfg is None:
                    return
                save_config(cfg)
            listeners = [(cfg["port"] + i, t) for i, t in enumerate(cfg["targets"])]
            insecure = cfg["insecure"]
            dark_mode = bool(cfg.get("dark_mode", False))
    else:
        listeners = [(args.port, None)]
        pref = load_saved_config()  # forward mode chi lay dark_mode
        dark_mode = bool(pref.get("dark_mode", False)) if pref else False

    root = tk.Tk()
    _apply_theme(root)
    NetworkMonitorApp(root, args.mode, listeners, insecure, dark_mode=dark_mode)
    root.mainloop()


if __name__ == "__main__":
    main()
