#!/usr/bin/env python3
"""
Antigravity Desktop Always-On-Top Mini Quota Widget.

Connects to the local `quota-monitor` Sidecar HTTP API (`GET /api/quota`)
with zero extra LLM tokens. Supports docking snugly into the bottom-left
sidebar area of the Antigravity desktop window or floating freely anywhere
on screen, with click-to-expand complete model group quotas.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
try:
    import winreg
except ImportError:
    winreg = None

from PyQt5.QtCore import (
    QObject,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt5.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PyQt5.QtWidgets import (
    QAction,
    QActionGroup,
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

# ---------------------------------------------------------------------------
# Paths & Constants
# ---------------------------------------------------------------------------
APP_DATA = Path.home() / ".gemini" / "antigravity"
PLUGIN_DIR_NAME = Path(__file__).resolve().parent.parent.name
SIDECAR_BASE = APP_DATA / "sidecar_data" / PLUGIN_DIR_NAME / "quota_panel"
RUNTIME_FILE = SIDECAR_BASE / "data" / "runtime.json"
SIDECAR_TOKEN_FILE = SIDECAR_BASE / "data" / "sidecar_token.json"
SIDECAR_LOG = SIDECAR_BASE / "logs" / "sidecar.log"
PREFS_FILE = SIDECAR_BASE / "data" / "widget_prefs.json"
WIDGET_LOG = SIDECAR_BASE / "data" / "widget.log"

MUTEX_NAME = "Local\\AntigravityQuotaFloatingWidget_v1"
ID_RE = re.compile(r"^[0-9a-f-]{8,}$", re.I)
LOG_PORT_RE = re.compile(r"\[sidecar\]\s+listening\s+on\s+127\.0\.0\.1:(\d+)")
TIER_SUFFIX_RE = re.compile(r"\s*\((Low|Medium|High|XHigh|Extra High|Minimal)\)\s*$", re.I)

SHADOW_MARGIN = 10
MINI_VIS_WIDTH = 140
MINI_VIS_HEIGHT = 42
POPOVER_VIS_WIDTH = 256

POLL_OK_MS = 30_000
POLL_RETRY_MS = 5_000
POLL_INIT_RETRY_MS = 2_000
TRACK_MS = 60
TICK_MS = 30_000
LIFECYCLE_MS = 1_000
APP_GONE_GRACE_SEC = 3.0

DEFAULT_PREFS = {
    "mode": "used",             # "used" | "remaining"
    "dock": "docked",           # "docked" | "float" | "taskbar"
    "theme": "system",          # "system" | "light" | "dark"
    "float_pos": None,          # [x, y] or None
    "taskbar_x": None,          # int or None (logical x inside taskbar)
    "pinned_pods": ["antigravity", "codex"],
    "auto_exit_with_app": True, # exit when Antigravity closes
}

PALETTES = {
    "light": {
        "card_bg": "#ffffff",
        "card_alt": "#f3f4f6",
        "fg": "#18181b",
        "muted": "#6b7280",
        "border": "#e5e7eb",
        "track": "#e5e7eb",
        "accent": "#2563eb",
        "accent_soft": "rgba(37, 99, 235, 0.12)",
        "accent_fg": "#ffffff",
        "green": "#16a34a",
        "amber": "#d97706",
        "red": "#dc2626",
        "hover": "rgba(24, 24, 27, 0.06)",
        "shadow_docked": QColor(15, 23, 42, 42),
        "shadow_float": QColor(15, 23, 42, 82),
    },
    "dark": {
        "card_bg": "#13151b",
        "card_alt": "#1b1f29",
        "fg": "#f1f5f9",
        "muted": "#94a3b8",
        "border": "#292e3b",
        "track": "#232836",
        "accent": "#60a5fa",
        "accent_soft": "rgba(96, 165, 250, 0.18)",
        "accent_fg": "#ffffff",
        "green": "#34d399",
        "amber": "#f59e0b",
        "red": "#ef4444",
        "hover": "rgba(255, 255, 255, 0.08)",
        "shadow_docked": QColor(0, 0, 0, 125),
        "shadow_float": QColor(0, 0, 0, 185),
    },
}


# ---------------------------------------------------------------------------
# Preferences & System Theme Helpers
# ---------------------------------------------------------------------------
def load_prefs() -> dict:
    prefs = dict(DEFAULT_PREFS)
    try:
        if PREFS_FILE.exists():
            raw = json.loads(PREFS_FILE.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                if raw.get("mode") in ("used", "remaining"):
                    prefs["mode"] = raw["mode"]
                if raw.get("dock") in ("docked", "float", "taskbar"):
                    prefs["dock"] = raw["dock"]
                if raw.get("theme") in ("system", "light", "dark"):
                    prefs["theme"] = raw["theme"]
                fp = raw.get("float_pos")
                if isinstance(fp, list) and len(fp) == 2 and all(isinstance(v, (int, float)) for v in fp):
                    prefs["float_pos"] = [int(fp[0]), int(fp[1])]
                tx = raw.get("taskbar_x")
                if isinstance(tx, (int, float)):
                    prefs["taskbar_x"] = int(tx)
                pp = raw.get("pinned_pods")
                if isinstance(pp, list):
                    valid = [p for p in pp if p in ("antigravity", "codex")]
                    if valid:
                        prefs["pinned_pods"] = valid
                if isinstance(raw.get("auto_exit_with_app"), bool):
                    prefs["auto_exit_with_app"] = raw["auto_exit_with_app"]
    except Exception:
        pass
    return prefs


def log_event(msg: str) -> None:
    try:
        WIDGET_LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = []
        if WIDGET_LOG.exists():
            lines = WIDGET_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        lines.append(f"{ts} {msg}")
        if len(lines) > 80:
            lines = lines[-80:]
        WIDGET_LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


def save_prefs(prefs: dict) -> None:
    try:
        PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
        PREFS_FILE.write_text(json.dumps(prefs, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def detect_windows_theme() -> str:
    if winreg is None:
        return "light"
    try:
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return "light" if int(val) == 1 else "dark"
    except Exception:
        return "light"


def resolve_theme(theme_pref: str) -> str:
    if theme_pref in ("light", "dark"):
        return theme_pref
    return detect_windows_theme()


def acquire_single_instance_mutex():
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.GetLastError.argtypes = []
        kernel32.GetLastError.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            if handle:
                kernel32.CloseHandle(handle)
            return None
        return handle
    except Exception:
        return True  # Proceed if mutex API unexpectedly unavailable


# ---------------------------------------------------------------------------
# Formatting Helpers
# ---------------------------------------------------------------------------
def fmt_pct(value) -> str:
    if not isinstance(value, (int, float)):
        return "--"
    if abs(value - round(value)) < 0.05:
        return f"{int(round(value))}%"
    return f"{value:.1f}%"


def fmt_short_countdown(reset_ms) -> str:
    if not isinstance(reset_ms, (int, float)):
        return ""
    diff_ms = reset_ms - time.time() * 1000.0
    if diff_ms <= 0:
        return "即将重置"
    total_min = int(diff_ms // 60_000)
    if total_min < 1:
        return "<1m"
    days = total_min // 1440
    hours = (total_min % 1440) // 60
    mins = total_min % 60
    if days > 0:
        return f"{days}d{hours}h"
    if hours > 0:
        return f"{hours}h{mins}m"
    return f"{mins}m"


def fmt_full_reset_time(reset_ms) -> str:
    if not isinstance(reset_ms, (int, float)):
        return ""
    try:
        dt = datetime.fromtimestamp(reset_ms / 1000.0)
        return f"预计重置时间: {dt.strftime('%Y-%m-%d %H:%M')}"
    except Exception:
        return ""


def fmt_relative_updated(fetched_at) -> str:
    if not isinstance(fetched_at, (int, float)):
        return "尚未更新"
    sec = max(0, int((time.time() * 1000.0 - fetched_at) // 1000))
    if sec < 10:
        return "刚刚更新"
    if sec < 60:
        return f"{sec}秒前更新"
    mins = sec // 60
    if mins < 60:
        return f"{mins}分钟前更新"
    return datetime.fromtimestamp(fetched_at / 1000.0).strftime("%H:%M 更新")


def split_model_short_and_tier(label: str) -> tuple[str, str]:
    text = (label or "").strip() or "未知模型"
    m = TIER_SUFFIX_RE.search(text)
    if not m:
        return text, ""
    return text[: m.start()].strip(), m.group(1)


def short_family_chip_name(family_name: str) -> str:
    name = (family_name or "").strip()
    for prefix in ("Gemini 4 ", "Gemini 3.8 ", "Gemini ", "Claude "):
        if name.startswith(prefix) and len(name) > len(prefix):
            return name[len(prefix) :]
    return name


def tone_color_key(used_pct: float) -> str:
    if used_pct >= 85.0:
        return "red"
    if used_pct >= 60.0:
        return "amber"
    return "green"


# ---------------------------------------------------------------------------
# Sidecar Zero-Token HTTP Client
# ---------------------------------------------------------------------------
class SidecarQuotaClient(QObject):
    quota_fetched = pyqtSignal(object, object)  # (payload_dict | None, error_str | None)
    accounts_fetched = pyqtSignal(object, object)  # (accounts_dict | None, error_str | None)
    account_action_done = pyqtSignal(str, bool, object, object)  # (action, ok, payload, err)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._conv_cache = (0.0, "")
        self._lock = threading.Lock()
        self._fetching = False
        self._fetching_accounts = False
        self._no_proxy_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @staticmethod
    def _read_token_file(path: Path) -> str:
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                tok = data.get("token")
                if isinstance(tok, str) and tok:
                    return tok
        except Exception:
            pass
        return ""

    def _discover_endpoints(self) -> list[tuple[int, str]]:
        endpoints: list[tuple[int, str]] = []
        seen_ports: set[int] = set()
        fallback_tok = self._read_sidecar_token()

        def add_candidate(port_val, tok_val: str = "") -> None:
            if isinstance(port_val, int) and 0 < port_val < 65536 and port_val not in seen_ports:
                seen_ports.add(port_val)
                endpoints.append((port_val, tok_val or fallback_tok))

        # 1. Primary: runtime.json + sibling sidecar_token.json written by main.mjs
        primary_tok = self._read_token_file(SIDECAR_TOKEN_FILE) or fallback_tok
        try:
            if RUNTIME_FILE.exists():
                rt = json.loads(RUNTIME_FILE.read_text(encoding="utf-8"))
                add_candidate(rt.get("port"), primary_tok)
        except Exception:
            pass

        # 2. Fallback: scan sidecar.log from the bottom
        try:
            if SIDECAR_LOG.exists():
                lines = SIDECAR_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
                for line in reversed(lines):
                    m = LOG_PORT_RE.search(line)
                    if m:
                        add_candidate(int(m.group(1)), primary_tok)
                        break
        except Exception:
            pass

        # 3. Fallback: scan any installed plugin directory name under sidecar_data/*/quota_panel
        sidecar_root = APP_DATA / "sidecar_data"
        try:
            for rt_file in sidecar_root.glob("*/quota_panel/data/runtime.json"):
                try:
                    rt = json.loads(rt_file.read_text(encoding="utf-8"))
                    sib_tok = self._read_token_file(rt_file.parent / "sidecar_token.json")
                    add_candidate(rt.get("port"), sib_tok)
                except Exception:
                    pass
            if not endpoints:
                for log_file in sidecar_root.glob("*/quota_panel/logs/sidecar.log"):
                    try:
                        sib_tok = self._read_token_file(log_file.parent.parent / "data" / "sidecar_token.json")
                        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
                        for line in reversed(lines):
                            m = LOG_PORT_RE.search(line)
                            if m:
                                add_candidate(int(m.group(1)), sib_tok)
                                break
                    except Exception:
                        pass
        except Exception:
            pass

        return endpoints

    def discover_ports(self) -> list[int]:
        return [p for p, _ in self._discover_endpoints()]

    def _read_sidecar_token(self) -> str:
        candidates = [SIDECAR_TOKEN_FILE]
        sidecar_root = APP_DATA / "sidecar_data"
        try:
            for tok_file in sidecar_root.glob("*/quota_panel/data/sidecar_token.json"):
                if tok_file not in candidates:
                    candidates.append(tok_file)
        except Exception:
            pass

        for path in candidates:
            tok = self._read_token_file(path)
            if tok:
                return tok
        return ""

    def find_latest_conversation_id(self) -> str:
        now = time.monotonic()
        cached_at, cached_id = self._conv_cache
        if now - cached_at < 3.0 and cached_id:
            return cached_id

        brain_dir = APP_DATA / "brain"
        best_id = ""
        best_mtime = -1.0
        try:
            if brain_dir.is_dir():
                for entry in brain_dir.iterdir():
                    if not entry.is_dir() or not ID_RE.match(entry.name):
                        continue
                    tr = entry / ".system_generated" / "logs" / "transcript.jsonl"
                    try:
                        st = tr.stat()
                        if st.st_mtime > best_mtime:
                            best_mtime = st.st_mtime
                            best_id = entry.name
                    except OSError:
                        continue
        except Exception:
            pass

        self._conv_cache = (now, best_id)
        return best_id

    def _request_json(
        self,
        path: str,
        method: str = "GET",
        body: dict | None = None,
        timeout: float = 6.0,
    ) -> tuple[dict | None, str | None, int | None]:
        endpoints = self._discover_endpoints()
        if not endpoints:
            return None, "未检测到 Sidecar 端口，请确认 Antigravity 已启动", None

        is_write = method.upper() != "GET"
        data_bytes = json.dumps(body or {}).encode("utf-8") if is_write else None

        last_err = None
        last_port = None
        total = len(endpoints)
        for idx, (port, port_token) in enumerate(endpoints):
            url = f"http://127.0.0.1:{port}{path}"
            headers = {}
            if is_write:
                headers["Content-Type"] = "application/json"
                tok = port_token or self._read_sidecar_token()
                if tok:
                    headers["X-Sidecar-Token"] = tok
            try:
                req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method.upper())
                with self._no_proxy_opener.open(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    payload = json.loads(raw) if raw else {}
                    return payload, payload.get("error"), port
            except urllib.error.HTTPError as exc:
                try:
                    err_body = json.loads(exc.read().decode("utf-8"))
                    err_msg = err_body.get("error") or f"HTTP {exc.code}"
                except Exception:
                    err_msg = f"HTTP {exc.code}"
                if exc.code in (401, 404, 405, 502, 503) and idx + 1 < total:
                    last_err = err_msg
                    last_port = port
                    continue
                return None, err_msg, port
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                last_err = str(exc)
                last_port = port

        return None, last_err or "无法连接到本地 Sidecar 服务", last_port

    def fetch_quota_sync(self, force: bool = False) -> tuple[dict | None, str | None, int | None]:
        conv_id = self.find_latest_conversation_id()
        qs = {}
        if conv_id:
            qs["conversationId"] = conv_id
        if force:
            qs["force"] = "1"
        query = urllib.parse.urlencode(qs)
        path = f"/api/quota?{query}" if query else "/api/quota"
        return self._request_json(path, method="GET", timeout=5.0)

    def fetch_accounts_sync(self) -> tuple[dict | None, str | None, int | None]:
        conv_id = self.find_latest_conversation_id()
        path = f"/api/accounts?conversationId={urllib.parse.quote(conv_id)}" if conv_id else "/api/accounts"
        return self._request_json(path, method="GET", timeout=5.0)

    def request_fetch(self, force: bool = False) -> None:
        with self._lock:
            if self._fetching and not force:
                return
            self._fetching = True

        def worker():
            try:
                payload, err, _ = self.fetch_quota_sync(force=force)
            finally:
                with self._lock:
                    self._fetching = False
            self.quota_fetched.emit(payload, err)

        threading.Thread(target=worker, daemon=True).start()

    def request_fetch_accounts(self) -> None:
        with self._lock:
            if self._fetching_accounts:
                return
            self._fetching_accounts = True

        def worker():
            try:
                payload, err, _ = self.fetch_accounts_sync()
            finally:
                with self._lock:
                    self._fetching_accounts = False
            self.accounts_fetched.emit(payload, err)

        threading.Thread(target=worker, daemon=True).start()

    def request_account_action(self, action_name: str, path: str, body: dict | None = None) -> None:
        conv_id = self.find_latest_conversation_id()
        payload_body = dict(body or {})
        if conv_id and "conversationId" not in payload_body:
            payload_body["conversationId"] = conv_id
        log_event(f"account_action_start action={action_name}")

        def worker():
            payload, err, port = self._request_json(path, method="POST", body=payload_body, timeout=12.0)
            ok = payload is not None and not err and payload.get("ok") is not False
            safe_err = (err or "").replace("\r", " ").replace("\n", " ")[:120]
            log_event(f"account_action_done action={action_name} ok={ok} port={port} err={safe_err or 'none'}")
            self.account_action_done.emit(action_name, ok, payload, err)

        threading.Thread(target=worker, daemon=True).start()

    def request_capture_account(self) -> None:
        self.request_account_action("capture", "/api/accounts/capture")

    def request_import_agy_switch(self) -> None:
        self.request_account_action("import_agy", "/api/accounts/import-agy-switch")

    def request_switch_account(self, account_id: str) -> None:
        self.request_account_action("switch", "/api/accounts/switch", {"id": account_id})


# ---------------------------------------------------------------------------
# Win32 Window & HiDPI Coordinate Tracker
# ---------------------------------------------------------------------------
class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class Win32WindowTracker:
    DWMWA_EXTENDED_FRAME_BOUNDS = 9
    DWMWA_CLOAKED = 14
    MONITOR_DEFAULTTONEAREST = 2
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    GWL_EXSTYLE = -20
    WS_EX_TOPMOST = 0x00000008
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_LAYERED = 0x00080000
    HWND_TOP = 0
    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOACTIVATE = 0x0010

    def __init__(self):
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32
        self.dwmapi = getattr(ctypes.windll, "dwmapi", None)
        self.own_pid = os.getpid()

        self.WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        self.user32.EnumWindows.argtypes = [self.WNDENUMPROC, wintypes.LPARAM]
        self.user32.EnumWindows.restype = wintypes.BOOL
        self.user32.IsWindow.argtypes = [wintypes.HWND]
        self.user32.IsWindow.restype = wintypes.BOOL
        self.user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user32.IsWindowVisible.restype = wintypes.BOOL
        self.user32.IsIconic.argtypes = [wintypes.HWND]
        self.user32.IsIconic.restype = wintypes.BOOL
        self.user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.GetWindowLongW.restype = ctypes.c_long
        self.user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        self.user32.SetWindowPos.restype = wintypes.BOOL
        self.user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self.user32.GetWindowTextLengthW.restype = ctypes.c_int
        self.user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.GetWindowTextW.restype = ctypes.c_int
        self.user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.GetClassNameW.restype = ctypes.c_int
        self.user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user32.GetWindowRect.restype = wintypes.BOOL
        self.user32.GetForegroundWindow.argtypes = []
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        self.user32.MonitorFromWindow.restype = wintypes.HMONITOR
        self.user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW)]
        self.user32.GetMonitorInfoW.restype = wintypes.BOOL

        self.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        self.kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

        if self.dwmapi is not None:
            self.dwmapi.DwmGetWindowAttribute.argtypes = [
                wintypes.HWND,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
            ]
            self.dwmapi.DwmGetWindowAttribute.restype = ctypes.HRESULT

    def get_pid(self, hwnd: int) -> int:
        if not hwnd:
            return 0
        pid = wintypes.DWORD(0)
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def get_process_image_path(self, pid: int) -> str:
        if not pid:
            return ""
        handle = self.kernel32.OpenProcess(self.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(1024)
            if self.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value.lower()
            return ""
        finally:
            self.kernel32.CloseHandle(handle)

    def get_process_exe_name(self, pid: int) -> str:
        img = self.get_process_image_path(pid)
        return Path(img).stem.lower() if img else ""

    def get_window_title(self, hwnd: int) -> str:
        length = self.user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        self.user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value

    def get_class_name(self, hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(128)
        self.user32.GetClassNameW(hwnd, buf, 128)
        return buf.value

    def is_iconic(self, hwnd: int) -> bool:
        return bool(hwnd and self.user32.IsIconic(hwnd))

    def is_process_alive(self, pid: int) -> bool:
        if not pid or pid <= 0:
            return False
        handle = self.kernel32.OpenProcess(self.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD(0)
            if not self.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return int(code.value) == 259  # STILL_ACTIVE
        finally:
            self.kernel32.CloseHandle(handle)

    def count_visible_titled_windows(self) -> int:
        cnt = [0]

        def enum_cb(hwnd, _lparam):
            if self.user32.IsWindowVisible(hwnd):
                if self.get_window_title(hwnd).strip():
                    cnt[0] += 1
            return True

        self.user32.EnumWindows(self.WNDENUMPROC(enum_cb), 0)
        return cnt[0]

    def get_foreground_hwnd(self) -> int:
        return int(self.user32.GetForegroundWindow() or 0)

    def get_window_rect(self, hwnd: int) -> tuple[int, int, int, int] | None:
        if not hwnd:
            return None
        rect = wintypes.RECT()
        ok = False
        if self.dwmapi is not None:
            hr = self.dwmapi.DwmGetWindowAttribute(
                hwnd,
                self.DWMWA_EXTENDED_FRAME_BOUNDS,
                ctypes.byref(rect),
                ctypes.sizeof(rect),
            )
            ok = hr == 0
        if not ok:
            ok = bool(self.user32.GetWindowRect(hwnd, ctypes.byref(rect)))
        if not ok:
            return None
        return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)

    def find_antigravity_hwnd(self) -> int:
        primary_matches: list[int] = []
        fallback_matches: list[int] = []

        def enum_cb(hwnd, _lparam):
            if not self.user32.IsWindowVisible(hwnd):
                return True
            pid = self.get_pid(hwnd)
            if pid == self.own_pid:
                return True

            iconic = self.is_iconic(hwnd)
            if not iconic:
                rect = self.get_window_rect(hwnd)
                if not rect or (rect[2] - rect[0]) < 600 or (rect[3] - rect[1]) < 400:
                    return True

            cls = self.get_class_name(hwnd)
            exe = self.get_process_exe_name(pid)
            title = self.get_window_title(hwnd)

            if cls == "Chrome_WidgetWin_1" and ("antigravity" in exe or "agy" in exe):
                primary_matches.append(int(hwnd))
            elif "antigravity" in title.lower():
                fallback_matches.append(int(hwnd))
            return True

        self.user32.EnumWindows(self.WNDENUMPROC(enum_cb), 0)
        if primary_matches:
            return primary_matches[0]
        if fallback_matches:
            return fallback_matches[0]
        return 0

    def find_codex_hwnd(self) -> int:
        primary_matches: list[int] = []
        fallback_matches: list[int] = []

        def enum_cb(hwnd, _lparam):
            if not self.user32.IsWindowVisible(hwnd):
                return True
            pid = self.get_pid(hwnd)
            if pid == self.own_pid:
                return True

            iconic = self.is_iconic(hwnd)
            if not iconic:
                rect = self.get_window_rect(hwnd)
                if not rect or (rect[2] - rect[0]) < 320 or (rect[3] - rect[1]) < 240:
                    return True

            img_path = self.get_process_image_path(pid)
            exe = Path(img_path).stem.lower() if img_path else ""
            title = self.get_window_title(hwnd)
            if not title:
                return True

            if "openai.codex" in img_path or exe == "codex":
                primary_matches.append(int(hwnd))
            elif exe == "chatgpt" and "codex" in title.lower():
                fallback_matches.append(int(hwnd))
            return True

        self.user32.EnumWindows(self.WNDENUMPROC(enum_cb), 0)
        if primary_matches:
            return primary_matches[0]
        if fallback_matches:
            return fallback_matches[0]
        return 0

    def focus_window(self, hwnd: int) -> bool:
        if not hwnd:
            return False
        try:
            SW_RESTORE = 9
            SW_SHOW = 5
            if self.is_iconic(hwnd):
                self.user32.ShowWindow(hwnd, SW_RESTORE)
            else:
                self.user32.ShowWindow(hwnd, SW_SHOW)

            fg_hwnd = self.get_foreground_hwnd()
            cur_tid = self.kernel32.GetCurrentThreadId()
            fg_tid = self.user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
            tgt_tid = self.user32.GetWindowThreadProcessId(hwnd, None)

            attached_fg = False
            attached_tgt = False
            try:
                if fg_tid and fg_tid != cur_tid:
                    attached_fg = bool(self.user32.AttachThreadInput(cur_tid, fg_tid, True))
                if tgt_tid and tgt_tid != cur_tid and tgt_tid != fg_tid:
                    attached_tgt = bool(self.user32.AttachThreadInput(cur_tid, tgt_tid, True))
                self.user32.BringWindowToTop(hwnd)
                self.user32.SetForegroundWindow(hwnd)
            finally:
                if attached_tgt:
                    self.user32.AttachThreadInput(cur_tid, tgt_tid, False)
                if attached_fg:
                    self.user32.AttachThreadInput(cur_tid, fg_tid, False)
            return True
        except Exception:
            return False

    def physical_to_logical(self, hwnd: int, phys_x: int, phys_y: int):
        screens = QApplication.screens()
        default_screen = QApplication.primaryScreen() or (screens[0] if screens else None)

        hmon = self.user32.MonitorFromWindow(hwnd, self.MONITOR_DEFAULTTONEAREST)
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if hmon and self.user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            dev_name = mi.szDevice
            matched_screen = None
            for scr in screens:
                if scr.name() == dev_name:
                    matched_screen = scr
                    break
            screen = matched_screen or default_screen
            dpr = screen.devicePixelRatio() if screen else 1.0
            if dpr <= 0:
                dpr = 1.0
            geo = screen.geometry() if screen else QRect(0, 0, 1920, 1080)
            lx = geo.left() + round((phys_x - mi.rcMonitor.left) / dpr)
            ly = geo.top() + round((phys_y - mi.rcMonitor.top) / dpr)
            return lx, ly, screen, dpr, (mi.rcMonitor.left, mi.rcMonitor.top)

        dpr = default_screen.devicePixelRatio() if default_screen else 1.0
        if dpr <= 0:
            dpr = 1.0
        return round(phys_x / dpr), round(phys_y / dpr), default_screen, dpr, (0, 0)

    def is_valid_hwnd(self, hwnd: int) -> bool:
        return bool(hwnd and self.user32.IsWindow(hwnd))

    def is_window_topmost(self, hwnd: int) -> bool:
        if not self.is_valid_hwnd(hwnd):
            return False
        return bool(self.user32.GetWindowLongW(hwnd, self.GWL_EXSTYLE) & self.WS_EX_TOPMOST)

    def is_window_cloaked(self, hwnd: int) -> bool:
        if not hwnd or self.dwmapi is None:
            return False
        cloaked = wintypes.DWORD(0)
        hr = self.dwmapi.DwmGetWindowAttribute(
            hwnd,
            self.DWMWA_CLOAKED,
            ctypes.byref(cloaked),
            ctypes.sizeof(cloaked),
        )
        return hr == 0 and int(cloaked.value) != 0

    def set_topmost(self, hwnd: int, topmost: bool) -> None:
        if not self.is_valid_hwnd(hwnd):
            return
        flags = self.SWP_NOMOVE | self.SWP_NOSIZE | self.SWP_NOACTIVATE
        if topmost:
            self.user32.SetWindowPos(hwnd, self.HWND_TOPMOST, 0, 0, 0, 0, flags)
        elif self.is_window_topmost(hwnd):
            self.user32.SetWindowPos(hwnd, self.HWND_NOTOPMOST, 0, 0, 0, 0, flags)

    @staticmethod
    def _rects_intersect(
        a: tuple[int, int, int, int],
        b: tuple[int, int, int, int],
    ) -> bool:
        return not (a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3])

    def sync_docked_zorder(
        self,
        mini_hwnd: int,
        pop_hwnd: int,
        pop_visible: bool,
        agy_hwnd: int,
        card_phys_rect: tuple[int, int, int, int] | None = None,
        raise_for_popover: bool = False,
        ignore_own_pid: bool = True,
    ) -> bool:
        """
        Keeps `mini_hwnd` (and `pop_hwnd` when visible) glued above `agy_hwnd`:
        - When NO other normal window is in front of `Antigravity` (or the user is
          actively interacting with the expanded popover), promotes our widget to
          `HWND_TOPMOST` so Windows Foreground Lock never lets `Antigravity` cover it.
        - As soon as any other non-minimized, uncloaked window (e.g. QQ, Clash, WeChat)
          is stacked above `Antigravity`, clears `HWND_TOPMOST` and inserts our widget
          directly below that window and above `agy_hwnd`.
        """
        if not self.is_valid_hwnd(mini_hwnd) or not self.is_valid_hwnd(agy_hwnd):
            return True

        flags = self.SWP_NOMOVE | self.SWP_NOSIZE | self.SWP_NOACTIVATE
        agy_topmost = self.is_window_topmost(agy_hwnd)
        agy_pid = self.get_pid(agy_hwnd) if ignore_own_pid else 0
        has_pop = bool(pop_visible and self.is_valid_hwnd(pop_hwnd))

        seq = [0]
        mini_idx: list[int | None] = [None]
        pop_idx: list[int | None] = [None]
        agy_idx: list[int | None] = [None]
        above_others: list[tuple[int, int, tuple[int, int, int, int]]] = []

        def enum_cb(w, _lparam):
            wh = int(w)
            if wh == mini_hwnd:
                mini_idx[0] = seq[0]
                seq[0] += 1
                return True
            if has_pop and wh == pop_hwnd:
                pop_idx[0] = seq[0]
                seq[0] += 1
                return True
            if wh == agy_hwnd:
                agy_idx[0] = seq[0]
                return False

            if not self.user32.IsWindowVisible(wh) or self.is_iconic(wh):
                return True
            w_pid = self.get_pid(wh)
            if ignore_own_pid and w_pid == self.own_pid:
                return True

            ex_style = self.user32.GetWindowLongW(wh, self.GWL_EXSTYLE)
            w_topmost = bool(ex_style & self.WS_EX_TOPMOST)
            if not agy_topmost and w_topmost:
                return True
            if (ex_style & self.WS_EX_LAYERED) and (ex_style & self.WS_EX_TRANSPARENT):
                return True

            cls = self.get_class_name(wh)
            if cls in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
                return True
            if self.is_window_cloaked(wh):
                return True

            rect = self.get_window_rect(wh)
            if not rect or (rect[2] - rect[0]) < 24 or (rect[3] - rect[1]) < 24:
                return True

            if agy_pid and w_pid == agy_pid:
                if not card_phys_rect or not self._rects_intersect(rect, card_phys_rect):
                    return True

            above_others.append((seq[0], wh, rect))
            seq[0] += 1
            return True

        self.user32.EnumWindows(self.WNDENUMPROC(enum_cb), 0)
        if agy_idx[0] is None:
            return True

        m_i = mini_idx[0]
        p_i = pop_idx[0]
        a_i = agy_idx[0]

        # When Antigravity is at the front (no other normal window above it), or user is
        # actively interacting with the open popover, use HWND_TOPMOST to bypass Win32's
        # cross-process foreground Z-order lock so Antigravity itself never covers us.
        if not above_others or raise_for_popover:
            mini_top = self.is_window_topmost(mini_hwnd)
            if has_pop:
                pop_top = self.is_window_topmost(pop_hwnd)
                order_ok = p_i is not None and m_i is not None and p_i < m_i < a_i
                if not (mini_top and pop_top and order_ok):
                    self.user32.SetWindowPos(mini_hwnd, self.HWND_TOPMOST, 0, 0, 0, 0, flags)
                    self.user32.SetWindowPos(pop_hwnd, self.HWND_TOPMOST, 0, 0, 0, 0, flags)
            else:
                order_ok = m_i is not None and m_i < a_i
                if not (mini_top and order_ok):
                    self.user32.SetWindowPos(mini_hwnd, self.HWND_TOPMOST, 0, 0, 0, 0, flags)
            return True

        # One or more normal windows (QQ, Clash, WeChat, etc.) are above Antigravity:
        # drop HWND_TOPMOST (unless Antigravity itself is topmost) and sandwich below them.
        mini_top = self.is_window_topmost(mini_hwnd)
        pop_top = self.is_window_topmost(pop_hwnd) if has_pop else False
        band_mismatch = (mini_top != agy_topmost) or (has_pop and pop_top != agy_topmost)

        last_other_seq = above_others[-1][0]
        if has_pop:
            already_sandwiched = (
                not band_mismatch
                and p_i is not None
                and m_i is not None
                and last_other_seq < p_i < m_i < a_i
            )
        else:
            already_sandwiched = not band_mismatch and m_i is not None and last_other_seq < m_i < a_i

        if already_sandwiched:
            return True

        if not agy_topmost:
            if mini_top:
                self.user32.SetWindowPos(mini_hwnd, self.HWND_NOTOPMOST, 0, 0, 0, 0, flags)
            if has_pop and pop_top:
                self.user32.SetWindowPos(pop_hwnd, self.HWND_NOTOPMOST, 0, 0, 0, 0, flags)
        else:
            if not mini_top:
                self.user32.SetWindowPos(mini_hwnd, self.HWND_TOPMOST, 0, 0, 0, 0, flags)
            if has_pop and not pop_top:
                self.user32.SetWindowPos(pop_hwnd, self.HWND_TOPMOST, 0, 0, 0, 0, flags)

        for i in range(len(above_others) - 1, -1, -1):
            _, anchor_hwnd, _ = above_others[i]
            if has_pop:
                ok1 = bool(self.user32.SetWindowPos(pop_hwnd, anchor_hwnd, 0, 0, 0, 0, flags))
                ok2 = bool(self.user32.SetWindowPos(mini_hwnd, pop_hwnd, 0, 0, 0, 0, flags)) if ok1 else False
                placed = ok1 and ok2
            else:
                placed = bool(self.user32.SetWindowPos(mini_hwnd, anchor_hwnd, 0, 0, 0, 0, flags))

            if placed:
                if card_phys_rect:
                    for _, _wh, skipped_rect in above_others[i + 1 :]:
                        if self._rects_intersect(skipped_rect, card_phys_rect):
                            return False
                return True

        # Fallback if every window above Antigravity rejected SetWindowPos (e.g. elevated UIPI)
        if card_phys_rect:
            for _, _wh, r in above_others:
                if self._rects_intersect(r, card_phys_rect):
                    return False
        return True


# ---------------------------------------------------------------------------
# Custom Qt Widgets & Layouts
# ---------------------------------------------------------------------------
class FlowLayout(QLayout):
    """Wraps model chips cleanly across multiple rows inside narrow cards."""

    def __init__(self, parent=None, margin=0, h_spacing=4, v_spacing=4):
        super().__init__(parent)
        self.setContentsMargins(margin, margin, margin, margin)
        self._h_space = h_spacing
        self._v_space = v_spacing
        self._items = []

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        m = self.contentsMargins()
        eff = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x = eff.x()
        y = eff.y()
        line_height = 0
        for item in self._items:
            sz = item.sizeHint()
            next_x = x + sz.width() + self._h_space
            if next_x - self._h_space > eff.right() and line_height > 0:
                x = eff.x()
                y = y + line_height + self._v_space
                next_x = x + sz.width() + self._h_space
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), sz))
            x = next_x
            line_height = max(line_height, sz.height())
        return y + line_height - rect.y() + m.bottom()


class QuotaProgressBar(QWidget):
    """Sleek 6px rounded progress bar painted with QPainter."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pct = 0.0
        self._track_color = QColor("#e5e7eb")
        self._fill_color = QColor("#16a34a")
        self.setFixedHeight(6)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_state(self, pct: float, track_hex: str, fill_hex: str) -> None:
        self._pct = max(0.0, min(100.0, float(pct)))
        self._track_color = QColor(track_hex)
        self._fill_color = QColor(fill_hex)
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w = self.width()
        h = self.height()
        r = h / 2.0

        p.setPen(Qt.NoPen)
        p.setBrush(self._track_color)
        p.drawRoundedRect(QRectF(0.0, 0.0, float(w), float(h)), r, r)

        if self._pct > 0.0 and w > 0:
            fill_w = max(float(h), w * (self._pct / 100.0))
            fill_w = min(float(w), fill_w)
            p.setBrush(self._fill_color)
            p.drawRoundedRect(QRectF(0.0, 0.0, float(fill_w), float(h)), r, r)
        p.end()


