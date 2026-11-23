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
import os
import re
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
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt5.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
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
SIDECAR_LOG = SIDECAR_BASE / "logs" / "sidecar.log"
PREFS_FILE = SIDECAR_BASE / "data" / "widget_prefs.json"
WIDGET_LOG = SIDECAR_BASE / "data" / "widget.log"

MUTEX_NAME = "Local\\AntigravityQuotaFloatingWidget_v1"
ID_RE = re.compile(r"^[0-9a-f-]{8,}$", re.I)
LOG_PORT_RE = re.compile(r"\[sidecar\]\s+listening\s+on\s+127\.0\.0\.1:(\d+)")
TIER_SUFFIX_RE = re.compile(r"\s*\((Low|Medium|High|XHigh|Extra High|Minimal)\)\s*$", re.I)

SHADOW_MARGIN = 10
MINI_VIS_WIDTH = 220
POPOVER_VIS_WIDTH = 332

POLL_OK_MS = 30_000
POLL_RETRY_MS = 5_000
POLL_INIT_RETRY_MS = 2_000
TRACK_MS = 150
TICK_MS = 30_000
LIFECYCLE_MS = 1_500
APP_GONE_GRACE_SEC = 3.0

DEFAULT_PREFS = {
    "mode": "used",             # "used" | "remaining"
    "dock": "docked",           # "docked" | "float"
    "theme": "system",          # "system" | "light" | "dark"
    "float_pos": None,          # [x, y] or None
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
        "card_bg": "#21242a",
        "card_alt": "#282c34",
        "fg": "#f3f4f6",
        "muted": "#9ca3af",
        "border": "#2f343e",
        "track": "#323741",
        "accent": "#3b82f6",
        "accent_soft": "rgba(59, 130, 246, 0.20)",
        "accent_fg": "#ffffff",
        "green": "#22c55e",
        "amber": "#f59e0b",
        "red": "#ef4444",
        "hover": "rgba(255, 255, 255, 0.08)",
        "shadow_docked": QColor(0, 0, 0, 115),
        "shadow_float": QColor(0, 0, 0, 170),
    },
}