def make_pill_label(text: str, bg: str, fg: str, bold: bool = False, tooltip: str = "") -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"QLabel {{ background: {bg}; color: {fg}; border-radius: 4px; "
        f"padding: 1px 5px; font-size: 11px; font-weight: {'600' if bold else '500'}; }}"
    )
    if tooltip:
        lbl.setToolTip(tooltip)
    return lbl


def paint_soft_shadow(widget: QWidget, target_rect: QRect, radius: float, is_float: bool, theme: str) -> None:
    """Paints smooth multi-layer rounded drop shadow without QGraphicsDropShadowEffect.

    Avoids Qt5's well-known crash when QGraphicsDropShadowEffect wraps a QScrollArea.
    """
    if not target_rect.isValid():
        return
    p = QPainter(widget)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    base_rgb = (0, 0, 0) if theme == "dark" else (15, 23, 42)
    layers = (
        [(1, 2, 30), (3, 4, 20), (5, 5, 12), (6, 6, 6)]
        if is_float
        else [(1, 1, 18), (3, 2, 10), (5, 3, 5)]
    )
    if theme == "dark":
        layers = [(spread, y_off, min(140, alpha * 2)) for spread, y_off, alpha in layers]
    for spread, y_off, alpha in layers:
        dy = y_off // 2
        r = target_rect.adjusted(-spread, -spread + dy, spread, spread + dy)
        p.setBrush(QColor(base_rgb[0], base_rgb[1], base_rgb[2], alpha))
        p.drawRoundedRect(QRectF(r), radius + spread * 0.5, radius + spread * 0.5)
    p.end()


def _build_antigravity_icon_path(scale: float = 1.0) -> QPainterPath:
    """Smooth Antigravity ribbon 'A' arch path centered at (0, 0)."""
    s = scale
    path = QPainterPath()
    path.moveTo(-8.2 * s, 7.6 * s)
    path.cubicTo(-4.8 * s, 6.2 * s, -3.6 * s, -7.8 * s, 0.0, -7.8 * s)
    path.cubicTo(3.6 * s, -7.8 * s, 4.8 * s, 6.2 * s, 8.2 * s, 7.6 * s)
    path.cubicTo(5.8 * s, 8.2 * s, 4.4 * s, 6.4 * s, 3.0 * s, 3.4 * s)
    path.cubicTo(1.7 * s, 0.6 * s, -1.7 * s, 0.6 * s, -3.0 * s, 3.4 * s)
    path.cubicTo(-4.4 * s, 6.4 * s, -5.8 * s, 8.2 * s, -8.2 * s, 7.6 * s)
    path.closeSubpath()
    return path


def _draw_antigravity_logo(p: QPainter, cx: float, cy: float, size: float, color: QColor) -> None:
    p.save()
    p.translate(cx, cy + size * 0.03)
    path = _build_antigravity_icon_path(size / 20.0)
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawPath(path)
    p.restore()


def _draw_openai_logo(p: QPainter, cx: float, cy: float, size: float, color: QColor) -> None:
    p.save()
    p.translate(cx, cy)
    r = size * 0.46
    pen_w = max(1.3, size * 0.105)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(color, pen_w, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    for i in range(6):
        p.save()
        p.rotate(i * 60 + 15)
        path = QPainterPath()
        path.moveTo(-r * 0.28, -r * 0.48)
        path.lineTo(r * 0.46, -r * 0.48)
        path.cubicTo(r * 0.96, -r * 0.48, r * 1.02, r * 0.14, r * 0.52, r * 0.36)
        path.lineTo(r * 0.14, r * 0.14)
        p.drawPath(path)
        p.restore()
    p.restore()


class MiniQuotaCard(QWidget):
    """Minimal Split-Capsule dual-ring dock (Antigravity + OpenAI Codex)."""

    toggled_expand = pyqtSignal()
    toggled_dock = pyqtSignal()
    drag_started = pyqtSignal()
    moved_while_dragging = pyqtSignal()
    drag_finished = pyqtSignal(int, int)
    pod_clicked = pyqtSignal(str)
    context_menu_requested = pyqtSignal(QPoint)
    hover_entered = pyqtSignal()
    hover_left = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Antigravity Quota")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setCursor(Qt.PointingHandCursor)

        self._dock_mode = "docked"
        self._theme = "dark"
        self._mode = "used"
        self._expanded = False
        self._hovered = False
        self._tb_height = 32
        self._pinned_pods = ["antigravity", "codex"]
        self._dragging = False
        self._drag_moved = False
        self._press_global = QPoint()
        self._press_win = QPoint()
        self._pressed_pod: str | None = None
        self._pod_scale = {"antigravity": 1.0, "codex": 1.0}
        self._bounce_anim: dict[str, tuple[float, float]] = {}
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(16)
        self._anim_timer.timeout.connect(self._tick_pod_anim)

        self._left_data = {
            "kind": "antigravity",
            "fh_pct": 0.0,
            "fh_used": 0.0,
            "wk_pct": None,
            "fh_reset_text": "--",
            "has_data": False,
        }
        self._right_data = {
            "kind": "codex",
            "fh_pct": 0.0,
            "fh_used": 0.0,
            "wk_pct": None,
            "fh_reset_text": "--",
            "has_data": False,
        }
        self._stale = False
        self._error_msg = None

        self.setFixedSize(
            MINI_VIS_WIDTH + SHADOW_MARGIN * 2,
            MINI_VIS_HEIGHT + SHADOW_MARGIN * 2,
        )
        self.apply_theme("dark", "docked")

    def _calc_vis_width(self) -> int:
        count = max(1, len(self._pinned_pods))
        in_tb = self._dock_mode == "taskbar" and not self._drag_moved
        pod_w = 61 if in_tb else 66
        return pod_w * count + 6

    def _sync_fixed_size(self) -> None:
        w = self._calc_vis_width() + SHADOW_MARGIN * 2
        h = MINI_VIS_HEIGHT + SHADOW_MARGIN * 2
        if self.width() != w or self.height() != h:
            self.setFixedSize(w, h)

    def visible_size(self) -> tuple[int, int]:
        return self._calc_vis_width(), MINI_VIS_HEIGHT

    def set_pinned_pods(self, pods: list[str]) -> None:
        valid = [p for p in pods if p in ("antigravity", "codex")]
        self._pinned_pods = valid or ["antigravity"]
        self._sync_fixed_size()
        self.update()

    def apply_theme(self, theme: str, dock_mode: str) -> None:
        self._theme = theme
        self._dock_mode = dock_mode
        self._sync_fixed_size()
        self.update()

    def _extract_group_metrics(self, group: dict | None, fallback_fh=None, fallback_wk=None) -> dict:
        fh = (group.get("fiveHour") if isinstance(group, dict) else None) or fallback_fh
        wk = (group.get("weekly") if isinstance(group, dict) else None) or fallback_wk
        is_used = self._mode == "used"

        if fh:
            fh_used = float(fh.get("usedPercent") or 0.0)
            fh_rem = float(fh.get("remainingPercent") if fh.get("remainingPercent") is not None else max(0.0, 100.0 - fh_used))
            fh_pct = fh_used if is_used else fh_rem
            reset_ms = fh.get("resetMs")
            cd = fmt_short_countdown(reset_ms)
            fh_reset_text = cd if cd else ("已重置" if fh_used <= 0 else "--")
            fh_tip_reset = fmt_full_reset_time(reset_ms)
        else:
            fh_used = 0.0
            fh_pct = 0.0
            fh_reset_text = "--"
            fh_tip_reset = ""

        if wk:
            wk_used = float(wk.get("usedPercent") or 0.0)
            wk_rem = float(wk.get("remainingPercent") if wk.get("remainingPercent") is not None else max(0.0, 100.0 - wk_used))
            wk_pct = wk_used if is_used else wk_rem
        else:
            wk_pct = None

        return {
            "fh_pct": fh_pct,
            "fh_used": fh_used,
            "wk_pct": wk_pct,
            "fh_reset_text": fh_reset_text,
            "fh_tip_reset": fh_tip_reset,
            "has_data": bool(fh or wk),
        }

    def update_data(self, payload: dict | None, error_msg: str | None, mode: str, expanded: bool) -> None:
        self._mode = mode
        self._expanded = expanded
        self._error_msg = error_msg
        self._stale = bool((payload or {}).get("stale") or error_msg)

        groups = (payload or {}).get("groups") or []
        cur = (payload or {}).get("current") or {}

        gemini_grp = None
        codex_grp = None
        for g in groups:
            gid = str(g.get("id") or "").lower()
            gname = str(g.get("displayName") or "").lower()
            if g.get("externalSource") == "codex" or "codex" in gid or "codex" in gname:
                codex_grp = g
            elif gemini_grp is None:
                gemini_grp = g

        self._left_data = {
            "kind": "antigravity",
            **self._extract_group_metrics(gemini_grp, cur.get("fiveHour"), cur.get("weekly")),
        }
        self._right_data = {
            "kind": "codex",
            **self._extract_group_metrics(codex_grp),
        }

        self.setToolTip("")
        self.update()

    def _ring_color(self, base_hex: str, used_pct: float) -> QColor:
        if used_pct >= 90.0:
            return QColor("#ef4444")
        if used_pct >= 75.0:
            return QColor("#f59e0b")
        return QColor(base_hex)

    def set_taskbar_height(self, tb_h: int) -> None:
        self._tb_height = max(26, min(32, int(tb_h) - 4))
        self.update()

    def _hit_test_pod(self, local_pos: QPoint) -> str:
        pods = [p for p in ("antigravity", "codex") if p in self._pinned_pods] or ["antigravity"]
        if len(pods) == 1:
            return pods[0]
        mid_x = SHADOW_MARGIN + self._calc_vis_width() / 2.0
        return pods[0] if local_pos.x() < mid_x else pods[1]

    def _start_pod_bounce(self, pod_key: str) -> None:
        self._bounce_anim[pod_key] = (time.monotonic(), self._pod_scale.get(pod_key, 0.88))
        if not self._anim_timer.isActive():
            self._anim_timer.start()

    def _tick_pod_anim(self) -> None:
        now = time.monotonic()
        done: list[str] = []
        changed = False
        for key, (t0, s0) in list(self._bounce_anim.items()):
            dt = now - t0
            if dt >= 0.26:
                self._pod_scale[key] = 1.0
                done.append(key)
                changed = True
            else:
                # Crisp underdamped spring bounce: 0.88 -> ~1.035 -> 1.0
                val = 1.0 + (s0 - 1.0) * math.exp(-13.5 * dt) * math.cos(24.0 * dt)
                self._pod_scale[key] = val
                changed = True
        for key in done:
            self._bounce_anim.pop(key, None)
        if not self._bounce_anim:
            self._anim_timer.stop()
        if changed:
            self.update()

    def _paint_pod(
        self,
        p: QPainter,
        pod_left: float,
        cy_mid: float,
        data: dict,
        base_accent: str,
        is_dark: bool,
        compact_tb: bool = False,
    ) -> None:
        kind = data.get("kind") or "antigravity"
        sc = float(self._pod_scale.get(kind, 1.0))
        pod_w = 61.0 if compact_tb else 66.0
        p.save()
        if abs(sc - 1.0) > 0.001:
            pivot_x = pod_left + pod_w * 0.48
            p.translate(pivot_x, cy_mid)
            p.scale(sc, sc)
            p.translate(-pivot_x, -cy_mid)

        rad = 10.0 if compact_tb else 11.5
        ring_cx = pod_left + (14.5 if compact_tb else 16.5)
        ring_rect = QRectF(ring_cx - rad, cy_mid - rad, rad * 2, rad * 2)

        track_col = QColor("#232733") if is_dark else QColor("#e2e8f0")
        icon_col = QColor("#f1f5f9") if is_dark else QColor("#1e293b")
        week_col = QColor("#cbd5e1") if is_dark else QColor("#334155")
        reset_col = QColor("#64748b") if is_dark else QColor("#64748b")

        stroke = 2.1 if compact_tb else 2.4
        p.save()
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(track_col, stroke, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(ring_rect, 0, 360 * 16)

        pct = float(data.get("fh_pct") or 0.0)
        if data.get("has_data") and pct > 0.0:
            arc_col = self._ring_color(base_accent, float(data.get("fh_used") or 0.0))
            p.setPen(QPen(arc_col, stroke, Qt.SolidLine, Qt.RoundCap))
            span = int(-360 * 16 * max(0.04, min(1.0, pct / 100.0)))
            p.drawArc(ring_rect, 90 * 16, span)
        p.restore()

        if data.get("kind") == "antigravity":
            _draw_antigravity_logo(p, ring_cx, cy_mid, 9.5 if compact_tb else 11.0, icon_col)
        else:
            _draw_openai_logo(p, ring_cx, cy_mid, 10.5 if compact_tb else 12.0, icon_col)

        tx = ring_cx + (12.5 if compact_tb else 14.5)
        wk_val = data.get("wk_pct")
        wk_txt = f"周{wk_val:.0f}%" if wk_val is not None else "周--"
        f_wk = QFont("Microsoft YaHei UI")
        f_wk.setPixelSize(8 if compact_tb else 9)
        f_wk.setWeight(QFont.DemiBold)
        p.setFont(f_wk)
        p.setPen(week_col)
        p.drawText(
            QRectF(tx, cy_mid - (12.0 if compact_tb else 13.5), 36, 12 if compact_tb else 14),
            Qt.AlignVCenter | Qt.AlignLeft,
            wk_txt,
        )

        rst_txt = data.get("fh_reset_text") or "--"
        f_rst = QFont("Segoe UI")
        f_rst.setPixelSize(7 if compact_tb else 8)
        p.setFont(f_rst)
        p.setPen(reset_col)
        p.drawText(
            QRectF(tx, cy_mid + (0.0 if compact_tb else 0.5), 36, 11 if compact_tb else 13),
            Qt.AlignVCenter | Qt.AlignLeft,
            rst_txt,
        )
        p.restore()

    def paintEvent(self, _event):
        in_taskbar = self._dock_mode == "taskbar" and not self._drag_moved
        vis_w = self._calc_vis_width()
        if in_taskbar:
            vis_h = self._tb_height
            y_off = SHADOW_MARGIN + (MINI_VIS_HEIGHT - vis_h) // 2
            surface_rect = QRect(SHADOW_MARGIN, y_off, vis_w, vis_h)
            radius = 5.0
        else:
            surface_rect = QRect(SHADOW_MARGIN, SHADOW_MARGIN, vis_w, MINI_VIS_HEIGHT)
            radius = MINI_VIS_HEIGHT / 2.0

        if not in_taskbar:
            paint_soft_shadow(
                self,
                surface_rect,
                radius,
                self._dock_mode == "float",
                self._theme,
            )

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)

        is_dark = self._theme == "dark"
        rect = QRectF(surface_rect)
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)

        if in_taskbar:
            if self._hovered or self._expanded:
                bg = QColor(255, 255, 255, 16) if is_dark else QColor(0, 0, 0, 12)
                bd = QColor(255, 255, 255, 22) if is_dark else QColor(0, 0, 0, 18)
            else:
                bg = QColor(255, 255, 255, 1)
                bd = QColor(0, 0, 0, 0)
            p.fillPath(path, bg)
            if bd.alpha() > 0:
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(bd, 1.0))
                p.drawPath(path)
        else:
            bg = QColor(19, 21, 27, 242) if is_dark else QColor(255, 255, 255, 244)
            p.fillPath(path, bg)

            if is_dark:
                grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
                grad.setColorAt(0.0, QColor(255, 255, 255, 16))
                grad.setColorAt(0.35, QColor(255, 255, 255, 0))
                p.fillPath(path, grad)

            if self._expanded:
                bd = QColor("#60a5fa") if is_dark else QColor("#2563eb")
                bd.setAlpha(200)
            elif self._dock_mode == "float":
                bd = QColor("#3b4459") if is_dark else QColor("#cbd5e1")
                bd.setAlpha(220)
            else:
                bd = QColor("#292e3b") if is_dark else QColor("#e2e8f0")
                bd.setAlpha(215)

            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(bd, 1.0))
            p.drawPath(path)

        pods_to_draw = []
        if "antigravity" in self._pinned_pods:
            pods_to_draw.append((self._left_data, "#60a5fa"))
        if "codex" in self._pinned_pods:
            pods_to_draw.append((self._right_data, "#34d399"))
        if not pods_to_draw:
            pods_to_draw.append((self._left_data, "#60a5fa"))

        cy_mid = rect.top() + rect.height() / 2.0
        if len(pods_to_draw) == 2:
            div_col = QColor(255, 255, 255, 18) if is_dark else QColor(15, 23, 42, 22)
            mid_x = rect.left() + rect.width() / 2.0
            div_pad = 6.0 if in_taskbar else 9.0
            p.setPen(QPen(div_col, 1.0))
            p.drawLine(QPointF(mid_x, rect.top() + div_pad), QPointF(mid_x, rect.bottom() - div_pad))
            self._paint_pod(p, rect.left() + 3.0, cy_mid, pods_to_draw[0][0], pods_to_draw[0][1], is_dark, compact_tb=in_taskbar)
            self._paint_pod(p, mid_x + 2.0, cy_mid, pods_to_draw[1][0], pods_to_draw[1][1], is_dark, compact_tb=in_taskbar)
        else:
            self._paint_pod(p, rect.left() + 3.0, cy_mid, pods_to_draw[0][0], pods_to_draw[0][1], is_dark, compact_tb=in_taskbar)

        p.end()

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        if not self._dragging:
            self.hover_entered.emit()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        if not self._dragging:
            self.hover_left.emit()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._dragging = True
            self._drag_moved = False
            self._press_global = event.globalPos()
            self._press_win = self.pos()
            pod = self._hit_test_pod(event.pos())
            self._pressed_pod = pod
            self._bounce_anim.pop(pod, None)
            self._pod_scale[pod] = 0.88
            self.update()
            event.accept()
            return
        if event.button() == Qt.RightButton:
            self.context_menu_requested.emit(event.globalPos())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging and (event.buttons() & Qt.LeftButton):
            delta = event.globalPos() - self._press_global
            if self._dock_mode in ("float", "taskbar") and (abs(delta.x()) >= 5 or abs(delta.y()) >= 5):
                if not self._drag_moved:
                    self._drag_moved = True
                    if self._pressed_pod:
                        self._start_pod_bounce(self._pressed_pod)
                        self._pressed_pod = None
                    self._sync_fixed_size()
                    self.drag_started.emit()
                    self.update()
            if self._drag_moved:
                self.move(self._press_win + delta)
                self.moved_while_dragging.emit()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._dragging:
            was_drag = self._drag_moved
            pressed_pod = self._pressed_pod
            self._dragging = False
            self._drag_moved = False
            self._pressed_pod = None
            if pressed_pod:
                self._start_pod_bounce(pressed_pod)
            if was_drag:
                self.drag_finished.emit(self.x(), self.y())
            else:
                target = pressed_pod or self._hit_test_pod(event.pos())
                self.pod_clicked.emit(target)
            event.accept()
            return
        super().mouseReleaseEvent(event)