# ---------------------------------------------------------------------------
# Preferences & System Theme Helpers
# ---------------------------------------------------------------------------
def load_prefs() -> dict:
    prefs = dict(DEFAULT_PREFS)
    try:
        if PREFS_FILE.exists():
            raw = json.loads(PREFS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                if raw.get("mode") in ("used", "remaining"):
                    prefs["mode"] = raw["mode"]
                if raw.get("dock") in ("docked", "float"):
                    prefs["dock"] = raw["dock"]
                if raw.get("theme") in ("system", "light", "dark"):
                    prefs["theme"] = raw["theme"]
                fp = raw.get("float_pos")
                if isinstance(fp, list) and len(fp) == 2 and all(isinstance(v, (int, float)) for v in fp):
                    prefs["float_pos"] = [int(fp[0]), int(fp[1])]
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

    def __init__(self, parent=None):
        super().__init__(parent)
        self._conv_cache = (0.0, "")
        self._lock = threading.Lock()
        self._fetching = False

    def discover_ports(self) -> list[int]:
        ports: list[int] = []
        # 1. Primary: runtime.json written by main.mjs
        try:
            if RUNTIME_FILE.exists():
                rt = json.loads(RUNTIME_FILE.read_text(encoding="utf-8"))
                p = rt.get("port")
                if isinstance(p, int) and 0 < p < 65536:
                    ports.append(p)
        except Exception:
            pass

        # 2. Fallback: scan sidecar.log from the bottom
        try:
            if SIDECAR_LOG.exists():
                lines = SIDECAR_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
                for line in reversed(lines):
                    m = LOG_PORT_RE.search(line)
                    if m:
                        p = int(m.group(1))
                        if 0 < p < 65536 and p not in ports:
                            ports.append(p)
                        break
        except Exception:
            pass

        # 3. Fallback: scan any installed plugin directory name under sidecar_data/*/quota_panel
        if not ports:
            sidecar_root = APP_DATA / "sidecar_data"
            try:
                for rt_file in sidecar_root.glob("*/quota_panel/data/runtime.json"):
                    try:
                        rt = json.loads(rt_file.read_text(encoding="utf-8"))
                        p = rt.get("port")
                        if isinstance(p, int) and 0 < p < 65536 and p not in ports:
                            ports.append(p)
                    except Exception:
                        pass
                for log_file in sidecar_root.glob("*/quota_panel/logs/sidecar.log"):
                    try:
                        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
                        for line in reversed(lines):
                            m = LOG_PORT_RE.search(line)
                            if m:
                                p = int(m.group(1))
                                if 0 < p < 65536 and p not in ports:
                                    ports.append(p)
                                break
                    except Exception:
                        pass
            except Exception:
                pass

        return ports

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

    def fetch_quota_sync(self, force: bool = False) -> tuple[dict | None, str | None, int | None]:
        ports = self.discover_ports()
        if not ports:
            return None, "未检测到 Sidecar 端口，请确认 Antigravity 已启动", None

        conv_id = self.find_latest_conversation_id()
        qs = {}
        if conv_id:
            qs["conversationId"] = conv_id
        if force:
            qs["force"] = "1"
        query = urllib.parse.urlencode(qs)

        last_err = None
        for port in ports:
            url = f"http://127.0.0.1:{port}/api/quota"
            if query:
                url = f"{url}?{query}"
            try:
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    if resp.status != 200:
                        last_err = f"HTTP {resp.status}"
                        continue
                    body = resp.read().decode("utf-8")
                    payload = json.loads(body)
                    return payload, payload.get("error"), port
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                last_err = str(exc)

        return None, last_err or "无法连接到本地 Sidecar 服务", None

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
    MONITOR_DEFAULTTONEAREST = 2
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def __init__(self):
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32
        self.dwmapi = getattr(ctypes.windll, "dwmapi", None)
        self.own_pid = os.getpid()

        self.WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        self.user32.EnumWindows.argtypes = [self.WNDENUMPROC, wintypes.LPARAM]
        self.user32.EnumWindows.restype = wintypes.BOOL
        self.user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user32.IsWindowVisible.restype = wintypes.BOOL
        self.user32.IsIconic.argtypes = [wintypes.HWND]
        self.user32.IsIconic.restype = wintypes.BOOL
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

    def get_process_exe_name(self, pid: int) -> str:
        if not pid:
            return ""
        handle = self.kernel32.OpenProcess(self.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(512)
            buf = ctypes.create_unicode_buffer(512)
            if self.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return Path(buf.value).stem.lower()
            return ""
        finally:
            self.kernel32.CloseHandle(handle)

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


class MiniQuotaCard(QWidget):
    """Compact always-on-top mini card docked to Antigravity bottom-left or floating."""

    toggled_expand = pyqtSignal()
    toggled_dock = pyqtSignal()
    moved_while_dragging = pyqtSignal()
    drag_finished = pyqtSignal(int, int)
    context_menu_requested = pyqtSignal(QPoint)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Antigravity Quota")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setCursor(Qt.PointingHandCursor)

        self._dock_mode = "docked"
        self._theme = "light"
        self._mode = "used"
        self._expanded = False
        self._dragging = False
        self._drag_moved = False
        self._press_global = QPoint()
        self._press_win = QPoint()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN)
        outer.setSpacing(0)

        self.surface = QFrame()
        self.surface.setObjectName("miniSurface")
        self.surface.setFixedWidth(MINI_VIS_WIDTH)
        outer.addWidget(self.surface)

        root = QVBoxLayout(self.surface)
        root.setContentsMargins(10, 9, 10, 8)
        root.setSpacing(6)

        # Header row
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(5)

        self.grip_lbl = QLabel("::")
        self.grip_lbl.setToolTip("按住可拖拽悬浮位置")
        self.grip_lbl.setVisible(False)
        head.addWidget(self.grip_lbl)

        self.dot_lbl = QLabel()
        self.dot_lbl.setFixedSize(8, 8)
        head.addWidget(self.dot_lbl)

        self.model_lbl = QLabel("加载中...")
        self.model_lbl.setMinimumWidth(40)
        self.model_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        head.addWidget(self.model_lbl, 1)

        self.tier_lbl = QLabel("")
        self.tier_lbl.setVisible(False)
        head.addWidget(self.tier_lbl)

        self.dock_btn = QPushButton("钉")
        self.dock_btn.setFixedSize(20, 18)
        self.dock_btn.setToolTip("切换 吸附左下角 / 自由悬浮")
        self.dock_btn.setCursor(Qt.PointingHandCursor)
        self.dock_btn.clicked.connect(self.toggled_dock.emit)
        head.addWidget(self.dock_btn)

        root.addLayout(head)

        # Status notice (used while connecting / error without cache)
        self.status_lbl = QLabel("正在连接本地配额服务...")
        self.status_lbl.setWordWrap(True)
        root.addWidget(self.status_lbl)

        # 5h meter container
        self.fh_widget = QWidget()
        fh_lay = QVBoxLayout(self.fh_widget)
        fh_lay.setContentsMargins(0, 0, 0, 0)
        fh_lay.setSpacing(3)
        fh_top = QHBoxLayout()
        fh_top.setContentsMargins(0, 0, 0, 0)
        fh_top.setSpacing(4)
        self.fh_title = QLabel("5h")
        self.fh_val = QLabel("--")
        self.fh_cd = QLabel("")
        fh_top.addWidget(self.fh_title)
        fh_top.addStretch(1)
        fh_top.addWidget(self.fh_val)
        fh_top.addWidget(self.fh_cd)
        self.fh_bar = QuotaProgressBar()
        fh_lay.addLayout(fh_top)
        fh_lay.addWidget(self.fh_bar)
        root.addWidget(self.fh_widget)

        self.fh_note = QLabel("5h: 仅周限额")
        self.fh_note.setVisible(False)
        root.addWidget(self.fh_note)

        # Weekly meter container
        self.wk_widget = QWidget()
        wk_lay = QVBoxLayout(self.wk_widget)
        wk_lay.setContentsMargins(0, 0, 0, 0)
        wk_lay.setSpacing(3)
        wk_top = QHBoxLayout()
        wk_top.setContentsMargins(0, 0, 0, 0)
        wk_top.setSpacing(4)
        self.wk_title = QLabel("1周")
        self.wk_val = QLabel("--")
        self.wk_cd = QLabel("")
        wk_top.addWidget(self.wk_title)
        wk_top.addStretch(1)
        wk_top.addWidget(self.wk_val)
        wk_top.addWidget(self.wk_cd)
        self.wk_bar = QuotaProgressBar()
        wk_lay.addLayout(wk_top)
        wk_lay.addWidget(self.wk_bar)
        root.addWidget(self.wk_widget)

        # Divider + Footer cue
        self.divider = QFrame()
        self.divider.setFixedHeight(1)
        root.addWidget(self.divider)

        foot = QHBoxLayout()
        foot.setContentsMargins(0, 1, 0, 0)
        foot.setSpacing(4)
        self.hint_lbl = QLabel("点击展开全部模型")
        self.chevron_lbl = QLabel(">")
        foot.addWidget(self.hint_lbl)
        foot.addStretch(1)
        foot.addWidget(self.chevron_lbl)
        root.addLayout(foot)

        self.apply_theme("light", "docked")

    def visible_size(self) -> tuple[int, int]:
        return self.surface.width(), self.surface.height()

    def apply_theme(self, theme: str, dock_mode: str) -> None:
        self._theme = theme
        self._dock_mode = dock_mode
        pal = PALETTES[theme]
        is_float = dock_mode == "float"

        border_col = pal["accent"] if is_float else pal["border"]
        self.surface.setStyleSheet(
            f"#miniSurface {{ background: {pal['card_bg']}; border: 1px solid {border_col}; "
            f"border-radius: 11px; }}"
        )
        self.update()

        self.grip_lbl.setVisible(is_float)
        self.grip_lbl.setStyleSheet(f"color: {pal['muted']}; font-size: 11px;")

        self.model_lbl.setStyleSheet(f"color: {pal['fg']}; font-size: 12px; font-weight: 600;")
        self.tier_lbl.setStyleSheet(
            f"background: {pal['accent_soft']}; color: {pal['accent']}; border-radius: 4px; "
            f"padding: 1px 5px; font-size: 11px; font-weight: 600;"
        )

        self.dock_btn.setText("浮" if is_float else "钉")
        self.dock_btn.setStyleSheet(
            f"QPushButton {{ background: {pal['card_alt']}; color: {pal['muted']}; border: none; "
            f"border-radius: 4px; font-size: 11px; font-weight: 600; }}"
            f"QPushButton:hover {{ background: {pal['hover']}; color: {pal['fg']}; }}"
        )

        self.status_lbl.setStyleSheet(f"color: {pal['muted']}; font-size: 11.5px;")
        self.fh_title.setStyleSheet(f"color: {pal['muted']}; font-size: 11.5px; font-weight: 500;")
        self.wk_title.setStyleSheet(f"color: {pal['muted']}; font-size: 11.5px; font-weight: 500;")
        self.fh_val.setStyleSheet(f"color: {pal['fg']}; font-size: 12px; font-weight: 600;")
        self.wk_val.setStyleSheet(f"color: {pal['fg']}; font-size: 12px; font-weight: 600;")
        self.fh_cd.setStyleSheet(f"color: {pal['muted']}; font-size: 11px;")
        self.wk_cd.setStyleSheet(f"color: {pal['muted']}; font-size: 11px;")
        self.fh_note.setStyleSheet(
            f"background: {pal['card_alt']}; color: {pal['muted']}; border-radius: 5px; "
            f"padding: 3px 7px; font-size: 11px;"
        )
        self.divider.setStyleSheet(f"background: {pal['border']}; border: none;")
        self.hint_lbl.setStyleSheet(f"color: {pal['muted']}; font-size: 11px;")
        self.chevron_lbl.setStyleSheet(f"color: {pal['muted']}; font-size: 12px;")

    def update_data(self, payload: dict | None, error_msg: str | None, mode: str, expanded: bool) -> None:
        self._mode = mode
        self._expanded = expanded
        pal = PALETTES[self._theme]

        self.hint_lbl.setText("点击收起全部模型" if expanded else "点击展开全部模型")
        self.chevron_lbl.setText("<" if expanded else ">")

        has_groups = bool(payload and payload.get("groups"))
        if not has_groups:
            dot_col = pal["red"] if error_msg else pal["amber"]
            self.dot_lbl.setStyleSheet(f"background: {dot_col}; border-radius: 4px;")
            self.model_lbl.setText("Antigravity 用量")
            self.tier_lbl.setVisible(False)
            self.fh_widget.setVisible(False)
            self.fh_note.setVisible(False)
            self.wk_widget.setVisible(False)
            self.status_lbl.setVisible(True)
            self.status_lbl.setText("等待 Antigravity 连接..." if error_msg else "正在获取配额信息...")
            self.adjustSize()
            return

        self.status_lbl.setVisible(False)
        stale = bool(payload.get("stale") or error_msg)
        dot_col = pal["amber"] if stale else pal["accent"]
        self.dot_lbl.setStyleSheet(f"background: {dot_col}; border-radius: 4px;")

        cur = payload.get("current") or {}
        full_label = cur.get("label") or "当前模型"
        short_name, tier = split_model_short_and_tier(full_label)

        elide_font = QFont(self.model_lbl.font())
        elide_font.setPixelSize(12)
        elide_font.setWeight(QFont.DemiBold)
        fm = QFontMetrics(elide_font)
        elided = fm.elidedText(short_name, Qt.ElideRight, 130 if tier else 164)
        self.model_lbl.setText(elided)
        self.model_lbl.setToolTip(f"{full_label} ({cur.get('groupName') or ''})")

        if tier:
            self.tier_lbl.setText(tier)
            self.tier_lbl.setVisible(True)
        else:
            self.tier_lbl.setVisible(False)

        self._bind_meter(
            bucket=cur.get("fiveHour"),
            container=self.fh_widget,
            note=self.fh_note,
            val_lbl=self.fh_val,
            cd_lbl=self.fh_cd,
            bar=self.fh_bar,
            is_five_hour=True,
        )
        self._bind_meter(
            bucket=cur.get("weekly"),
            container=self.wk_widget,
            note=None,
            val_lbl=self.wk_val,
            cd_lbl=self.wk_cd,
            bar=self.wk_bar,
            is_five_hour=False,
        )
        self.adjustSize()

    def _bind_meter(self, bucket, container, note, val_lbl, cd_lbl, bar, is_five_hour: bool) -> None:
        pal = PALETTES[self._theme]
        if not bucket:
            container.setVisible(False)
            if note is not None and is_five_hour:
                note.setVisible(True)
            return

        if note is not None:
            note.setVisible(False)
        container.setVisible(True)

        used = float(bucket.get("usedPercent") or 0.0)
        rem = float(bucket.get("remainingPercent") if bucket.get("remainingPercent") is not None else max(0.0, 100.0 - used))
        is_used = self._mode == "used"
        primary_val = used if is_used else rem
        prefix = "已用" if is_used else "剩余"
        val_lbl.setText(f"{prefix} {fmt_pct(primary_val)}")

        reset_ms = bucket.get("resetMs")
        cd_lbl.setText(fmt_short_countdown(reset_ms))
        tip = fmt_full_reset_time(reset_ms)
        if tip:
            container.setToolTip(tip)

        bar.set_state(primary_val, pal["track"], pal[tone_color_key(used)])

    def paintEvent(self, _event):
        paint_soft_shadow(
            self,
            self.surface.geometry(),
            11.0,
            self._dock_mode == "float",
            self._theme,
        )

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._dragging = True
            self._drag_moved = False
            self._press_global = event.globalPos()
            self._press_win = self.pos()
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
            if self._dock_mode == "float" and (abs(delta.x()) >= 5 or abs(delta.y()) >= 5):
                self._drag_moved = True
            if self._drag_moved:
                self.move(self._press_win + delta)
                self.moved_while_dragging.emit()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._dragging:
            was_drag = self._drag_moved
            self._dragging = False
            self._drag_moved = False
            if was_drag:
                self.drag_finished.emit(self.x(), self.y())
            else:
                self.toggled_expand.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ExpandedPopover(QWidget):
    """Expanded floating card displaying all model groups and family chips."""

    closed_requested = pyqtSignal()
    mode_toggled = pyqtSignal()
    refresh_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("全部模型用量")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        self._theme = "light"
        self._mode = "used"

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN)
        outer.setSpacing(0)

        self.surface = QFrame()
        self.surface.setObjectName("popoverSurface")
        self.surface.setFixedWidth(POPOVER_VIS_WIDTH)
        outer.addWidget(self.surface)

        root = QVBoxLayout(self.surface)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        # Header
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(6)

        self.title_lbl = QLabel("全部模型用量")
        head.addWidget(self.title_lbl)

        self.plan_lbl = QLabel("")
        self.plan_lbl.setVisible(False)
        head.addWidget(self.plan_lbl)

        head.addStretch(1)

        self.mode_btn = QPushButton("已用视角")
        self.mode_btn.setCursor(Qt.PointingHandCursor)
        self.mode_btn.setToolTip("点击切换 已用 / 剩余 显示")
        self.mode_btn.clicked.connect(self.mode_toggled.emit)
        head.addWidget(self.mode_btn)

        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.setFixedSize(36, 20)
        self.refresh_btn.setCursor(Qt.PointingHandCursor)
        self.refresh_btn.setToolTip("立即刷新配额")
        self.refresh_btn.clicked.connect(self.refresh_requested.emit)
        head.addWidget(self.refresh_btn)

        self.close_btn = QPushButton("×")
        self.close_btn.setFixedSize(22, 20)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.setToolTip("收起 (Esc)")
        self.close_btn.clicked.connect(self.closed_requested.emit)
        head.addWidget(self.close_btn)

        root.addLayout(head)

        self.head_line = QFrame()
        self.head_line.setFixedHeight(1)
        root.addWidget(self.head_line)

        # Groups container (wrapped in borderless scroll area only when exceeding screen height)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        self.groups_host = QWidget()
        self.groups_host.setFixedWidth(POPOVER_VIS_WIDTH - 24)
        self.groups_host.setStyleSheet("background: transparent;")
        self.groups_layout = QVBoxLayout(self.groups_host)
        self.groups_layout.setContentsMargins(0, 0, 0, 0)
        self.groups_layout.setSpacing(8)
        self.scroll.setWidget(self.groups_host)
        root.addWidget(self.scroll)

        # Footer
        foot = QHBoxLayout()
        foot.setContentsMargins(0, 2, 0, 0)
        foot.setSpacing(6)
        self.foot_note = QLabel("同组模型共享 5h 与一周配额")
        self.foot_time = QLabel("")
        foot.addWidget(self.foot_note)
        foot.addStretch(1)
        foot.addWidget(self.foot_time)
        root.addLayout(foot)

        self.apply_theme("light")

    def visible_size(self) -> tuple[int, int]:
        return self.surface.width(), self.surface.height()

    def apply_theme(self, theme: str) -> None:
        self._theme = theme
        pal = PALETTES[theme]
        self.surface.setStyleSheet(
            f"#popoverSurface {{ background: {pal['card_bg']}; border: 1px solid {pal['border']}; "
            f"border-radius: 12px; }}"
        )
        self.update()
        self.title_lbl.setStyleSheet(f"color: {pal['fg']}; font-size: 13px; font-weight: 600;")
        self.plan_lbl.setStyleSheet(
            f"background: {pal['accent_soft']}; color: {pal['accent']}; border-radius: 4px; "
            f"padding: 1px 6px; font-size: 11px; font-weight: 600;"
        )
        btn_css = (
            f"QPushButton {{ background: {pal['card_alt']}; color: {pal['muted']}; border: none; "
            f"border-radius: 5px; padding: 2px 7px; font-size: 11px; font-weight: 500; }}"
            f"QPushButton:hover {{ background: {pal['hover']}; color: {pal['fg']}; }}"
        )
        self.mode_btn.setStyleSheet(btn_css)
        self.refresh_btn.setStyleSheet(btn_css)
        self.close_btn.setStyleSheet(btn_css.replace("padding: 2px 7px;", "padding: 0px; font-size: 14px;"))
        self.head_line.setStyleSheet(f"background: {pal['border']}; border: none;")
        self.foot_note.setStyleSheet(f"color: {pal['muted']}; font-size: 11px;")
        self.foot_time.setStyleSheet(f"color: {pal['muted']}; font-size: 11px;")

    def _clear_groups(self) -> None:
        while self.groups_layout.count():
            item = self.groups_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()

    def update_data(self, payload: dict | None, mode: str) -> None:
        self._mode = mode
        pal = PALETTES[self._theme]
        self.mode_btn.setText("已用视角" if mode == "used" else "剩余视角")

        if not payload:
            self._clear_groups()
            empty = QLabel("暂未获取到配额数据")
            empty.setStyleSheet(f"color: {pal['muted']}; font-size: 12px; padding: 12px 0;")
            self.groups_layout.addWidget(empty)
            self.plan_lbl.setVisible(False)
            self.foot_time.setText("")
            self._resize_popover()
            return

        plan_name = (payload.get("tier") or {}).get("planName") or (payload.get("tier") or {}).get("name") or ""
        if plan_name:
            self.plan_lbl.setText(plan_name)
            self.plan_lbl.setVisible(True)
        else:
            self.plan_lbl.setVisible(False)

        self.foot_time.setText(fmt_relative_updated(payload.get("fetchedAt")))

        self._clear_groups()
        groups = payload.get("groups") or []
        for g in groups:
            self.groups_layout.addWidget(self._build_group_card(g, mode))

        self._resize_popover()

    def _resize_popover(self) -> None:
        host_w = POPOVER_VIS_WIDTH - 24
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

    def paintEvent(self, _event):
        paint_soft_shadow(self, self.surface.geometry(), 12.0, True, self._theme)

    def _build_group_card(self, g: dict, mode: str) -> QFrame:
        pal = PALETTES[self._theme]
        is_cur_group = bool(g.get("isCurrentGroup"))
        card = QFrame()
        card.setObjectName("groupCard")
        border_col = pal["accent"] if is_cur_group else pal["border"]
        card.setStyleSheet(
            f"#groupCard {{ background: {pal['card_bg']}; border: 1px solid {border_col}; border-radius: 9px; }}"
        )
        lay = QVBoxLayout(card)
        lay.setContentsMargins(9, 8, 9, 8)
        lay.setSpacing(6)

        # Group header
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
        g_name = QLabel(g.get("displayName") or "Model Group")
        g_name.setStyleSheet(f"color: {pal['fg']}; font-size: 12px; font-weight: 600;")
        top.addWidget(g_name)
        if is_cur_group:
            top.addWidget(make_pill_label("当前", pal["accent_soft"], pal["accent"], bold=True))
        top.addStretch(1)
        cnt = QLabel(f"{g.get('modelCount', 0)} 模型")
        cnt.setStyleSheet(f"color: {pal['muted']}; font-size: 11px;")
        top.addWidget(cnt)
        lay.addLayout(top)

        # 2-column meters grid (5h | 1周)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(3)

        grid.addWidget(self._build_bucket_cell("5h", g.get("fiveHour"), mode, "5h: 仅周限额"), 0, 0)
        grid.addWidget(self._build_bucket_cell("1周", g.get("weekly"), mode, "暂无周限额"), 0, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)

        # Model family chips
        families = g.get("families") or []
        if families:
            chips_host = QWidget()
            chips_host.setStyleSheet("background: transparent;")
            flow = FlowLayout(chips_host, margin=0, h_spacing=4, v_spacing=4)
            for fam in families:
                fam_name = fam.get("name") or ""
                short_name = short_family_chip_name(fam_name)
                variants = fam.get("variants") or []
                variant_tiers = [v.get("tierLabel") for v in variants if v.get("tierLabel")]
                cur_var = next((v for v in variants if v.get("isCurrent")), None)
                if cur_var and cur_var.get("tierLabel"):
                    chip_text = f"{short_name} ({cur_var['tierLabel']})"
                else:
                    chip_text = short_name

                tip = f"{fam_name}: {' / '.join(variant_tiers)}" if variant_tiers else fam_name
                is_cur_fam = bool(fam.get("hasCurrent"))
                chip = make_pill_label(
                    chip_text,
                    pal["accent"] if is_cur_fam else pal["card_alt"],
                    pal["accent_fg"] if is_cur_fam else pal["muted"],
                    bold=is_cur_fam,
                    tooltip=tip,
                )
                flow.addWidget(chip)
            lay.addWidget(chips_host)

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
        row.setSpacing(4)
        l_lbl = QLabel(label)
        l_lbl.setStyleSheet(f"color: {pal['muted']}; font-size: 11px;")
        v_lbl = QLabel(fmt_pct(val))
        v_lbl.setStyleSheet(f"color: {pal['fg']}; font-size: 11.5px; font-weight: 600;")
        row.addWidget(l_lbl)
        row.addStretch(1)
        row.addWidget(v_lbl)
        if cd:
            c_lbl = QLabel(cd)
            c_lbl.setStyleSheet(f"color: {pal['muted']}; font-size: 10.5px;")
            row.addWidget(c_lbl)
        lay.addLayout(row)

        bar = QuotaProgressBar()
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
        self.error_msg: str | None = None
        self.expanded = False
        self.manual_hidden = False
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
        self.mini_card.moved_while_dragging.connect(self._on_mini_drag_moved)
        self.mini_card.drag_finished.connect(self._on_mini_drag_finished)
        self.mini_card.context_menu_requested.connect(self._show_context_menu)

        self.popover.closed_requested.connect(self.collapse)
        self.popover.mode_toggled.connect(self.toggle_display_mode)
        self.popover.refresh_requested.connect(lambda: self.client.request_fetch(force=True))

        self.client.quota_fetched.connect(self._on_quota_fetched)

        self.tray = None
        if enable_tray and QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(build_tray_icon(), self)
            self.tray.setToolTip("Antigravity 模型用量浮窗")
            self.tray.activated.connect(self._on_tray_activated)
            self.tray.setContextMenu(self._build_menu())
            self.tray.show()

        self.apply_theme_and_render()

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(lambda: self.client.request_fetch(force=False))
        self.poll_timer.start(POLL_OK_MS)

        self.tick_timer = QTimer(self)
        self.tick_timer.timeout.connect(self._on_ui_tick)
        self.tick_timer.start(TICK_MS)

        self.track_timer = QTimer(self)
        self.track_timer.timeout.connect(self._update_dock_position)

        self.lifecycle_timer = QTimer(self)
        self.lifecycle_timer.timeout.connect(self._check_app_lifecycle)

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
        else:
            self.track_timer.start(TRACK_MS)
            self._update_dock_position()
        self.client.request_fetch(force=False)

    def _effective_theme(self) -> str:
        return resolve_theme(self.prefs["theme"])

    def apply_theme_and_render(self) -> None:
        theme = self._effective_theme()
        self.mini_card.apply_theme(theme, self.prefs["dock"])
        self.popover.apply_theme(theme)
        self.mini_card.update_data(self.payload, self.error_msg, self.prefs["mode"], self.expanded)
        self.popover.update_data(self.payload, self.prefs["mode"])
        if self.expanded and self.mini_card.isVisible():
            self._position_popover()
        self._update_tray_tooltip()

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

        self.apply_theme_and_render()
        if self.prefs["dock"] == "docked":
            self._update_dock_position()

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
        self.apply_theme_and_render()

    def toggle_expand(self) -> None:
        if self.expanded:
            self.collapse()
        else:
            self.expanded = True
            self.apply_theme_and_render()
            self._position_popover()
            self.popover.show()
            self.popover.raise_()

    def collapse(self) -> None:
        self.expanded = False
        self.popover.hide()
        self.apply_theme_and_render()

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
            self.apply_theme_and_render()
            self._place_float_initial()
            self.mini_card.show()
            self.mini_card.raise_()
            if self.expanded:
                self._position_popover()
                self.popover.show()
        else:
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
                self.mini_card.raise_()
                if self.expanded:
                    self._position_popover()
                    self.popover.show()

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

    def _on_mini_drag_finished(self, x: int, y: int) -> None:
        if self.prefs["dock"] == "float":
            self.prefs["float_pos"] = [x, y]
            save_prefs(self.prefs)
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

        if self.tracker.is_iconic(hwnd):
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

        # Check foreground window overlap
        fg_hwnd = self.tracker.get_foreground_hwnd()
        agy_pid = self.tracker.get_pid(hwnd)
        fg_pid = self.tracker.get_pid(fg_hwnd) if fg_hwnd else 0

        should_show = True
        if fg_hwnd and fg_pid not in (self.tracker.own_pid, agy_pid):
            fg_rect = self.tracker.get_window_rect(fg_hwnd)
            if fg_rect:
                geo = screen.geometry() if screen else QRect(0, 0, 1920, 1080)
                card_phys_l = mon_origin[0] + round((target_x + SHADOW_MARGIN - geo.left()) * dpr)
                card_phys_t = mon_origin[1] + round((target_y + SHADOW_MARGIN - geo.top()) * dpr)
                card_phys_r = card_phys_l + round(vis_w * dpr)
                card_phys_b = card_phys_t + round(vis_h * dpr)
                intersects = not (
                    fg_rect[2] <= card_phys_l
                    or fg_rect[0] >= card_phys_r
                    or fg_rect[3] <= card_phys_t
                    or fg_rect[1] >= card_phys_b
                )
                if intersects:
                    should_show = False

        if not should_show:
            self.mini_card.hide()
            self.popover.hide()
            return

        if self.mini_card.pos() != QPoint(target_x, target_y):
            self.mini_card.move(target_x, target_y)
        if not self.mini_card.isVisible():
            self.mini_card.show()
        if self.expanded:
            self._position_popover()
            if not self.popover.isVisible():
                self.popover.show()

    def _update_tray_tooltip(self) -> None:
        if not self.tray:
            return
        cur = (self.payload or {}).get("current") or {}
        if not cur:
            self.tray.setToolTip("Antigravity 模型用量浮窗")
            return
        label = cur.get("label") or "当前模型"
        fh = cur.get("fiveHour")
        wk = cur.get("weekly")
        is_used = self.prefs["mode"] == "used"
        key = "usedPercent" if is_used else "remainingPercent"
        prefix = "已用" if is_used else "剩余"
        fh_txt = f"5h {prefix} {fmt_pct(fh.get(key))}" if fh else "5h 仅周限额"
        wk_txt = f"1周 {prefix} {fmt_pct(wk.get(key))}" if wk else ""
        self.tray.setToolTip(f"{label}\n{fh_txt} | {wk_txt}".strip(" |"))

    def _build_menu(self) -> QMenu:
        menu = QMenu()

        toggle_vis = QAction("隐藏浮窗" if self.mini_card.isVisible() else "显示浮窗", menu)
        toggle_vis.triggered.connect(self.toggle_visible)
        menu.addAction(toggle_vis)

        toggle_exp = QAction("收起全部模型" if self.expanded else "展开全部模型", menu)
        toggle_exp.triggered.connect(self.toggle_expand)
        menu.addAction(toggle_exp)

        menu.addSeparator()

        dock_group = QActionGroup(menu)
        act_docked = QAction("吸附到 Antigravity 左下角", menu, checkable=True)
        act_docked.setChecked(self.prefs["dock"] == "docked")
        act_docked.triggered.connect(lambda: self.set_dock_mode("docked"))
        dock_group.addAction(act_docked)
        menu.addAction(act_docked)

        act_float = QAction("自由悬浮置顶", menu, checkable=True)
        act_float.setChecked(self.prefs["dock"] == "float")
        act_float.triggered.connect(lambda: self.set_dock_mode("float"))
        dock_group.addAction(act_float)
        menu.addAction(act_float)

        menu.addSeparator()

        mode_group = QActionGroup(menu)
        act_used = QAction("已用视角", menu, checkable=True)
        act_used.setChecked(self.prefs["mode"] == "used")
        act_used.triggered.connect(lambda: self.set_display_mode("used"))
        mode_group.addAction(act_used)
        menu.addAction(act_used)

        act_rem = QAction("剩余视角", menu, checkable=True)
        act_rem.setChecked(self.prefs["mode"] == "remaining")
        act_rem.triggered.connect(lambda: self.set_display_mode("remaining"))
        mode_group.addAction(act_rem)
        menu.addAction(act_rem)

        theme_menu = menu.addMenu("外观主题")
        theme_group = QActionGroup(theme_menu)
        for label, key in [("跟随系统", "system"), ("浅色", "light"), ("深色", "dark")]:
            act = QAction(label, theme_menu, checkable=True)
            act.setChecked(self.prefs["theme"] == key)
            act.triggered.connect(lambda _checked=False, k=key: self.set_theme_pref(k))
            theme_group.addAction(act)
            theme_menu.addAction(act)

        menu.addSeparator()

        act_auto_exit = QAction("随 Antigravity 关闭自动退出", menu, checkable=True)
        act_auto_exit.setChecked(bool(self.prefs.get("auto_exit_with_app", True)))
        act_auto_exit.triggered.connect(self.toggle_auto_exit)
        menu.addAction(act_auto_exit)

        act_refresh = QAction("立即刷新", menu)
        act_refresh.triggered.connect(lambda: self.client.request_fetch(force=True))
        menu.addAction(act_refresh)

        act_quit = QAction("退出", menu)
        act_quit.triggered.connect(self.quit_app)
        menu.addAction(act_quit)

        return menu

    def _show_context_menu(self, global_pos: QPoint) -> None:
        menu = self._build_menu()
        menu.exec_(global_pos)

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger:
            if self.mini_card.isVisible():
                self.mini_card.raise_()
                if self.expanded:
                    self.popover.raise_()
            else:
                self.manual_hidden = False
                if self.prefs["dock"] == "docked":
                    self._update_dock_position()
                else:
                    self.mini_card.show()
                    self.mini_card.raise_()
        elif reason == QSystemTrayIcon.Context and self.tray:
            self.tray.setContextMenu(self._build_menu())

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

    mini = MiniQuotaCard()
    pop = ExpandedPopover()

    renders = []
    for theme in ("light", "dark"):
        for mode in ("used", "remaining"):
            dock = "docked" if mode == "used" else "float"
            mini.apply_theme(theme, dock)
            pop.apply_theme(theme)
            mini.update_data(payload, err, mode, expanded=True)
            pop.update_data(payload, mode)
            mini.show()
            pop.show()
            app.processEvents()

            pm_mini = mini.grab()
            pm_pop = pop.grab()
            if pm_mini.isNull() or pm_mini.width() <= 0 or pm_mini.height() <= 0:
                print(json.dumps({"ok": False, "error": "MiniQuotaCard grab failed"}, ensure_ascii=False))
                return 1
            if pm_pop.isNull() or pm_pop.width() <= 0 or pm_pop.height() <= 0:
                print(json.dumps({"ok": False, "error": "ExpandedPopover grab failed"}, ensure_ascii=False))
                return 1
            renders.append({
                "theme": theme,
                "mode": mode,
                "dock": dock,
                "miniSize": [pm_mini.width(), pm_mini.height()],
                "popoverSize": [pm_pop.width(), pm_pop.height()],
            })

    tray_icon = build_tray_icon()
    pop.hide()
    mini.hide()

    cur = payload.get("current") or {}
    report = {
        "ok": True,
        "discoveredPorts": ports,
        "connectedPort": used_port,
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
        log_event("skipped reason=already_running")
        return 0

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
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