def _make_mini_icon(kind: str, color_hex: str, filled: bool = False, size: int = 16) -> QIcon:
    """Draw crisp vector icons (user / refresh / pin) at 2x resolution for HiDPI."""
    scale = 2
    s = size * scale
    pm = QPixmap(s, s)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    col = QColor(color_hex)
    pen = QPen(col, 1.5 * scale, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)

    cx, cy = s / 2.0, s / 2.0
    if kind == "user":
        # Minimalist head + shoulders
        p.drawEllipse(QPointF(cx, cy - 2.6 * scale), 2.3 * scale, 2.3 * scale)
        arc = QPainterPath()
        arc.moveTo(cx - 4.5 * scale, cy + 4.8 * scale)
        arc.cubicTo(cx - 4.2 * scale, cy + 1.2 * scale, cx + 4.2 * scale, cy + 1.2 * scale, cx + 4.5 * scale, cy + 4.8 * scale)
        p.drawPath(arc)
    elif kind == "refresh":
        # Minimalist circular refresh arrow
        rad = 4.2 * scale
        rect = QRectF(cx - rad, cy - rad, rad * 2, rad * 2)
        p.drawArc(rect, 35 * 16, 290 * 16)
        # Arrowhead at top-right
        ax, ay = cx + 3.4 * scale, cy - 2.6 * scale
        p.drawLine(QPointF(ax, ay), QPointF(ax - 2.4 * scale, ay - 0.3 * scale))
        p.drawLine(QPointF(ax, ay), QPointF(ax + 0.3 * scale, ay - 2.4 * scale))
    elif kind == "pin":
        # Minimalist pushpin (tilted 38 deg when unpinned, upright & filled when pinned)
        p.save()
        p.translate(cx, cy)
        if not filled:
            p.rotate(38)
        # Pin head
        head = QPainterPath()
        head.moveTo(-2.5 * scale, -4.4 * scale)
        head.lineTo(2.5 * scale, -4.4 * scale)
        head.lineTo(1.8 * scale, -1.2 * scale)
        head.lineTo(3.2 * scale, 0.6 * scale)
        head.lineTo(-3.2 * scale, 0.6 * scale)
        head.lineTo(-1.8 * scale, -1.2 * scale)
        head.closeSubpath()
        if filled:
            p.setBrush(col)
        p.drawPath(head)
        # Pin needle
        p.drawLine(QPointF(0, 0.8 * scale), QPointF(0, 4.8 * scale))
        p.restore()

    p.end()
    pm.setDevicePixelRatio(scale)
    return QIcon(pm)


class ExpandedPopover(QWidget):
    """Expanded floating card displaying all model groups and local account switching."""

    closed_requested = pyqtSignal()
    mode_toggled = pyqtSignal()
    refresh_requested = pyqtSignal()
    pin_toggled = pyqtSignal()
    pod_pin_toggled = pyqtSignal(str)
    hover_entered = pyqtSignal()
    hover_left = pyqtSignal()
    tab_changed = pyqtSignal(str)
    size_changed = pyqtSignal()
    capture_account_requested = pyqtSignal()
    import_agy_switch_requested = pyqtSignal()
    switch_account_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("全部模型用量")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

        self._theme = "light"
        self._mode = "used"
        self._pinned = False
        self._pinned_pods = ["antigravity", "codex"]
        self._active_tab = "quota"
        self._payload: dict | None = None
        self._accounts_data: dict | None = None
        self._action_banner: tuple[str, str] | None = None
        self._confirm_switch_acc: dict | None = None
        self._busy_action: str | None = None
        self._render_pending = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN)
        outer.setSpacing(0)

        self.surface = QFrame()
        self.surface.setObjectName("popoverSurface")
        self.surface.setFixedWidth(POPOVER_VIS_WIDTH)
        outer.addWidget(self.surface)

        root = QVBoxLayout(self.surface)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        # Header
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(4)

        self.title_lbl = QLabel("模型用量")
        head.addWidget(self.title_lbl)

        self.plan_lbl = QLabel("")
        self.plan_lbl.setVisible(False)
        head.addWidget(self.plan_lbl)

        head.addStretch(1)

        self.mode_btn = QPushButton("已用")
        self.mode_btn.setVisible(False)
        self.mode_btn.clicked.connect(self.mode_toggled.emit)

        self.acc_toggle_btn = QPushButton("")
        self.acc_toggle_btn.setFixedSize(20, 20)
        self.acc_toggle_btn.setIconSize(QSize(13, 13))
        self.acc_toggle_btn.setCursor(Qt.PointingHandCursor)
        self.acc_toggle_btn.setToolTip("账号切换 / 管理")
        self.acc_toggle_btn.clicked.connect(
            lambda: self._select_tab("quota" if self._active_tab == "accounts" else "accounts")
        )
        head.addWidget(self.acc_toggle_btn)

        self.refresh_btn = QPushButton("")
        self.refresh_btn.setFixedSize(20, 20)
        self.refresh_btn.setIconSize(QSize(13, 13))
        self.refresh_btn.setCursor(Qt.PointingHandCursor)
        self.refresh_btn.setToolTip("立即刷新配额")
        self.refresh_btn.clicked.connect(self.refresh_requested.emit)
        head.addWidget(self.refresh_btn)

        self.pin_btn = QPushButton("")
        self.pin_btn.setFixedSize(20, 20)
        self.pin_btn.setIconSize(QSize(13, 13))
        self.pin_btn.setCursor(Qt.PointingHandCursor)
        self.pin_btn.setToolTip("置顶常驻显示（取消则鼠标挪开自动隐藏）")
        self.pin_btn.clicked.connect(self.pin_toggled.emit)
        head.addWidget(self.pin_btn)

        self.close_btn = QPushButton("×")
        self.close_btn.setVisible(False)
        self.close_btn.clicked.connect(self.closed_requested.emit)

        root.addLayout(head)

        self.head_line = QFrame()
        self.head_line.setFixedHeight(1)
        root.addWidget(self.head_line)

        # Hidden legacy tab_bar kept for compatibility
        self.tab_bar = QFrame()
        self.tab_bar.setObjectName("popoverTabBar")
        self.tab_bar.setVisible(False)
        tab_lay = QHBoxLayout(self.tab_bar)
        tab_lay.setContentsMargins(0, 0, 0, 0)
        self.tab_quota_btn = QPushButton("模型配额")
        self.tab_quota_btn.clicked.connect(lambda: self._select_tab("quota"))
        tab_lay.addWidget(self.tab_quota_btn)
        self.tab_accounts_btn = QPushButton("账号切换 (0)")
        self.tab_accounts_btn.clicked.connect(lambda: self._select_tab("accounts"))
        tab_lay.addWidget(self.tab_accounts_btn)

        # Groups / accounts container (wrapped in borderless scroll area)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        self.groups_host = QWidget()
        self.groups_host.setFixedWidth(POPOVER_VIS_WIDTH - 20)
        self.groups_host.setStyleSheet("background: transparent;")
        self.groups_layout = QVBoxLayout(self.groups_host)
        self.groups_layout.setContentsMargins(0, 0, 0, 0)
        self.groups_layout.setSpacing(6)
        self.scroll.setWidget(self.groups_host)
        root.addWidget(self.scroll)

        # Compact footer (hidden on quota view to keep popover ultra-slim)
        self.foot_widget = QWidget()
        foot = QHBoxLayout(self.foot_widget)
        foot.setContentsMargins(0, 0, 0, 0)
        foot.setSpacing(4)
        self.foot_note = QLabel("")
        self.foot_time = QLabel("")
        foot.addWidget(self.foot_note)
        foot.addStretch(1)
        foot.addWidget(self.foot_time)
        self.foot_widget.setVisible(False)
        root.addWidget(self.foot_widget)

        self.apply_theme("light")

    def visible_size(self) -> tuple[int, int]:
        return self.surface.width(), self.surface.height()

    def has_active_account_interaction(self) -> bool:
        return self._active_tab == "accounts" and (
            self._confirm_switch_acc is not None or self._busy_action is not None
        )

    def _schedule_render(self) -> None:
        if not self.isVisible():
            self._render_current_tab()
            return
        if self._render_pending:
            return
        self._render_pending = True
        QTimer.singleShot(0, self._flush_scheduled_render)

    def _flush_scheduled_render(self) -> None:
        self._render_pending = False
        self._render_current_tab()

    def _select_tab(self, tab: str) -> None:
        if tab not in ("quota", "accounts"):
            return
        if self._active_tab != tab:
            self._active_tab = tab
            self._confirm_switch_acc = None
            self._apply_tab_styles()
            self._schedule_render()
            self.tab_changed.emit(tab)

    def set_active_tab(self, tab: str) -> None:
        if tab in ("quota", "accounts"):
            self._active_tab = tab
            self._confirm_switch_acc = None
            self._apply_tab_styles()
            self._render_current_tab()

    def set_action_banner(self, kind: str | None, text: str | None) -> None:
        self._action_banner = (kind, text) if (kind and text) else None
        if self._active_tab == "accounts":
            self._schedule_render()

    def set_busy_action(self, action: str | None) -> None:
        self._busy_action = action
        if self._active_tab == "accounts":
            self._schedule_render()

    def refresh_timestamps(
        self,
        payload: dict | None = None,
        accounts_data: dict | None = None,
    ) -> None:
        if payload is not None:
            self._payload = payload
        if accounts_data is not None:
            self._accounts_data = accounts_data
        acc_count = len((self._accounts_data or {}).get("accounts") or [])
        self.tab_accounts_btn.setText(f"账号切换 ({acc_count})")
        if self._payload:
            self.plan_lbl.setVisible(False)
            self.foot_time.setText(fmt_relative_updated(self._payload.get("fetchedAt")))
        else:
            self.plan_lbl.setVisible(False)
            self.foot_time.setText("")

    def _apply_tab_styles(self) -> None:
        pal = PALETTES[self._theme]
        is_acc = self._active_tab == "accounts"
        self.acc_toggle_btn.setIcon(_make_mini_icon("user", pal["accent"] if is_acc else pal["muted"], size=13))
        self.acc_toggle_btn.setToolTip("返回模型配额" if is_acc else "账号切换 / 管理")
        if is_acc:
            self.acc_toggle_btn.setStyleSheet(
                f"QPushButton {{ background: {pal['accent_soft']}; border: none; border-radius: 5px; padding: 0px; }}"
            )
        else:
            self.acc_toggle_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; border: none; border-radius: 5px; padding: 0px; }}"
                f"QPushButton:hover {{ background: {pal['hover']}; }}"
            )

    def set_pinned(self, pinned: bool) -> None:
        self._pinned = bool(pinned)
        self._apply_pin_style()

    def set_pinned_pods(self, pods: list[str]) -> None:
        valid = [p for p in pods if p in ("antigravity", "codex")]
        self._pinned_pods = valid or ["antigravity"]
        if self._active_tab == "quota" and self._payload is not None:
            self._schedule_render()

    def _apply_pin_style(self) -> None:
        pal = PALETTES[self._theme]
        self.pin_btn.setIcon(
            _make_mini_icon("pin", pal["accent"] if self._pinned else pal["muted"], filled=self._pinned, size=13)
        )
        if self._pinned:
            self.pin_btn.setToolTip("已常驻置顶（点击取消常驻）")
            self.pin_btn.setStyleSheet(
                f"QPushButton {{ background: {pal['accent_soft']}; border: none; border-radius: 5px; padding: 0px; }}"
                f"QPushButton:hover {{ background: {pal['hover']}; }}"
            )
        else:
            self.pin_btn.setToolTip("置顶常驻显示（未置顶时鼠标挪开自动消失）")
            self.pin_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; border: none; border-radius: 5px; padding: 0px; }}"
                f"QPushButton:hover {{ background: {pal['hover']}; }}"
            )

    def enterEvent(self, event):
        self.hover_entered.emit()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.hover_left.emit()
        super().leaveEvent(event)

    def apply_theme(self, theme: str) -> None:
        self._theme = theme
        pal = PALETTES[theme]
        self.surface.setStyleSheet(
            f"#popoverSurface {{ background: {pal['card_bg']}; border: 1px solid {pal['border']}; "
            f"border-radius: 12px; }}"
        )
        self.update()
        self.title_lbl.setStyleSheet(f"color: {pal['fg']}; font-size: 11px; font-weight: 600;")
        self.plan_lbl.setStyleSheet(
            f"background: {pal['accent_soft']}; color: {pal['accent']}; border-radius: 3px; "
            f"padding: 1px 5px; font-size: 9px; font-weight: 600;"
        )
        icon_btn_css = (
            f"QPushButton {{ background: transparent; border: none; border-radius: 5px; padding: 0px; }}"
            f"QPushButton:hover {{ background: {pal['hover']}; }}"
        )
        self.mode_btn.setStyleSheet(icon_btn_css)
        self.refresh_btn.setIcon(_make_mini_icon("refresh", pal["muted"], size=13))
        self.refresh_btn.setStyleSheet(icon_btn_css)
        self._apply_pin_style()
        self.head_line.setStyleSheet(f"background: {pal['border']}; border: none;")
        self.foot_note.setStyleSheet(f"color: {pal['muted']}; font-size: 9.5px;")
        self.foot_time.setStyleSheet(f"color: {pal['muted']}; font-size: 9.5px;")
        self._apply_tab_styles()

    def _clear_groups(self) -> None:
        while self.groups_layout.count():
            item = self.groups_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()

    def update_data(
        self,
        payload: dict | None,
        mode: str,
        accounts_data: dict | None = None,
    ) -> None:
        self._payload = payload
        self._mode = mode
        if accounts_data is not None:
            self._accounts_data = accounts_data

        self.mode_btn.setText("已用视角" if mode == "used" else "剩余视角")
        acc_count = len((self._accounts_data or {}).get("accounts") or [])
        self.tab_accounts_btn.setText(f"账号切换 ({acc_count})")

        if payload:
            self.plan_lbl.setVisible(False)
            self.foot_time.setText(fmt_relative_updated(payload.get("fetchedAt")))
        else:
            self.plan_lbl.setVisible(False)
            self.foot_time.setText("")

        self._render_current_tab()

    def _render_current_tab(self) -> None:
        if self._active_tab == "accounts":
            self.foot_note.setText("纯本地快照 · 切换将重启")
            self.foot_widget.setVisible(True)
            self._render_accounts_tab()
        else:
            self.foot_widget.setVisible(False)
            self._render_quota_tab()

    def _render_quota_tab(self) -> None:
        pal = PALETTES[self._theme]
        self._clear_groups()
        if not self._payload:
            empty = QLabel("暂未获取到配额数据")
            empty.setStyleSheet(f"color: {pal['muted']}; font-size: 12px; padding: 12px 0;")
            self.groups_layout.addWidget(empty)
            self._resize_popover()
            return

        groups = self._payload.get("groups") or []
        for g in groups:
            self.groups_layout.addWidget(self._build_group_card(g, self._mode))
        self._resize_popover()

    def _render_accounts_tab(self) -> None:
        pal = PALETTES[self._theme]
        self._clear_groups()

        acc_data = self._accounts_data or {}
        accounts = acc_data.get("accounts") or []
        live = acc_data.get("liveAccount") or (self._payload or {}).get("account") or {}
        live_email = (live or {}).get("email") or ""
        current_saved = next((a for a in accounts if a.get("isCurrent")), None)

        # 1. Active account + quick action card
        active_card = QFrame()
        active_card.setObjectName("accountActiveCard")
        active_card.setStyleSheet(
            f"#accountActiveCard {{ background: {pal['card_alt']}; border: 1px solid {pal['border']}; border-radius: 9px; }}"
        )
        a_lay = QVBoxLayout(active_card)
        a_lay.setContentsMargins(9, 8, 9, 8)
        a_lay.setSpacing(6)

        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(6)
        cur_title = QLabel(
            (current_saved.get("remark") + f" ({current_saved.get('email')})")
            if current_saved and current_saved.get("remark")
            else (current_saved.get("email") if current_saved else (live_email or "未检测到在线账号"))
        )
        cur_title.setStyleSheet(f"color: {pal['fg']}; font-size: 12px; font-weight: 600;")
        cur_title.setWordWrap(True)
        top_row.addWidget(cur_title, 1)

        if current_saved:
            badge_txt = current_saved.get("tier") or "已保存"
            top_row.addWidget(make_pill_label(badge_txt, pal["accent_soft"], pal["accent"], bold=True))
        elif live_email:
            top_row.addWidget(make_pill_label("未保存", pal["card_bg"], pal["muted"], bold=False))
        a_lay.addLayout(top_row)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(6)

        primary_btn_css = (
            f"QPushButton {{ background: {pal['accent']}; color: {pal['accent_fg']}; border: none; "
            f"border-radius: 5px; padding: 4px 8px; font-size: 11px; font-weight: 600; }}"
            f"QPushButton:disabled {{ opacity: 0.55; }}"
        )
        sec_btn_css = (
            f"QPushButton {{ background: {pal['card_bg']}; color: {pal['fg']}; border: 1px solid {pal['border']}; "
            f"border-radius: 5px; padding: 4px 8px; font-size: 11px; font-weight: 500; }}"
            f"QPushButton:hover {{ background: {pal['hover']}; }}"
            f"QPushButton:disabled {{ opacity: 0.55; }}"
        )

        save_btn = QPushButton("更新当前凭据" if current_saved else "保存当前账号")
        save_btn.setCursor(Qt.PointingHandCursor)
        save_btn.setStyleSheet(primary_btn_css)
        save_btn.setEnabled(self._busy_action is None)
        save_btn.clicked.connect(self.capture_account_requested.emit)
        btn_row.addWidget(save_btn)

        agy_cnt = int(acc_data.get("agySwitchCount") or 0)
        imp_label = f"导入 agy-switch ({agy_cnt})" if agy_cnt > 0 else "导入 agy-switch"
        imp_btn = QPushButton(imp_label)
        imp_btn.setCursor(Qt.PointingHandCursor)
        imp_btn.setStyleSheet(sec_btn_css)
        imp_btn.setEnabled(self._busy_action is None)
        imp_btn.clicked.connect(self.import_agy_switch_requested.emit)
        btn_row.addWidget(imp_btn)
        a_lay.addLayout(btn_row)
        self.groups_layout.addWidget(active_card)

        # 2. Action feedback or failed switch status banner
        banner = self._action_banner
        if not banner:
            sw_status = acc_data.get("switchStatus") or {}
            if sw_status.get("state") == "failed" and sw_status.get("error"):
                banner = ("error", f"上次切换失败: {sw_status['error']}")
        if banner:
            b_kind, b_text = banner
            b_col = pal["red"] if b_kind == "error" else (pal["green"] if b_kind == "success" else pal["accent"])
            banner_lbl = QLabel(b_text)
            banner_lbl.setWordWrap(True)
            banner_lbl.setStyleSheet(
                f"background: {pal['card_alt']}; color: {b_col}; border: 1px solid {b_col}; "
                f"border-radius: 7px; padding: 6px 8px; font-size: 11px;"
            )
            self.groups_layout.addWidget(banner_lbl)

        # 3. Fallback switch confirmation box (when target is outside current list)
        confirm_id = (self._confirm_switch_acc or {}).get("id") or ""
        matched_in_list = any(a.get("id") == confirm_id for a in accounts) if confirm_id else False
        if self._confirm_switch_acc and not matched_in_list:
            target = self._confirm_switch_acc
            conf = QFrame()
            conf.setObjectName("switchConfirmBox")
            conf.setStyleSheet(
                f"#switchConfirmBox {{ background: {pal['card_bg']}; border: 1px solid {pal['amber']}; border-radius: 9px; }}"
            )
            c_lay = QVBoxLayout(conf)
            c_lay.setContentsMargins(9, 8, 9, 8)
            c_lay.setSpacing(6)
            t_lbl = QLabel(f"确认切换到 {target.get('email', '')}？")
            t_lbl.setWordWrap(True)
            t_lbl.setStyleSheet(f"color: {pal['fg']}; font-size: 11.5px; font-weight: 600;")
            c_lay.addWidget(t_lbl)
            w_lbl = QLabel("将关闭并重启 Antigravity（同步更新 agy CLI），进行中的任务不会迁移。")
            w_lbl.setWordWrap(True)
            w_lbl.setStyleSheet(f"color: {pal['amber']}; font-size: 11px;")
            c_lay.addWidget(w_lbl)

            c_btns = QHBoxLayout()
            c_btns.setContentsMargins(0, 0, 0, 0)
            c_btns.setSpacing(6)
            c_btns.addStretch(1)
            cancel_b = QPushButton("取消")
            cancel_b.setCursor(Qt.PointingHandCursor)
            cancel_b.setStyleSheet(sec_btn_css)
            cancel_b.clicked.connect(self._cancel_confirm_switch)
            c_btns.addWidget(cancel_b)

            ok_b = QPushButton("确认重启切换")
            ok_b.setCursor(Qt.PointingHandCursor)
            ok_b.setStyleSheet(primary_btn_css)
            ok_b.setEnabled(self._busy_action != "switch")
            target_id = target.get("id", "")
            ok_b.clicked.connect(lambda _checked=False, tid=target_id: self._accept_confirm_switch(tid))
            c_btns.addWidget(ok_b)
            c_lay.addLayout(c_btns)
            self.groups_layout.addWidget(conf)

        # 4. Saved accounts list (with inline per-card confirmation)
        confirm_card_widget = None
        if not accounts:
            empty = QLabel("暂无已保存账号。点击上方「保存当前账号」或导入 agy-switch 数据。")
            empty.setWordWrap(True)
            empty.setStyleSheet(
                f"color: {pal['muted']}; background: {pal['card_alt']}; border-radius: 8px; "
                f"padding: 10px; font-size: 11.5px;"
            )
            self.groups_layout.addWidget(empty)
        else:
            locked = bool(acc_data.get("switchLocked")) or self._busy_action == "switch"
            for acc in accounts:
                is_confirming = bool(confirm_id and acc.get("id") == confirm_id)
                card_w = self._build_account_card(acc, locked, is_confirming=is_confirming)
                self.groups_layout.addWidget(card_w)
                if is_confirming:
                    confirm_card_widget = card_w

        self._resize_popover()
        if confirm_card_widget is not None:
            QTimer.singleShot(
                0,
                lambda w=confirm_card_widget: (
                    self.scroll.ensureWidgetVisible(w, 0, 8) if w.parent() is not None else None
                ),
            )

    def _ask_confirm_switch(self, acc: dict) -> None:
        self._action_banner = None
        self._confirm_switch_acc = acc
        self._schedule_render()

    def _cancel_confirm_switch(self) -> None:
        self._confirm_switch_acc = None
        self._schedule_render()

    def _accept_confirm_switch(self, target_id: str) -> None:
        self._confirm_switch_acc = None
        if target_id:
            self.switch_account_requested.emit(target_id)
        self._schedule_render()

    def _build_account_card(
        self,
        acc: dict,
        switch_locked: bool,
        is_confirming: bool = False,
    ) -> QFrame:
        pal = PALETTES[self._theme]
        is_cur = bool(acc.get("isCurrent"))
        has_cred = bool(acc.get("hasCredential"))
        card = QFrame()
        card.setObjectName("accountCard")
        border_col = pal["amber"] if is_confirming else (pal["accent"] if is_cur else pal["border"])
        card.setStyleSheet(
            f"#accountCard {{ background: {pal['card_bg']}; border: 1px solid {border_col}; border-radius: 9px; }}"
        )
        lay = QVBoxLayout(card)
        lay.setContentsMargins(9, 7, 9, 7)
        lay.setSpacing(5)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(5)

        title_text = acc.get("remark") or acc.get("email") or ""
        t_lbl = QLabel(title_text)
        t_lbl.setStyleSheet(f"color: {pal['fg']}; font-size: 12px; font-weight: 600;")
        t_lbl.setToolTip(acc.get("email") or "")
        top.addWidget(t_lbl, 1)

        if acc.get("tier"):
            top.addWidget(make_pill_label(acc["tier"], pal["card_alt"], pal["muted"], bold=False))

        if is_cur:
            top.addWidget(make_pill_label("当前", pal["accent_soft"], pal["accent"], bold=True))
        elif is_confirming:
            top.addWidget(make_pill_label("待确认", pal["card_alt"], pal["amber"], bold=True))
        else:
            sw_btn = QPushButton("切换中..." if self._busy_action == "switch" else "切换")
            sw_btn.setCursor(Qt.PointingHandCursor)
            sw_btn.setEnabled(has_cred and not switch_locked)
            sw_btn.setStyleSheet(
                f"QPushButton {{ background: {pal['accent']}; color: {pal['accent_fg']}; border: none; "
                f"border-radius: 4px; padding: 2px 8px; font-size: 11px; font-weight: 600; }}"
                f"QPushButton:disabled {{ background: {pal['card_alt']}; color: {pal['muted']}; }}"
            )
            sw_btn.clicked.connect(lambda _checked=False, a=acc: self._ask_confirm_switch(a))
            top.addWidget(sw_btn)

        lay.addLayout(top)

        if acc.get("remark") and acc.get("email"):
            sub = QLabel(acc["email"])
            sub.setStyleSheet(f"color: {pal['muted']}; font-size: 10.5px;")
            lay.addWidget(sub)

        if is_confirming:
            conf_box = QFrame()
            conf_box.setObjectName("switchConfirmBox")
            conf_box.setStyleSheet(
                f"#switchConfirmBox {{ background: {pal['card_alt']}; border: 1px solid {pal['border']}; border-radius: 7px; }}"
            )
            c_lay = QVBoxLayout(conf_box)
            c_lay.setContentsMargins(8, 6, 8, 6)
            c_lay.setSpacing(5)

            w_lbl = QLabel("将关闭并重启 Antigravity（同步更新 agy CLI），进行中的任务不会迁移。")
            w_lbl.setWordWrap(True)
            w_lbl.setStyleSheet(f"color: {pal['amber']}; font-size: 11px;")
            c_lay.addWidget(w_lbl)

            c_btns = QHBoxLayout()
            c_btns.setContentsMargins(0, 0, 0, 0)
            c_btns.setSpacing(6)
            c_btns.addStretch(1)

            cancel_b = QPushButton("取消")
            cancel_b.setCursor(Qt.PointingHandCursor)
            cancel_b.setStyleSheet(
                f"QPushButton {{ background: {pal['card_bg']}; color: {pal['fg']}; border: 1px solid {pal['border']}; "
                f"border-radius: 5px; padding: 3px 9px; font-size: 11px; font-weight: 500; }}"
                f"QPushButton:hover {{ background: {pal['hover']}; }}"
            )
            cancel_b.clicked.connect(self._cancel_confirm_switch)
            c_btns.addWidget(cancel_b)

            ok_b = QPushButton("确认重启切换")
            ok_b.setCursor(Qt.PointingHandCursor)
            ok_b.setEnabled(not switch_locked)
            ok_b.setStyleSheet(
                f"QPushButton {{ background: {pal['accent']}; color: {pal['accent_fg']}; border: none; "
                f"border-radius: 5px; padding: 3px 10px; font-size: 11px; font-weight: 600; }}"
                f"QPushButton:disabled {{ opacity: 0.55; }}"
            )
            target_id = acc.get("id", "")
            ok_b.clicked.connect(lambda _checked=False, tid=target_id: self._accept_confirm_switch(tid))
            c_btns.addWidget(ok_b)

            c_lay.addLayout(c_btns)
            lay.addWidget(conf_box)

        last_quota = acc.get("lastQuota") or {}
        groups = last_quota.get("groups") or []
        if groups:
            chips_host = QWidget()
            chips_host.setStyleSheet("background: transparent;")
            flow = FlowLayout(chips_host, margin=0, h_spacing=4, v_spacing=3)
            is_used = self._mode == "used"
            for grp in groups[:3]:
                fh = grp.get("fiveHourUsed") if is_used else grp.get("fiveHourRemaining")
                wk = grp.get("weeklyUsed") if is_used else grp.get("weeklyRemaining")
                parts = []
                if isinstance(fh, (int, float)):
                    parts.append(f"5h {fmt_pct(fh)}")
                if isinstance(wk, (int, float)):
                    parts.append(f"周 {fmt_pct(wk)}")
                if not parts:
                    continue
                g_short = short_family_chip_name(grp.get("displayName") or "Group")
                if "claude" in g_short.lower() or "gpt" in g_short.lower():
                    g_short = "Claude/GPT"
                elif "gemini" in (grp.get("displayName") or "").lower() and "argon" not in (grp.get("displayName") or "").lower():
                    g_short = "Gemini"
                flow.addWidget(
                    make_pill_label(
                        f"{g_short} {' / '.join(parts)}",
                        pal["card_alt"],
                        pal["muted"],
                    )
                )
            lay.addWidget(chips_host)

        return card

    def _resize_popover(self) -> None:
        prev_size = self.size()
        host_w = POPOVER_VIS_WIDTH - 20
        self.groups_host.setFixedWidth(host_w)
        self.groups_layout.invalidate()
        self.groups_layout.activate()

        total_h = 0
        count = self.groups_layout.count()
        spacing = max(0, self.groups_layout.spacing())
        for i in range(count):
            w = self.groups_layout.itemAt(i).widget()
            if w is not None:
                w.ensurePolished()
                wh = w.heightForWidth(host_w) if w.hasHeightForWidth() else w.sizeHint().height()
                if wh <= 0:
                    wh = w.sizeHint().height()
                total_h += wh
                if i > 0:
                    total_h += spacing

        content_h = max(
            total_h,
            self.groups_layout.totalHeightForWidth(host_w),
            self.groups_host.sizeHint().height(),
        )
        self.groups_host.setFixedHeight(content_h)
        screen = QApplication.primaryScreen()
        max_avail_h = (screen.availableGeometry().height() - 120) if screen else 520
        max_scroll_h = max(220, min(460, max_avail_h))
        self.scroll.setFixedHeight(min(content_h + 4, max_scroll_h))
        self.surface.adjustSize()
        self.adjustSize()
        if self.size() != prev_size:
            self.size_changed.emit()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            if self._confirm_switch_acc is not None:
                self._cancel_confirm_switch()
            else:
                self.closed_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event):
        paint_soft_shadow(self, self.surface.geometry(), 12.0, True, self._theme)

    def _build_group_card(self, g: dict, mode: str) -> QFrame:
        pal = PALETTES[self._theme]
        is_codex = g.get("externalSource") == "codex" or "codex" in str(g.get("id") or "").lower()
        accent_dot = pal["green"] if is_codex else pal["accent"]

        card = QFrame()
        card.setObjectName("groupCard")
        card.setStyleSheet(
            f"#groupCard {{ background: {pal['card_alt']}; border: 1px solid {pal['border']}; border-radius: 8px; }}"
        )
        if is_codex and g.get("recordedAt"):
            card.setToolTip(f"OpenAI Codex 配额 · {fmt_relative_updated(g.get('recordedAt'))}（来自最近一次 Codex 会话）")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(8, 6, 8, 7)
        lay.setSpacing(5)

        # Compact single-line header: dot + group name + subtle active model label
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(5)

        dot = QLabel()
        dot.setFixedSize(6, 6)
        dot.setStyleSheet(f"background: {accent_dot}; border-radius: 3px;")
        top.addWidget(dot)

        raw_name = g.get("displayName") or "Model Group"
        short_group_name = "Gemini" if "gemini" in raw_name.lower() else raw_name
        g_name = QLabel(short_group_name)
        g_name.setStyleSheet(f"color: {pal['fg']}; font-size: 10.5px; font-weight: 600;")
        top.addWidget(g_name)
        top.addStretch(1)

        # Pick single concise active/primary model label
        sub_model = ""
        families = g.get("families") or []
        for fam in families:
            if fam.get("hasCurrent"):
                cur_var = next((v for v in (fam.get("variants") or []) if v.get("isCurrent")), None)
                sn = short_family_chip_name(fam.get("name") or "")
                sub_model = f"{sn} ({cur_var['tierLabel']})" if (cur_var and cur_var.get("tierLabel")) else sn
                break
        if not sub_model and families:
            sub_model = short_family_chip_name(families[0].get("name") or "")

        if sub_model:
            sub_lbl = QLabel(sub_model)
            sub_lbl.setStyleSheet(f"color: {pal['muted']}; font-size: 9px;")
            top.addWidget(sub_lbl)

        pod_key = "codex" if is_codex else "antigravity"
        is_pod_pinned = pod_key in self._pinned_pods
        pin_pod_btn = QPushButton("")
        pin_pod_btn.setFixedSize(18, 18)
        pin_pod_btn.setIconSize(QSize(11, 11))
        pin_pod_btn.setIcon(
            _make_mini_icon("pin", accent_dot if is_pod_pinned else pal["muted"], filled=is_pod_pinned, size=11)
        )
        pin_pod_btn.setCursor(Qt.PointingHandCursor)
        if is_pod_pinned and len(self._pinned_pods) <= 1:
            pin_pod_btn.setToolTip("已固定到胶囊（至少保留 1 个显示项）")
        elif is_pod_pinned:
            pin_pod_btn.setToolTip("已固定到胶囊（点击取消固定）")
        else:
            pin_pod_btn.setToolTip("未在胶囊显示（点击固定到胶囊）")

        pin_pod_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; border-radius: 4px; padding: 0px; }}"
            f"QPushButton:hover {{ background: {pal['hover']}; }}"
        )
        pin_pod_btn.clicked.connect(lambda _checked=False, k=pod_key: self.pod_pin_toggled.emit(k))
        top.addWidget(pin_pod_btn)
        lay.addLayout(top)

        # 2-column compact meters grid (5h | 周)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(2)

        grid.addWidget(self._build_bucket_cell("5h", g.get("fiveHour"), mode, "无 5h 限额"), 0, 0)
        grid.addWidget(self._build_bucket_cell("周", g.get("weekly"), mode, "无周限额"), 0, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)

        return card

    def _build_bucket_cell(self, label: str, bucket: dict | None, mode: str, missing_text: str) -> QWidget:
        pal = PALETTES[self._theme]
        cell = QWidget()
        cell.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(cell)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)

        if not bucket:
            note = QLabel(missing_text)
            note.setAlignment(Qt.AlignCenter)
            note.setStyleSheet(
                f"background: {pal['card_alt']}; color: {pal['muted']}; border-radius: 5px; "
                f"padding: 4px 6px; font-size: 11px;"
            )
            lay.addWidget(note)
            return cell

        used = float(bucket.get("usedPercent") or 0.0)
        rem = float(bucket.get("remainingPercent") if bucket.get("remainingPercent") is not None else max(0.0, 100.0 - used))
        val = used if mode == "used" else rem
        reset_ms = bucket.get("resetMs")
        cd = fmt_short_countdown(reset_ms)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(3)
        l_lbl = QLabel(label)
        l_lbl.setStyleSheet(f"color: {pal['muted']}; font-size: 9.5px;")
        v_lbl = QPushButton(fmt_pct(val))
        v_lbl.setCursor(Qt.PointingHandCursor)
        v_lbl.setFlat(True)
        mode_hint = "已用视角（点击切换为剩余视角）" if mode == "used" else "剩余视角（点击切换为已用视角）"
        tip_reset = fmt_full_reset_time(reset_ms)
        v_lbl.setToolTip(f"{mode_hint}\n{tip_reset}" if tip_reset else mode_hint)
        v_lbl.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; padding: 0px; "
            f"color: {pal['fg']}; font-size: 10px; font-weight: 600; }}"
            f"QPushButton:hover {{ color: {pal['accent']}; }}"
        )
        v_lbl.clicked.connect(self.mode_toggled.emit)
        row.addWidget(l_lbl)
        row.addStretch(1)
        row.addWidget(v_lbl)
        if cd:
            c_lbl = QLabel(cd)
            c_lbl.setStyleSheet(f"color: {pal['muted']}; font-size: 9px;")
            row.addWidget(c_lbl)
        lay.addLayout(row)

        bar = QuotaProgressBar()
        bar.setFixedHeight(4)
        bar.set_state(val, pal["track"], pal[tone_color_key(used)])
        lay.addWidget(bar)

        tip = fmt_full_reset_time(reset_ms)
        if tip:
            cell.setToolTip(f"已用 {fmt_pct(used)} · 剩余 {fmt_pct(rem)}\n{tip}")
        return cell

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.closed_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


# ---------------------------------------------------------------------------
# Tray Icon Painter & Application Controller
# ---------------------------------------------------------------------------
def build_tray_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)

    # Background rounded badge
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#1e293b"))
    p.drawRoundedRect(4, 4, 56, 56, 14, 14)

    # Track arc
    pen_track = QPen(QColor("#475569"), 6, Qt.SolidLine, Qt.RoundCap)
    p.setPen(pen_track)
    p.drawArc(14, 14, 36, 36, -45 * 16, 270 * 16)

    # Active blue-green arc
    pen_arc = QPen(QColor("#38bdf8"), 6, Qt.SolidLine, Qt.RoundCap)
    p.setPen(pen_arc)
    p.drawArc(14, 14, 36, 36, 90 * 16, 135 * 16)

    # Center bars
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#22c55e"))
    p.drawRoundedRect(23, 34, 5, 13, 2, 2)
    p.drawRoundedRect(30, 26, 5, 21, 2, 2)
    p.drawRoundedRect(37, 30, 5, 17, 2, 2)

    p.end()
    return QIcon(pm)


class QuotaWidgetController(QObject):
    def __init__(self, app: QApplication, enable_tray: bool = True):
        super().__init__()
        self.app = app
        self.prefs = load_prefs()
        self.payload: dict | None = None
        self.accounts_data: dict | None = None
        self.error_msg: str | None = None
        self.expanded = False
        self.popover_pinned = False
        self.manual_hidden = False
        self._popover_raised_by_user = False
        self._is_sidecar = bool(os.environ.get("ANTIGRAVITY_LS_ADDRESS"))
        self._parent_pid = os.getppid() if self._is_sidecar else 0
        self._seen_antigravity = False
        self._missing_since: float | None = None
        self._ever_connected = False

        self.client = SidecarQuotaClient(self)
        self.tracker = Win32WindowTracker()

        self.mini_card = MiniQuotaCard()
        self.popover = ExpandedPopover()

        self.mini_card.toggled_expand.connect(self.toggle_expand)
        self.mini_card.toggled_dock.connect(self.toggle_dock_mode)
        self.mini_card.drag_started.connect(self._on_mini_drag_started)
        self.mini_card.moved_while_dragging.connect(self._on_mini_drag_moved)
        self.mini_card.drag_finished.connect(self._on_mini_drag_finished)
        self.mini_card.pod_clicked.connect(self._focus_or_launch_app)
        self.mini_card.context_menu_requested.connect(self._show_context_menu)
        self.mini_card.hover_entered.connect(self._on_mini_hover_enter)
        self.mini_card.hover_left.connect(self._on_hover_leave)

        self.popover.closed_requested.connect(self.collapse)
        self.popover.mode_toggled.connect(self.toggle_display_mode)
        self.popover.refresh_requested.connect(self._refresh_all)
        self.popover.pin_toggled.connect(self.toggle_popover_pin)
        self.popover.pod_pin_toggled.connect(self._toggle_pod_pin)
        self.popover.hover_entered.connect(self._on_popover_hover_enter)
        self.popover.hover_left.connect(self._on_hover_leave)
        self.popover.tab_changed.connect(self._on_popover_tab_changed)

        self._hover_open_timer = QTimer(self)
        self._hover_open_timer.setSingleShot(True)
        self._hover_open_timer.timeout.connect(self._on_hover_open_timeout)

        self._hover_close_timer = QTimer(self)
        self._hover_close_timer.setSingleShot(True)
        self._hover_close_timer.timeout.connect(self._on_hover_close_timeout)
        self.popover.size_changed.connect(self._on_popover_size_changed)
        self.popover.capture_account_requested.connect(self._capture_current_account)
        self.popover.import_agy_switch_requested.connect(self._import_agy_switch)
        self.popover.switch_account_requested.connect(self._trigger_account_switch)

        self.client.quota_fetched.connect(self._on_quota_fetched)
        self.client.accounts_fetched.connect(self._on_accounts_fetched)
        self.client.account_action_done.connect(self._on_account_action_done)

        self._menu_refs: list[QObject] = []
        self._tray_menu = QMenu()
        self._tray_menu.aboutToShow.connect(self._refresh_tray_menu)
        self._ctx_menu = QMenu()

        self.tray = None
        if enable_tray and QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(build_tray_icon(), self)
            self.tray.setToolTip("Antigravity 模型用量浮窗")
            self.tray.activated.connect(self._on_tray_activated)
            self._populate_menu(self._tray_menu)
            self.tray.setContextMenu(self._tray_menu)
            self.tray.show()

        self.mini_card.set_pinned_pods(self.prefs.get("pinned_pods") or ["antigravity", "codex"])
        self.popover.set_pinned_pods(self.prefs.get("pinned_pods") or ["antigravity", "codex"])
        self.apply_theme_and_render()

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll_all)
        self.poll_timer.start(POLL_OK_MS)

        self.tick_timer = QTimer(self)
        self.tick_timer.timeout.connect(self._on_ui_tick)
        self.tick_timer.start(TICK_MS)

        self.track_timer = QTimer(self)
        self.track_timer.timeout.connect(self._update_dock_position)

        self.taskbar_timer = QTimer(self)
        self.taskbar_timer.timeout.connect(self._keep_taskbar_topmost)
        self._tb_keepalive_ticks = 0

        self.lifecycle_timer = QTimer(self)
        self.lifecycle_timer.timeout.connect(self._check_app_lifecycle)

    def _poll_all(self) -> None:
        self.client.request_fetch(force=False)
        self.client.request_fetch_accounts()

    def _refresh_all(self) -> None:
        self.client.request_fetch(force=True)
        self.client.request_fetch_accounts()

    def start(self) -> None:
        hwnd = self.tracker.find_antigravity_hwnd()
        vis_cnt = self.tracker.count_visible_titled_windows()
        if hwnd:
            self._seen_antigravity = True
        log_event(
            f"started pid={os.getpid()} ppid={self._parent_pid} sidecar={self._is_sidecar} "
            f"visible_windows={vis_cnt} agy_hwnd={hwnd} dock={self.prefs['dock']}"
        )
        self.lifecycle_timer.start(LIFECYCLE_MS)
        if self.prefs["dock"] == "float":
            self._place_float_initial()
            self.mini_card.show()
            self.tracker.set_topmost(int(self.mini_card.winId()), True)
        elif self.prefs["dock"] == "taskbar":
            self._place_taskbar()
            self.mini_card.show()
            self.tracker.set_topmost(int(self.mini_card.winId()), True)
            self.taskbar_timer.start(600)
        else:
            self.track_timer.start(TRACK_MS)
            self._update_dock_position()
        self._poll_all()

    def _effective_theme(self) -> str:
        return resolve_theme(self.prefs["theme"])

    def _is_user_interacting_with_popover(self) -> bool:
        if not (self.expanded and self.popover.isVisible()):
            return False
        if self.popover.has_active_account_interaction():
            return True
        if QApplication.mouseButtons() != Qt.NoButton and self.popover.underMouse():
            return True
        return False

    def _on_popover_size_changed(self) -> None:
        if self.expanded and self.mini_card.isVisible():
            self._position_popover()

    def apply_theme_and_render(self) -> None:
        theme = self._effective_theme()
        self.mini_card.apply_theme(theme, self.prefs["dock"])
        self.popover.apply_theme(theme)
        self.mini_card.update_data(self.payload, self.error_msg, self.prefs["mode"], self.expanded)
        self.popover.update_data(self.payload, self.prefs["mode"], self.accounts_data)
        if self.expanded and self.mini_card.isVisible():
            self._position_popover()
        self._update_tray_tooltip()

    def _render_passive_update(self) -> None:
        if self._is_user_interacting_with_popover():
            theme = self._effective_theme()
            self.mini_card.apply_theme(theme, self.prefs["dock"])
            self.mini_card.update_data(self.payload, self.error_msg, self.prefs["mode"], self.expanded)
            self.popover.refresh_timestamps(self.payload, self.accounts_data)
            self._update_tray_tooltip()
            return
        self.apply_theme_and_render()

    def _on_quota_fetched(self, payload: dict | None, error_msg: str | None) -> None:
        if payload is not None:
            self.payload = payload
            self.error_msg = error_msg
            self.poll_timer.setInterval(POLL_OK_MS)
            if not self._ever_connected:
                self._ever_connected = True
                cur_id = (payload.get("current") or {}).get("modelId") or "unknown"
                grp_cnt = len(payload.get("groups") or [])
                log_event(f"connected model={cur_id} groups={grp_cnt}")
        else:
            self.error_msg = error_msg or "连接失败"
            self.poll_timer.setInterval(POLL_RETRY_MS if self._ever_connected else POLL_INIT_RETRY_MS)

        self._render_passive_update()
        if self.prefs["dock"] == "docked":
            self._update_dock_position()

    def _on_accounts_fetched(self, accounts_data: dict | None, _error_msg: str | None) -> None:
        if accounts_data is not None:
            self.accounts_data = accounts_data
            self._render_passive_update()

    def _on_popover_tab_changed(self, tab: str) -> None:
        if tab == "accounts":
            self.client.request_fetch_accounts()
        if self.expanded and self.mini_card.isVisible():
            self._position_popover()

    def _capture_current_account(self) -> None:
        self.popover.set_busy_action("capture")
        self.popover.set_action_banner("info", "正在保存当前登录凭据快照...")
        self.client.request_capture_account()

    def _import_agy_switch(self) -> None:
        self.popover.set_busy_action("import_agy")
        self.popover.set_action_banner("info", "正在从 agy-switch 本地库导入账号...")
        self.client.request_import_agy_switch()

    def _trigger_account_switch(self, account_id: str) -> None:
        if not account_id:
            return
        self.popover.set_busy_action("switch")
        self.popover.set_action_banner("info", "正在启动后台切换，Antigravity 即将重启...")
        self.client.request_switch_account(account_id)

    def _confirm_and_switch_from_menu(self, acc: dict) -> None:
        snap = dict(acc or {})
        QTimer.singleShot(0, lambda a=snap: self._exec_menu_switch_confirm(a))

    def _exec_menu_switch_confirm(self, acc: dict) -> None:
        target_id = acc.get("id") or ""
        if not target_id:
            return
        email = acc.get("email") or ""
        label = f"{acc['remark']} ({email})" if acc.get("remark") else email
        box = QMessageBox()
        box.setWindowTitle("确认切换账号并重启？")
        box.setIcon(QMessageBox.Question)
        box.setText(f"即将切换到：{label}")
        box.setInformativeText("切换将关闭并重启 Antigravity（同步更新 agy CLI），进行中的任务不会迁移。")
        confirm_btn = box.addButton("确认重启切换", QMessageBox.AcceptRole)
        cancel_btn = box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(confirm_btn)
        box.setEscapeButton(cancel_btn)
        box.setWindowFlags(box.windowFlags() | Qt.WindowStaysOnTopHint)
        box.show()
        box.raise_()
        box.activateWindow()
        try:
            ctypes.windll.user32.SetForegroundWindow(int(box.winId()))
        except Exception:
            pass
        box.exec_()
        if box.clickedButton() == confirm_btn:
            self._trigger_account_switch(target_id)

    def _on_account_action_done(self, action: str, ok: bool, payload: dict | None, err: str | None) -> None:
        self.popover.set_busy_action(None)
        if payload and isinstance(payload.get("accountsState"), dict):
            self.accounts_data = payload["accountsState"]

        if not ok:
            msg = err or "操作失败"
            self.popover.set_action_banner("error", msg)
            if self.tray:
                self.tray.showMessage("账号操作失败", msg, QSystemTrayIcon.Warning, 4000)
            self.apply_theme_and_render()
            return

        if action == "capture":
            acc = (payload or {}).get("account") or {}
            msg = (
                f"已保存账号快照: {acc.get('email', '')}"
                if (payload or {}).get("created")
                else f"已更新账号凭据: {acc.get('email', '')}"
            )
            self.popover.set_action_banner("success", msg)
        elif action == "import_agy":
            imp = (payload or {}).get("imported", 0)
            upd = (payload or {}).get("updated", 0)
            skp = (payload or {}).get("skipped", 0)
            msg = f"导入完成：新增 {imp}，更新 {upd}" + (f"，跳过 {skp}" if skp else "")
            self.popover.set_action_banner("success", msg)
        elif action == "switch":
            msg = (payload or {}).get("message") or "已启动后台切换，Antigravity 即将重启..."
            self.popover.set_action_banner("info", msg)
            if self.tray:
                self.tray.showMessage("正在切换账号", msg, QSystemTrayIcon.Information, 3000)

        self.apply_theme_and_render()

    def _check_app_lifecycle(self) -> None:
        if self._is_sidecar and self._parent_pid > 0 and not self.tracker.is_process_alive(self._parent_pid):
            log_event("auto_exit reason=parent_ls_exited")
            self.quit_app()
            return

        if not self.prefs.get("auto_exit_with_app", True):
            return

        hwnd = self.tracker.find_antigravity_hwnd()
        if hwnd:
            self._seen_antigravity = True
            self._missing_since = None
        elif self._seen_antigravity:
            now = time.monotonic()
            if self._missing_since is None:
                self._missing_since = now
            elif now - self._missing_since >= APP_GONE_GRACE_SEC:
                log_event("auto_exit reason=antigravity_window_closed")
                self.quit_app()

    def toggle_auto_exit(self) -> None:
        self.prefs["auto_exit_with_app"] = not bool(self.prefs.get("auto_exit_with_app", True))
        save_prefs(self.prefs)

    def _on_ui_tick(self) -> None:
        if self.expanded and self.popover.isVisible() and self.popover._active_tab == "accounts":
            theme = self._effective_theme()
            self.mini_card.apply_theme(theme, self.prefs["dock"])
            if self.popover._theme != theme:
                self.popover.apply_theme(theme)
            self.mini_card.update_data(self.payload, self.error_msg, self.prefs["mode"], self.expanded)
            self.popover.refresh_timestamps(self.payload, self.accounts_data)
            self._update_tray_tooltip()
            return
        self._render_passive_update()

    def _is_cursor_over_widgets(self) -> bool:
        pos = QCursor.pos()
        if self.mini_card.isVisible() and self.mini_card.geometry().adjusted(-6, -6, 6, 6).contains(pos):
            return True
        if self.popover.isVisible() and self.popover.geometry().adjusted(-6, -6, 6, 6).contains(pos):
            return True
        return False

    def _on_mini_hover_enter(self) -> None:
        self._hover_close_timer.stop()
        if not self.expanded:
            self._hover_open_timer.start(150)

    def _on_popover_hover_enter(self) -> None:
        self._hover_close_timer.stop()
        self._hover_open_timer.stop()

    def _on_hover_leave(self) -> None:
        self._hover_open_timer.stop()
        if self.expanded and not self.popover_pinned:
            self._hover_close_timer.start(240)

    def _on_hover_open_timeout(self) -> None:
        if not self.expanded and self.mini_card.isVisible() and self.mini_card.geometry().contains(QCursor.pos()):
            self.expand(via_hover=True)

    def _on_hover_close_timeout(self) -> None:
        if not self.expanded or self.popover_pinned:
            return
        if self._ctx_menu.isVisible() or self.popover.has_active_account_interaction():
            return
        if self._is_cursor_over_widgets():
            return
        self.collapse()

    def toggle_popover_pin(self) -> None:
        self.popover_pinned = not self.popover_pinned
        self.popover.set_pinned(self.popover_pinned)
        if self.popover_pinned:
            self._hover_close_timer.stop()
        elif self.expanded and not self._is_cursor_over_widgets():
            self._hover_close_timer.start(240)

    def _toggle_pod_pin(self, pod_key: str) -> None:
        pods = list(self.prefs.get("pinned_pods") or ["antigravity", "codex"])
        if pod_key in pods:
            if len(pods) <= 1:
                return
            pods.remove(pod_key)
        else:
            # Keep consistent order: antigravity left, codex right
            pods.append(pod_key)
            pods = [k for k in ("antigravity", "codex") if k in pods]
        self.prefs["pinned_pods"] = pods
        save_prefs(self.prefs)
        self.mini_card.set_pinned_pods(pods)
        self.popover.set_pinned_pods(pods)
        if self.prefs["dock"] == "taskbar":
            self._place_taskbar()
        if self.expanded and self.popover.isVisible():
            self._position_popover()

    def expand(self, via_hover: bool = False) -> None:
        if self.expanded:
            return
        self._hover_open_timer.stop()
        self._hover_close_timer.stop()
        self.expanded = True
        self._popover_raised_by_user = not via_hover
        self.popover.set_pinned(self.popover_pinned)
        self.apply_theme_and_render()
        self._position_popover()
        self.popover.show()
        if self.prefs["dock"] in ("float", "taskbar"):
            self.tracker.set_topmost(int(self.mini_card.winId()), True)
            self.tracker.set_topmost(int(self.popover.winId()), True)
            self.popover.raise_()
        else:
            self._update_dock_position()
            self.popover.raise_()
            if not via_hover:
                self.popover.activateWindow()

    def toggle_expand(self) -> None:
        if self.expanded:
            self.collapse()
        else:
            self.expand(via_hover=False)

    def collapse(self) -> None:
        self._hover_open_timer.stop()
        self._hover_close_timer.stop()
        self.expanded = False
        self.popover_pinned = False
        self.popover.set_pinned(False)
        self._popover_raised_by_user = False
        self.popover.hide()
        self.apply_theme_and_render()
        if self.prefs["dock"] == "docked":
            self._update_dock_position()

    def toggle_display_mode(self) -> None:
        self.prefs["mode"] = "remaining" if self.prefs["mode"] == "used" else "used"
        save_prefs(self.prefs)
        self.apply_theme_and_render()

    def set_display_mode(self, mode: str) -> None:
        if self.prefs["mode"] != mode:
            self.prefs["mode"] = mode
            save_prefs(self.prefs)
            self.apply_theme_and_render()

    def toggle_dock_mode(self) -> None:
        new_dock = "float" if self.prefs["dock"] == "docked" else "docked"
        self.set_dock_mode(new_dock)

    def set_dock_mode(self, dock: str) -> None:
        self.prefs["dock"] = dock
        save_prefs(self.prefs)
        self.manual_hidden = False
        if dock == "float":
            self.track_timer.stop()
            self.taskbar_timer.stop()
            self.apply_theme_and_render()
            self._place_float_initial()
            self.mini_card.show()
            self.tracker.set_topmost(int(self.mini_card.winId()), True)
            self.mini_card.raise_()
            if self.expanded:
                self._position_popover()
                self.popover.show()
                self.tracker.set_topmost(int(self.popover.winId()), True)
                self.popover.raise_()
        elif dock == "taskbar":
            self.track_timer.stop()
            self.apply_theme_and_render()
            self._place_taskbar()
            self.mini_card.show()
            self.tracker.set_topmost(int(self.mini_card.winId()), True)
            self.mini_card.raise_()
            self.taskbar_timer.start(600)
            if self.expanded:
                self._position_popover()
                self.popover.show()
                self.tracker.set_topmost(int(self.popover.winId()), True)
                self.popover.raise_()
        else:
            self.taskbar_timer.stop()
            self.tracker.set_topmost(int(self.mini_card.winId()), False)
            self.tracker.set_topmost(int(self.popover.winId()), False)
            self.apply_theme_and_render()
            self.track_timer.start(TRACK_MS)
            self._update_dock_position()

    def set_theme_pref(self, theme_pref: str) -> None:
        self.prefs["theme"] = theme_pref
        save_prefs(self.prefs)
        self.apply_theme_and_render()

    def toggle_visible(self) -> None:
        if self.mini_card.isVisible():
            self.manual_hidden = True
            self.mini_card.hide()
            self.popover.hide()
        else:
            self.manual_hidden = False
            if self.prefs["dock"] == "docked":
                self._update_dock_position()
            else:
                self.mini_card.show()
                self.tracker.set_topmost(int(self.mini_card.winId()), True)
                self.mini_card.raise_()
                if self.expanded:
                    self._position_popover()
                    self.popover.show()
                    self.tracker.set_topmost(int(self.popover.winId()), True)
                    self.popover.raise_()

    def _place_float_initial(self) -> None:
        fp = self.prefs.get("float_pos")
        screen = QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else QRect(0, 0, 1280, 720)
        vis_w, vis_h = self.mini_card.visible_size()

        if fp is not None:
            tx, ty = fp[0], fp[1]
            # Validate target screen
            target_scr = QApplication.screenAt(QPoint(tx + vis_w // 2, ty + vis_h // 2)) or screen
            avail = target_scr.availableGeometry() if target_scr else avail
        else:
            tx = avail.left() + 18 - SHADOW_MARGIN
            ty = avail.bottom() - vis_h - 48 - SHADOW_MARGIN

        tx = max(avail.left() - SHADOW_MARGIN, min(tx, avail.right() - vis_w - SHADOW_MARGIN))
        ty = max(avail.top() - SHADOW_MARGIN, min(ty, avail.bottom() - vis_h - SHADOW_MARGIN))
        self.mini_card.move(tx, ty)

    def _on_mini_drag_moved(self) -> None:
        if self.expanded and self.popover.isVisible():
            self._position_popover()

    def _get_taskbar_geometry(self) -> tuple[QRect, int]:
        """Return (taskbar_rect_logical, safe_right_logical) avoiding tray & third-party taskbar widgets."""
        screen = QApplication.primaryScreen()
        geo = screen.geometry() if screen else QRect(0, 0, 1920, 1080)
        avail = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1044)
        dpr = float(screen.devicePixelRatio()) if screen else 1.0

        # Always derive vertical bounds from Qt logical coordinates to eliminate DPI mismatch.
        tb_top = avail.bottom() + 1
        tb_h = max(30, geo.bottom() - avail.bottom())
        tb_rect = QRect(geo.left(), tb_top, geo.width(), tb_h)
        safe_right = geo.right() - 280

        own_hwnds = set()
        try:
            own_hwnds.add(int(self.mini_card.winId()))
            own_hwnds.add(int(self.popover.winId()))
        except Exception:
            pass

        try:
            u32 = ctypes.windll.user32
            u32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
            u32.FindWindowW.restype = wintypes.HWND
            u32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
            u32.FindWindowExW.restype = wintypes.HWND
            u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            u32.GetWindowRect.restype = wintypes.BOOL
            u32.IsWindowVisible.argtypes = [wintypes.HWND]
            u32.IsWindowVisible.restype = wintypes.BOOL

            tb_hwnd = u32.FindWindowW("Shell_TrayWnd", None)
            if tb_hwnd:
                tray_hwnd = u32.FindWindowExW(tb_hwnd, None, "TrayNotifyWnd", None)
                if tray_hwnd:
                    tr = wintypes.RECT()
                    if u32.GetWindowRect(tray_hwnd, ctypes.byref(tr)) and tr.left > 0:
                        safe_right = min(safe_right, int(round(tr.left / dpr)) - 8)

                # Scan both taskbar child widgets and top-level floating taskbar orbs (e.g., PC Manager memory ball)
                tb_top_phys = int(round(tb_top * dpr))
                tb_bot_phys = int(round((geo.bottom() + 1) * dpr))
                mid_x_logical = geo.left() + int(geo.width() * 0.52)

                EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

                def _check_obstacle(hwnd, _lp):
                    if int(hwnd) in own_hwnds or int(hwnd) == int(tb_hwnd):
                        return True
                    if not u32.IsWindowVisible(hwnd):
                        return True
                    wr = wintypes.RECT()
                    if not u32.GetWindowRect(hwnd, ctypes.byref(wr)):
                        return True
                    w_log = (wr.right - wr.left) / dpr
                    h_log = (wr.bottom - wr.top) / dpr
                    if w_log <= 6 or h_log <= 6 or w_log > 420 or h_log > tb_h + 26:
                        return True
                    # Vertically overlaps the taskbar band
                    if wr.bottom >= tb_top_phys + 4 and wr.top <= tb_bot_phys:
                        left_log = int(round(wr.left / dpr))
                        if mid_x_logical <= left_log <= geo.right():
                            nonlocal safe_right
                            safe_right = min(safe_right, left_log - 8)
                    return True

                cb = EnumProc(_check_obstacle)
                u32.EnumChildWindows(tb_hwnd, cb, 0)
                u32.EnumWindows(cb, 0)
        except Exception:
            pass

        return tb_rect, safe_right

    def _place_taskbar(self, preferred_x: int | None = None) -> None:
        tb_rect, safe_right = self._get_taskbar_geometry()
        self.mini_card.set_taskbar_height(tb_rect.height())
        vis_w, vis_h = self.mini_card.visible_size()

        if preferred_x is not None:
            vis_x = preferred_x + SHADOW_MARGIN
        elif self.prefs.get("taskbar_x") is not None:
            vis_x = int(self.prefs["taskbar_x"])
        else:
            vis_x = safe_right - vis_w

        min_x = tb_rect.left() + 8
        max_x = max(min_x, safe_right - vis_w)
        vis_x = max(min_x, min(vis_x, max_x))

        # Center the full MINI_VIS_HEIGHT box vertically on the taskbar so the inner _tb_height sits cleanly inside.
        vis_y = tb_rect.top() + (tb_rect.height() - vis_h) // 2
        self.mini_card.move(vis_x - SHADOW_MARGIN, vis_y - SHADOW_MARGIN)
        log_event(f"taskbar_place tb=({tb_rect.left()},{tb_rect.top()},{tb_rect.width()}x{tb_rect.height()}) safe_right={safe_right} pos=({vis_x},{vis_y})")

    def _focus_or_launch_app(self, kind: str) -> None:
        self._hover_open_timer.stop()
        if self.expanded and not self.popover_pinned:
            self.collapse()

        if kind == "antigravity":
            hwnd = self.tracker.find_antigravity_hwnd()
            if hwnd and self.tracker.focus_window(hwnd):
                return
            local_app = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
            for cand in [
                local_app / "Antigravity" / "Antigravity.exe",
                local_app / "Programs" / "Antigravity" / "Antigravity.exe",
            ]:
                if cand.is_file():
                    try:
                        subprocess.Popen([str(cand)], close_fds=True)
                    except Exception:
                        pass
                    return
            return

        if kind == "codex":
            hwnd = self.tracker.find_codex_hwnd()
            if hwnd and self.tracker.focus_window(hwnd):
                return
            # Try launching installed Codex Desktop (MSIX App or standalone executable)
            local_app = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
            for cand in [
                local_app / "Programs" / "OpenAI" / "Codex" / "Codex.exe",
                local_app / "Programs" / "Codex" / "Codex.exe",
            ]:
                if cand.is_file():
                    try:
                        subprocess.Popen([str(cand)], close_fds=True)
                    except Exception:
                        pass
                    return
            try:
                # MSIX / Microsoft Store Codex app launch via shell:AppsFolder
                subprocess.Popen(
                    ["explorer.exe", r"shell:AppsFolder\OpenAI.Codex_2p2nqsd0c76g0!App"],
                    close_fds=True,
                )
            except Exception:
                pass

    def _on_mini_drag_started(self) -> None:
        self._hover_open_timer.stop()
        self._hover_close_timer.stop()
        if self.expanded and not self.popover_pinned:
            self.collapse()

    def _keep_taskbar_topmost(self) -> None:
        if self.prefs["dock"] != "taskbar" or self.manual_hidden or self.mini_card._dragging:
            return
        if not self.mini_card.isVisible():
            return
        self.tracker.set_topmost(int(self.mini_card.winId()), True)
        if self.expanded and self.popover.isVisible():
            self.tracker.set_topmost(int(self.popover.winId()), True)

        # Every ~3s, re-check right taskbar boundary to yield smoothly if tray icons expand
        self._tb_keepalive_ticks += 1
        if self._tb_keepalive_ticks >= 5:
            self._tb_keepalive_ticks = 0
            tb_rect, safe_right = self._get_taskbar_geometry()
            vis_w, vis_h = self.mini_card.visible_size()
            cur_vis_x = self.mini_card.x() + SHADOW_MARGIN
            max_x = max(tb_rect.left() + 8, safe_right - vis_w)
            if cur_vis_x > max_x:
                vis_y = tb_rect.top() + (tb_rect.height() - vis_h) // 2
                self.mini_card.move(max_x - SHADOW_MARGIN, vis_y - SHADOW_MARGIN)
                if self.expanded and self.popover.isVisible():
                    self._position_popover()

    def _on_mini_drag_finished(self, x: int, y: int) -> None:
        if self.prefs["dock"] not in ("float", "taskbar"):
            return
        tb_rect, _tray_left = self._get_taskbar_geometry()
        vis_w, vis_h = self.mini_card.visible_size()
        center_y = y + SHADOW_MARGIN + vis_h // 2

        if center_y >= tb_rect.top() - 4:
            # Dropped into taskbar -> seamlessly merge into taskbar
            self.prefs["dock"] = "taskbar"
            self._place_taskbar(preferred_x=x)
            self.prefs["taskbar_x"] = self.mini_card.x() + SHADOW_MARGIN
            save_prefs(self.prefs)
            self.apply_theme_and_render()
            self.tracker.set_topmost(int(self.mini_card.winId()), True)
            self.taskbar_timer.start(600)
        else:
            # Lifted out of taskbar -> restore floating capsule
            self.prefs["dock"] = "float"
            self.prefs["float_pos"] = [x, y]
            save_prefs(self.prefs)
            self.taskbar_timer.stop()
            self.apply_theme_and_render()
            self.tracker.set_topmost(int(self.mini_card.winId()), True)

        if self.expanded and self.popover.isVisible():
            self._position_popover()

    def _position_popover(self) -> None:
        mini_geo = self.mini_card.geometry()
        screen = QApplication.screenAt(mini_geo.center()) or QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else QRect(0, 0, 1280, 720)

        pop_w, pop_h = self.popover.visible_size()
        mini_vis_left = mini_geo.left() + SHADOW_MARGIN
        mini_vis_right = mini_geo.right() - SHADOW_MARGIN
        mini_vis_bottom = mini_geo.bottom() - SHADOW_MARGIN

        if self.prefs["dock"] == "taskbar":
            mini_vis_w, _ = self.mini_card.visible_size()
            pop_vis_left = mini_vis_left + (mini_vis_w - pop_w) // 2
            pop_vis_left = max(avail.left() + 6, min(pop_vis_left, avail.right() - pop_w - 6))
            pop_vis_top = avail.bottom() - pop_h - 6
            pop_vis_top = max(avail.top(), pop_vis_top)
            self.popover.move(pop_vis_left - SHADOW_MARGIN, pop_vis_top - SHADOW_MARGIN)
            return

        pop_vis_left = mini_vis_right + 8
        if pop_vis_left + pop_w > avail.right():
            pop_vis_left = mini_vis_left - 8 - pop_w

        pop_vis_left = max(avail.left(), min(pop_vis_left, avail.right() - pop_w))
        pop_vis_top = mini_vis_bottom - pop_h
        pop_vis_top = max(avail.top(), min(pop_vis_top, avail.bottom() - pop_h))

        self.popover.move(pop_vis_left - SHADOW_MARGIN, pop_vis_top - SHADOW_MARGIN)

    def _update_dock_position(self) -> None:
        if self.prefs["dock"] != "docked":
            return
        if self.manual_hidden:
            self.mini_card.hide()
            self.popover.hide()
            return

        hwnd = self.tracker.find_antigravity_hwnd()
        vis_w, vis_h = self.mini_card.visible_size()

        if not hwnd:
            self.tracker.set_topmost(int(self.mini_card.winId()), False)
            self.tracker.set_topmost(int(self.popover.winId()), False)
            if self._seen_antigravity:
                self.mini_card.hide()
                self.popover.hide()
                return
            screen = QApplication.primaryScreen()
            avail = screen.availableGeometry() if screen else QRect(0, 0, 1280, 720)
            tx = avail.left() + 10 - SHADOW_MARGIN
            ty = avail.bottom() - vis_h - 48 - SHADOW_MARGIN
            if self.mini_card.pos() != QPoint(tx, ty):
                self.mini_card.move(tx, ty)
            if not self.mini_card.isVisible():
                self.mini_card.show()
            if self.expanded:
                self._position_popover()
                if not self.popover.isVisible():
                    self.popover.show()
            return

        if self.tracker.is_iconic(hwnd) or self.tracker.is_window_cloaked(hwnd):
            self.mini_card.hide()
            self.popover.hide()
            return

        agy_rect = self.tracker.get_window_rect(hwnd)
        if not agy_rect:
            return
        agy_phys_l, _agy_phys_t, _agy_phys_r, agy_phys_b = agy_rect

        agy_log_l, agy_log_b, screen, dpr, mon_origin = self.tracker.physical_to_logical(
            hwnd, agy_phys_l, agy_phys_b
        )
        avail = screen.availableGeometry() if screen else QRect(0, 0, 1280, 720)

        target_x = agy_log_l + 10 - SHADOW_MARGIN
        target_y = agy_log_b - vis_h - 46 - SHADOW_MARGIN

        target_x = max(avail.left() - SHADOW_MARGIN, min(target_x, avail.right() - vis_w - SHADOW_MARGIN))
        target_y = max(avail.top() - SHADOW_MARGIN, min(target_y, avail.bottom() - vis_h - SHADOW_MARGIN))

        geo = screen.geometry() if screen else QRect(0, 0, 1920, 1080)
        card_phys_l = mon_origin[0] + round((target_x + SHADOW_MARGIN - geo.left()) * dpr)
        card_phys_t = mon_origin[1] + round((target_y + SHADOW_MARGIN - geo.top()) * dpr)
        card_phys_r = card_phys_l + round(vis_w * dpr)
        card_phys_b = card_phys_t + round(vis_h * dpr)
        card_phys_rect = (card_phys_l, card_phys_t, card_phys_r, card_phys_b)

        fg_hwnd = self.tracker.get_foreground_hwnd()
        agy_pid = self.tracker.get_pid(hwnd)
        fg_pid = self.tracker.get_pid(fg_hwnd) if fg_hwnd else 0

        if self._popover_raised_by_user and fg_pid and fg_pid not in (self.tracker.own_pid, agy_pid):
            if not (self.popover.underMouse() or self.popover.has_active_account_interaction()):
                self._popover_raised_by_user = False

        raise_for_popover = bool(
            self.expanded
            and (
                self._popover_raised_by_user
                or fg_pid == self.tracker.own_pid
                or self.popover.has_active_account_interaction()
            )
        )

        if self.mini_card.pos() != QPoint(target_x, target_y):
            self.mini_card.move(target_x, target_y)

        mini_hwnd = int(self.mini_card.winId())
        pop_hwnd = int(self.popover.winId()) if self.expanded else 0

        should_show = self.tracker.sync_docked_zorder(
            mini_hwnd=mini_hwnd,
            pop_hwnd=pop_hwnd,
            pop_visible=self.expanded,
            agy_hwnd=hwnd,
            card_phys_rect=card_phys_rect,
            raise_for_popover=raise_for_popover,
        )

        if not should_show:
            self.mini_card.hide()
            self.popover.hide()
            return

        newly_shown = False
        if not self.mini_card.isVisible():
            self.mini_card.show()
            newly_shown = True
        if self.expanded:
            self._position_popover()
            if not self.popover.isVisible():
                self.popover.show()
                newly_shown = True

        if newly_shown:
            self.tracker.sync_docked_zorder(
                mini_hwnd=mini_hwnd,
                pop_hwnd=pop_hwnd,
                pop_visible=self.expanded,
                agy_hwnd=hwnd,
                card_phys_rect=card_phys_rect,
                raise_for_popover=raise_for_popover,
            )

    def _update_tray_tooltip(self) -> None:
        if not self.tray:
            return
        cur = (self.payload or {}).get("current") or {}
        live_email = (
            ((self.accounts_data or {}).get("liveAccount") or {}).get("email")
            or ((self.payload or {}).get("account") or {}).get("email")
            or ""
        )
        if not cur:
            tip = f"Antigravity 模型用量浮窗\n{live_email}" if live_email else "Antigravity 模型用量浮窗"
            self.tray.setToolTip(tip)
            return
        label = cur.get("label") or "当前模型"
        fh = cur.get("fiveHour")
        wk = cur.get("weekly")
        is_used = self.prefs["mode"] == "used"
        key = "usedPercent" if is_used else "remainingPercent"
        prefix = "已用" if is_used else "剩余"
        fh_txt = f"5h {prefix} {fmt_pct(fh.get(key))}" if fh else "5h 仅周限额"
        wk_txt = f"1周 {prefix} {fmt_pct(wk.get(key))}" if wk else ""
        head = f"{live_email} · {label}" if live_email else label
        self.tray.setToolTip(f"{head}\n{fh_txt} | {wk_txt}".strip(" |"))

    def _style_menu(self, menu: QMenu) -> None:
        pal = PALETTES[self._effective_theme()]
        menu.setWindowFlags(menu.windowFlags() | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        menu.setAttribute(Qt.WA_TranslucentBackground, True)
        menu.setStyleSheet(
            f"QMenu {{ background: {pal['card_bg']}; color: {pal['fg']}; "
            f"border: 1px solid {pal['border']}; border-radius: 10px; padding: 5px; font-size: 11px; }}"
            f"QMenu::item {{ padding: 5px 18px 5px 12px; border-radius: 6px; margin: 1px 0px; }}"
            f"QMenu::item:selected {{ background: {pal['accent_soft']}; color: {pal['fg']}; }}"
            f"QMenu::item:disabled {{ color: {pal['muted']}; }}"
            f"QMenu::separator {{ height: 1px; background: {pal['border']}; margin: 4px 8px; }}"
            f"QMenu::indicator {{ width: 12px; height: 12px; margin-left: 4px; }}"
        )

    def _populate_menu(self, menu: QMenu) -> QMenu:
        menu.clear()
        self._style_menu(menu)
        refs: list[QObject] = []

        # 1. Position mode (compact 3-way)
        dock_group = QActionGroup(menu)
        refs.append(dock_group)
        for label, key in [
            ("合并到任务栏", "taskbar"),
            ("桌面自由悬浮", "float"),
            ("吸附 Antigravity 左下角", "docked"),
        ]:
            act = QAction(label, menu, checkable=True)
            act.setChecked(self.prefs["dock"] == key)
            act.triggered.connect(lambda _checked=False, k=key: self.set_dock_mode(k))
            dock_group.addAction(act)
            menu.addAction(act)
            refs.append(act)

        menu.addSeparator()

        # 2. One-click display mode toggle
        next_mode_label = "切换为剩余视角" if self.prefs["mode"] == "used" else "切换为已用视角"
        act_mode = QAction(next_mode_label, menu)
        act_mode.triggered.connect(self.toggle_display_mode)
        menu.addAction(act_mode)
        refs.append(act_mode)

        # 3. Preferences submenu (low-frequency settings grouped cleanly)
        pref_menu = menu.addMenu("偏好设置")
        self._style_menu(pref_menu)
        refs.append(pref_menu)

        theme_menu = pref_menu.addMenu("外观主题")
        self._style_menu(theme_menu)
        refs.append(theme_menu)
        theme_group = QActionGroup(theme_menu)
        refs.append(theme_group)
        for label, key in [("跟随系统", "system"), ("浅色", "light"), ("深色", "dark")]:
            act = QAction(label, theme_menu, checkable=True)
            act.setChecked(self.prefs["theme"] == key)
            act.triggered.connect(lambda _checked=False, k=key: self.set_theme_pref(k))
            theme_group.addAction(act)
            theme_menu.addAction(act)
            refs.append(act)

        act_auto_exit = QAction("随 Antigravity 关闭退出", pref_menu, checkable=True)
        act_auto_exit.setChecked(bool(self.prefs.get("auto_exit_with_app", True)))
        act_auto_exit.triggered.connect(self.toggle_auto_exit)
        pref_menu.addAction(act_auto_exit)
        refs.append(act_auto_exit)

        toggle_vis = QAction("隐藏浮窗" if self.mini_card.isVisible() else "显示浮窗", pref_menu)
        toggle_vis.triggered.connect(self.toggle_visible)
        pref_menu.addAction(toggle_vis)
        refs.append(toggle_vis)

        menu.addSeparator()

        # 4. Refresh & Quit
        act_refresh = QAction("立即刷新", menu)
        act_refresh.triggered.connect(self._refresh_all)
        menu.addAction(act_refresh)
        refs.append(act_refresh)

        act_quit = QAction("退出", menu)
        act_quit.triggered.connect(self.quit_app)
        menu.addAction(act_quit)
        refs.append(act_quit)

        self._menu_refs = refs
        return menu

    def _refresh_tray_menu(self) -> None:
        self._populate_menu(self._tray_menu)

    def _build_menu(self) -> QMenu:
        menu = QMenu()
        return self._populate_menu(menu)

    def _show_context_menu(self, global_pos: QPoint) -> None:
        self._hover_open_timer.stop()
        if self.expanded and not self.popover_pinned:
            self.collapse()
        self._populate_menu(self._ctx_menu)
        self._ctx_menu.exec_(global_pos)

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger:
            if self.mini_card.isVisible():
                if self.prefs["dock"] == "docked":
                    if self.expanded:
                        self._popover_raised_by_user = True
                    self._update_dock_position()
                else:
                    self.tracker.set_topmost(int(self.mini_card.winId()), True)
                    self.mini_card.raise_()
                    if self.expanded:
                        self.tracker.set_topmost(int(self.popover.winId()), True)
                        self.popover.raise_()
            else:
                self.manual_hidden = False
                if self.prefs["dock"] == "docked":
                    self._update_dock_position()
                else:
                    self.mini_card.show()
                    self.tracker.set_topmost(int(self.mini_card.winId()), True)
                    self.mini_card.raise_()

    def quit_app(self) -> None:
        self.lifecycle_timer.stop()
        self.track_timer.stop()
        self.poll_timer.stop()
        self.tick_timer.stop()
        self.popover.hide()
        self.mini_card.hide()
        if self.tray:
            self.tray.hide()
        self.app.quit()


# ---------------------------------------------------------------------------
# Offscreen / Live Self-Test Mode
# ---------------------------------------------------------------------------
def run_self_test() -> int:
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)

    client = SidecarQuotaClient()
    ports = client.discover_ports()
    payload, err, used_port = client.fetch_quota_sync(force=False)
    if not payload or not payload.get("groups"):
        print(json.dumps({
            "ok": False,
            "ports": ports,
            "error": err,
        }, ensure_ascii=False))
        return 1

    live_accounts, _acc_err, _ = client.fetch_accounts_sync()
    if not live_accounts or not live_accounts.get("accounts"):
        accounts_fixture = {
            "schemaVersion": 1,
            "liveAccount": {
                "email": "test@example.com",
                "tier": "Google AI Pro",
                "isSaved": True,
            },
            "agySwitchDetected": True,
            "agySwitchCount": 2,
            "accounts": [
                {
                    "id": "self-test-1",
                    "email": "test@example.com",
                    "remark": "主力账号",
                    "tier": "Google AI Pro",
                    "isCurrent": True,
                    "hasCredential": True,
                    "lastQuota": {
                        "fetchedAt": payload.get("fetchedAt"),
                        "groups": [
                            {
                                "displayName": "Gemini",
                                "fiveHourUsed": 12.0,
                                "fiveHourRemaining": 88.0,
                                "weeklyUsed": 24.0,
                                "weeklyRemaining": 76.0,
                            }
                        ],
                    },
                },
                {
                    "id": "self-test-2",
                    "email": "backup@example.com",
                    "remark": "备用账号",
                    "tier": "Free",
                    "isCurrent": False,
                    "hasCredential": True,
                    "lastQuota": {
                        "fetchedAt": payload.get("fetchedAt"),
                        "groups": [
                            {
                                "displayName": "Claude/GPT",
                                "fiveHourUsed": 5.0,
                                "fiveHourRemaining": 95.0,
                                "weeklyUsed": 10.0,
                                "weeklyRemaining": 90.0,
                            }
                        ],
                    },
                },
            ],
        }
    else:
        accounts_fixture = live_accounts

    mini = MiniQuotaCard()
    pop = ExpandedPopover()

    renders = []
    for theme in ("light", "dark"):
        for mode in ("used", "remaining"):
            dock = "docked" if mode == "used" else "float"
            mini.apply_theme(theme, dock)
            pop.apply_theme(theme)
            pop.set_active_tab("quota")
            mini.update_data(payload, err, mode, expanded=True)
            pop.update_data(payload, mode, accounts_fixture)
            mini.show()
            pop.show()
            app.processEvents()

            pm_mini = mini.grab()
            pm_pop_quota = pop.grab()
            if pm_mini.isNull() or pm_mini.width() <= 0 or pm_mini.height() <= 0:
                print(json.dumps({"ok": False, "error": "MiniQuotaCard grab failed"}, ensure_ascii=False))
                return 1
            if pm_pop_quota.isNull() or pm_pop_quota.width() <= 0 or pm_pop_quota.height() <= 0:
                print(json.dumps({"ok": False, "error": "ExpandedPopover quota tab grab failed"}, ensure_ascii=False))
                return 1

            pop.set_active_tab("accounts")
            app.processEvents()
            pm_pop_accounts = pop.grab()
            if pm_pop_accounts.isNull() or pm_pop_accounts.width() <= 0 or pm_pop_accounts.height() <= 0:
                print(json.dumps({"ok": False, "error": "ExpandedPopover accounts tab grab failed"}, ensure_ascii=False))
                return 1

            renders.append({
                "theme": theme,
                "mode": mode,
                "dock": dock,
                "miniSize": [pm_mini.width(), pm_mini.height()],
                "popoverSize": [pm_pop_quota.width(), pm_pop_quota.height()],
                "popoverAccountsSize": [pm_pop_accounts.width(), pm_pop_accounts.height()],
            })

    tray_icon = build_tray_icon()
    pop.hide()
    mini.hide()

    # End-to-end controller switch interaction & tray menu survival verification (mocked HTTP)
    import gc
    import weakref

    switch_fixture_accounts = accounts_fixture
    if not any(
        not a.get("isCurrent") and a.get("hasCredential")
        for a in (switch_fixture_accounts.get("accounts") or [])
    ):
        switch_fixture_accounts = {
            "schemaVersion": 1,
            "liveAccount": {"email": "test@example.com", "tier": "Pro", "isSaved": True},
            "accounts": [
                {"id": "st-cur", "email": "test@example.com", "isCurrent": True, "hasCredential": True},
                {"id": "st-alt", "email": "alt@example.com", "isCurrent": False, "hasCredential": True},
            ],
        }

    ctrl = QuotaWidgetController(app, enable_tray=False)
    ctrl.poll_timer.stop()
    ctrl.tick_timer.stop()
    ctrl.track_timer.stop()
    ctrl.lifecycle_timer.stop()
    dispatched_switch_ids: list[str] = []
    ctrl.client.request_switch_account = lambda aid: dispatched_switch_ids.append(aid)
    ctrl.client.request_fetch_accounts = lambda: None
    ctrl.payload = payload
    ctrl.accounts_data = switch_fixture_accounts
    ctrl.apply_theme_and_render()

    screen = QApplication.primaryScreen()
    avail = screen.availableGeometry() if screen else QRect(0, 0, 1280, 720)
    vis_w, vis_h = ctrl.mini_card.visible_size()
    ctrl.mini_card.move(avail.left() + 10 - SHADOW_MARGIN, avail.bottom() - vis_h - 46 - SHADOW_MARGIN)
    ctrl.mini_card.show()
    ctrl.toggle_expand()
    app.processEvents()

    ctrl.popover._select_tab("accounts")
    app.processEvents()

    sw_buttons = [b for b in ctrl.popover.groups_host.findChildren(QPushButton) if b.text() == "切换" and b.isEnabled()]
    if not sw_buttons:
        print(json.dumps({"ok": False, "error": "No enabled switch button rendered in accounts tab"}, ensure_ascii=False))
        return 1
    sw_buttons[0].click()
    app.processEvents()

    # Simulate an async /api/accounts refresh arriving while confirmation is open (must NOT wipe confirm button!)
    ctrl._on_accounts_fetched(switch_fixture_accounts, None)
    ctrl._on_ui_tick()
    app.processEvents()

    pop_vis_bottom = ctrl.popover.geometry().bottom() - SHADOW_MARGIN
    popover_in_bounds = pop_vis_bottom <= avail.bottom() + 2

    ok_buttons = [
        b for b in ctrl.popover.groups_host.findChildren(QPushButton)
        if b.text() == "确认重启切换" and b.isEnabled()
    ]
    if not ok_buttons:
        print(json.dumps({"ok": False, "error": "Confirm switch button missing or clobbered by passive refresh"}, ensure_ascii=False))
        return 1
    ok_buttons[0].click()
    app.processEvents()

    if not dispatched_switch_ids:
        print(json.dumps({"ok": False, "error": "Confirm switch click did not dispatch request_switch_account"}, ensure_ascii=False))
        return 1

    # Verify persistent tray menu survives right-click activation + GC
    test_tray = QSystemTrayIcon()
    ctrl._populate_menu(ctrl._tray_menu)
    test_tray.setContextMenu(ctrl._tray_menu)
    menu_ref = weakref.ref(test_tray.contextMenu())
    ctrl._on_tray_activated(QSystemTrayIcon.Context)
    ctrl._refresh_tray_menu()
    gc.collect()
    tray_menu_survived = menu_ref() is not None and len(ctrl._tray_menu.actions()) > 0
    if not tray_menu_survived:
        print(json.dumps({"ok": False, "error": "Tray contextMenu destroyed during activation/GC"}, ensure_ascii=False))
        return 1

    ctrl.popover.hide()
    ctrl.mini_card.hide()

    cur = payload.get("current") or {}
    report = {
        "ok": True,
        "discoveredPorts": ports,
        "connectedPort": used_port,
        "accountsTabRendered": True,
        "controllerSwitchVerified": True,
        "popoverInBoundsAfterConfirm": popover_in_bounds,
        "trayMenuSurvivedRightClick": tray_menu_survived,
        "currentModel": {
            "modelId": cur.get("modelId"),
            "label": cur.get("label"),
            "source": cur.get("source"),
            "fiveHourUsed": (cur.get("fiveHour") or {}).get("usedPercent"),
            "weeklyUsed": (cur.get("weekly") or {}).get("usedPercent"),
        },
        "groupCount": len(payload.get("groups") or []),
        "trayIconEmpty": tray_icon.isNull(),
        "renders": renders,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return run_self_test()

    mutex_handle = acquire_single_instance_mutex()
    if mutex_handle is None:
        for _ in range(8):
            time.sleep(0.3)
            mutex_handle = acquire_single_instance_mutex()
            if mutex_handle is not None:
                break
    if mutex_handle is None:
        log_event("skipped reason=already_running")
        return 0

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei UI", 9))
    app.setQuitOnLastWindowClosed(False)

    controller = QuotaWidgetController(app, enable_tray=True)
    controller.start()
    rc = app.exec_()

    try:
        if isinstance(mutex_handle, int) and mutex_handle:
            ctypes.windll.kernel32.CloseHandle(mutex_handle)
    except Exception:
        pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
