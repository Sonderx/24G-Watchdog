# battery_monitor.py
# -*- coding: utf-8 -*-
import os
import sys
import time
import glob
import json
import ctypes
from ctypes import wintypes
import winreg
import threading
import tkinter as tk
from tkinter import ttk
import pystray
from PIL import Image, ImageDraw, ImageFont
import winrt.windows.ui.notifications as notif
import winrt.windows.data.xml.dom as dom
import win32file
import win32event
import win32api
import pywintypes

# High DPI awareness for crisp native rendering
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-monitor DPI aware
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor.log")
LOG_MAX_BYTES = 256 * 1024
_log_lock = threading.Lock()

DEFAULT_CONFIG = {
    "poll_interval_sec": 4,
    "low_battery_warn": 20,
    "low_battery_crit": 10,
    "theme_mode": "system",        # "system", "dark", "light"
    "start_with_windows": False,
    "style": "bars",               # "bars" (default) or "digits"
    "order": "mouse_first",        # "mouse_first" or "keyboard_first"
    # 键盘省电策略（关键）：后台绝不轮询，只有这些情况才发 RF 查询
    "keyboard_enabled": True,
    "kb_probe_on_start": True,
    "kb_activity_detect": True,    # Raw Input 按键后自动刷新
    "kb_query_cooldown_sec": 1800, # 按键触发自动刷新的最小间隔
    "kb_sleep_hint_sec": 600       # 无按键多久后本地标记“休眠 · 缓存”
}


def log_event(message):
    """Append a line to monitor.log in the source directory, rotating at 256 KB."""
    try:
        with _log_lock:
            try:
                if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
                    os.replace(LOG_FILE, LOG_FILE + ".1")
            except OSError:
                pass
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{stamp} {message}\n")
    except Exception:
        # 日志不能影响主功能
        pass


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                for k, v in DEFAULT_CONFIG.items():
                    cfg.setdefault(k, v)
                return cfg
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Error saving config: {e}")


def get_system_theme():
    """Return (taskbar_is_dark, apps_is_dark) from Windows 10 Personalize registry."""
    taskbar_dark = True
    apps_dark = True
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        )
        try:
            val, _ = winreg.QueryValueEx(key, "SystemUsesLightTheme")
            taskbar_dark = (val == 0)
        except FileNotFoundError:
            pass
        try:
            val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            apps_dark = (val == 0)
        except FileNotFoundError:
            pass
        winreg.CloseKey(key)
    except Exception:
        pass
    return taskbar_dark, apps_dark


def is_app_dark(config):
    mode = config.get("theme_mode", "system")
    if mode == "dark":
        return True
    if mode == "light":
        return False
    _, apps_dark = get_system_theme()
    return apps_dark


def is_taskbar_dark(config):
    mode = config.get("theme_mode", "system")
    if mode == "dark":
        return True
    if mode == "light":
        return False
    taskbar_dark, _ = get_system_theme()
    return taskbar_dark


# Win10 Native Toast Notification
def send_toast(title, message, urgent=False):
    try:
        if urgent:
            xml = (
                "<toast scenario=\"reminder\"><visual><binding template=\"ToastGeneric\">"
                f"<text>{title}</text><text>{message}</text>"
                "</binding></visual>"
                "<actions><action content=\"知道了\" arguments=\"dismiss\"/></actions></toast>"
            )
        else:
            xml = (
                "<toast><visual><binding template=\"ToastGeneric\">"
                f"<text>{title}</text><text>{message}</text>"
                "</binding></visual></toast>"
            )
        doc = dom.XmlDocument()
        doc.load_xml(xml)
        toast = notif.ToastNotification(doc)
        notifier = notif.ToastNotificationManager.create_toast_notifier(
            "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"
        )
        notifier.show(toast)
    except Exception as e:
        print(f"Toast error: {e}")


# SetupAPI HID enumeration helpers (64-bit safe)
class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("InterfaceClassGuid", _GUID),
        ("Flags", wintypes.DWORD),
        ("Reserved", ctypes.c_void_p),
    ]


class _HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Size", wintypes.ULONG),
        ("VendorID", wintypes.USHORT),
        ("ProductID", wintypes.USHORT),
        ("VersionNumber", wintypes.USHORT),
    ]


class _HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", wintypes.USHORT),
        ("UsagePage", wintypes.USHORT),
        ("InputReportByteLength", wintypes.USHORT),
        ("OutputReportByteLength", wintypes.USHORT),
        ("FeatureReportByteLength", wintypes.USHORT),
        ("Reserved", wintypes.USHORT * 17),
        ("NumberLinkCollectionNodes", wintypes.USHORT),
        ("NumberInputButtonCaps", wintypes.USHORT),
        ("NumberInputValueCaps", wintypes.USHORT),
        ("NumberInputDataIndices", wintypes.USHORT),
        ("NumberOutputButtonCaps", wintypes.USHORT),
        ("NumberOutputValueCaps", wintypes.USHORT),
        ("NumberOutputDataIndices", wintypes.USHORT),
        ("NumberFeatureButtonCaps", wintypes.USHORT),
        ("NumberFeatureValueCaps", wintypes.USHORT),
        ("NumberFeatureDataIndices", wintypes.USHORT),
    ]


def find_vgn_hid_endpoints():
    """Dynamically locate VGN S99 vendor HID interface (Wired PID 0x5055 or 2.4G Dongle PID 0x5088)."""
    setupapi = ctypes.windll.setupapi
    hid = ctypes.windll.hid
    kernel32 = ctypes.windll.kernel32

    setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_GUID),
        wintypes.DWORD, ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA)
    ]
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA),
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p
    ]
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    hid.HidD_GetAttributes.argtypes = [wintypes.HANDLE, ctypes.POINTER(_HIDD_ATTRIBUTES)]
    hid.HidD_GetPreparsedData.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
    hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
    hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(_HIDP_CAPS)]

    hid_guid = _GUID()
    hid.HidD_GetHidGuid(ctypes.byref(hid_guid))
    hdev = setupapi.SetupDiGetClassDevsW(ctypes.byref(hid_guid), None, None, 0x12)
    if not hdev or hdev == ctypes.c_void_p(-1).value:
        return []

    results = []
    idx = 0
    invalid_handle = ctypes.c_void_p(-1).value
    try:
        while True:
            iface = _SP_DEVICE_INTERFACE_DATA()
            iface.cbSize = ctypes.sizeof(iface)
            if not setupapi.SetupDiEnumDeviceInterfaces(hdev, None, ctypes.byref(hid_guid), idx, ctypes.byref(iface)):
                break
            idx += 1
            req = wintypes.DWORD()
            setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(iface), None, 0, ctypes.byref(req), None)
            if req.value == 0:
                continue
            buf = (ctypes.c_byte * req.value)()
            ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0] = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            if setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(iface), buf, req, None, None):
                path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
                if "vid_320f" not in path.lower():
                    continue
                h = kernel32.CreateFileW(path, 0, 3, None, 3, 0, None)
                if h != invalid_handle and h != 0:
                    try:
                        attr = _HIDD_ATTRIBUTES(Size=ctypes.sizeof(_HIDD_ATTRIBUTES))
                        if hid.HidD_GetAttributes(h, ctypes.byref(attr)):
                            if attr.VendorID == 0x320F and attr.ProductID in (0x5055, 0x5088):
                                pp = ctypes.c_void_p()
                                if hid.HidD_GetPreparsedData(h, ctypes.byref(pp)):
                                    try:
                                        caps = _HIDP_CAPS()
                                        if hid.HidP_GetCaps(pp, ctypes.byref(caps)) > 0:
                                            if caps.UsagePage == 0xFF1C and caps.Usage == 0x0092 and caps.OutputReportByteLength >= 32:
                                                results.append((path, attr.ProductID, caps.InputReportByteLength, caps.OutputReportByteLength))
                                    finally:
                                        hid.HidD_FreePreparsedData(pp)
                    finally:
                        kernel32.CloseHandle(h)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdev)

    # Prefer wired PID 0x5055 over 2.4G dongle PID 0x5088 if both are connected
    results.sort(key=lambda item: 0 if item[1] == 0x5055 else 1)
    return results


# Device Status Class
class DeviceStatus:
    def __init__(self, name, icon_type):
        self.name = name
        self.icon_type = icon_type  # 'mouse' or 'keyboard'
        self.battery = -1           # -1 = unknown, 0-100
        self.charging = False
        self.is_online = False
        self.asleep = False
        self.last_valid_battery = -1
        self.last_valid_time = 0
        self.notified_warn = False
        self.notified_crit = False


class QueryResult:
    """Result of one VGN vendor HID power query."""
    __slots__ = ("state", "level", "charging", "wired", "raw", "elapsed_ms", "error")

    def __init__(self, state, level=None, charging=False, wired=False,
                 raw=b"", elapsed_ms=0.0, error=""):
        self.state = state          # online / asleep / timeout / error / no_endpoint
        self.level = level
        self.charging = charging
        self.wired = wired
        self.raw = raw
        self.elapsed_ms = elapsed_ms
        self.error = error


class HardwareMonitor:
    def __init__(self, config):
        self.config = config
        self.mouse = DeviceStatus("雷蛇 DeathAdder V3 Pro", "mouse")
        self.keyboard = DeviceStatus("VGN S99 (2.4G)", "keyboard")
        self.lock = threading.Lock()
        self.running = True
        self._vgn_req_code = 0
        self._vgn_endpoint = None

        # 键盘省电状态机：后台不再按时间轮询，只有显式 probe 才发 RF
        self._kb_probe_lock = threading.Lock()
        self._kb_last_query = 0.0
        self._kb_last_result = ""
        self._kb_last_activity = 0.0      # 最近一次 Raw Input 按键
        self._kb_last_awake = 0.0         # 最近一次“确认醒着”（按键或查询成功）
        self._kb_activity_pending = 0.0   # 上一次查询后的首次按键时间
        self._kb_asleep = False
        self._kb_wired = False
        self._kb_activity_enabled = False
        self._kb_activity_seen = False
        self._last_endpoint_check = 0.0
        self._startup_probe_done = False
        self._state_dirty = True

    def mark_dirty(self):
        with self.lock:
            self._state_dirty = True

    def take_dirty(self):
        with self.lock:
            dirty = self._state_dirty
            self._state_dirty = False
            return dirty

    def set_activity_enabled(self, enabled):
        self._kb_activity_enabled = bool(enabled)

    def note_kb_activity(self):
        """Called by the Raw Input watcher. Only timestamps; never stores key codes."""
        now = time.time()
        woke = False
        first_seen = False
        with self.lock:
            self._kb_last_activity = now
            self._kb_last_awake = now
            if not self._kb_activity_seen:
                self._kb_activity_seen = True
                first_seen = True
            if self._kb_asleep or not self.keyboard.is_online:
                self._kb_asleep = False
                self.keyboard.asleep = False
                self.keyboard.is_online = True
                self.keyboard.name = "VGN S99 (有线)" if self._kb_wired else "VGN S99 (2.4G)"
                self._state_dirty = True
                woke = True
            if self._kb_activity_pending == 0.0:
                self._kb_activity_pending = now
        if first_seen:
            log_event("rawinput: VGN key activity detected")
        if woke:
            log_event("kb activity: wake")

    def _next_req_code(self):
        self._vgn_req_code = (self._vgn_req_code % 65535) + 1
        return self._vgn_req_code

    def _cancel_overlapped(self, h, ov, ev):
        """Cancel a pending overlapped I/O and reap it before the handle is closed."""
        try:
            cancel_ex = getattr(win32file, "CancelIoEx", None)
            if cancel_ex is not None:
                try:
                    cancel_ex(h, ov)
                except Exception:
                    win32file.CancelIo(h)
            else:
                win32file.CancelIo(h)
        except Exception:
            pass
        try:
            win32event.WaitForSingleObject(ev, 100)
        except Exception:
            pass
        try:
            win32file.GetOverlappedResult(h, ov, False)
        except Exception:
            pass

    def query_vgn(self):
        """One bounded vendor HID query. It never loops in the background by itself."""
        t0 = time.perf_counter()
        if not self._vgn_endpoint:
            eps = find_vgn_hid_endpoints()
            if not eps:
                return QueryResult("no_endpoint", elapsed_ms=(time.perf_counter() - t0) * 1000)
            self._vgn_endpoint = eps[0]

        path, pid, in_len, out_len = self._vgn_endpoint
        is_wired = (pid == 0x5055)
        h = None
        ev_w = None
        ev_r = None
        raw = b""
        try:
            h = win32file.CreateFile(
                path,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE,
                None,
                win32file.OPEN_EXISTING,
                win32file.FILE_FLAG_OVERLAPPED,
                None
            )
            req_code = self._next_req_code()
            out_buf = bytearray(max(64, out_len))
            out_buf[0] = 4                    # Report ID 4
            out_buf[1] = req_code & 0xFF      # RequestCode low
            out_buf[2] = (req_code >> 8) & 0xFF
            out_buf[3] = 26                   # xr.power_info = 26 (0x1A)

            ev_w = win32event.CreateEvent(None, True, False, None)
            ov_w = pywintypes.OVERLAPPED()
            ov_w.hEvent = ev_w
            win32file.WriteFile(h, bytes(out_buf[:out_len]), ov_w)
            if win32event.WaitForSingleObject(ev_w, 250) != win32event.WAIT_OBJECT_0:
                self._cancel_overlapped(h, ov_w, ev_w)
                return QueryResult("timeout", wired=is_wired, raw=raw,
                                   elapsed_ms=(time.perf_counter() - t0) * 1000)
            win32file.GetOverlappedResult(h, ov_w, False)

            ev_r = win32event.CreateEvent(None, True, False, None)
            buf = win32file.AllocateReadBuffer(max(64, in_len))
            for _ in range(2):
                win32event.ResetEvent(ev_r)
                ov_r = pywintypes.OVERLAPPED()
                ov_r.hEvent = ev_r
                try:
                    win32file.ReadFile(h, buf, ov_r)
                except pywintypes.error:
                    break
                if win32event.WaitForSingleObject(ev_r, 350) != win32event.WAIT_OBJECT_0:
                    self._cancel_overlapped(h, ov_r, ev_r)
                    break
                try:
                    n = win32file.GetOverlappedResult(h, ov_r, False)
                except pywintypes.error:
                    break
                raw = bytes(buf[:n])
                if len(raw) >= 10 and raw[0] == 4:
                    resp_code = raw[1] | (raw[2] << 8)
                    if resp_code == req_code:
                        if raw[3] == 0x1A:
                            return QueryResult(
                                "online", min(100, raw[8]), (raw[9] != 0) or is_wired,
                                is_wired, raw, (time.perf_counter() - t0) * 1000
                            )
                        if raw[3] == 0xFF:
                            return QueryResult(
                                "asleep", wired=is_wired, raw=raw,
                                elapsed_ms=(time.perf_counter() - t0) * 1000
                            )
            return QueryResult("timeout", wired=is_wired, raw=raw,
                               elapsed_ms=(time.perf_counter() - t0) * 1000)
        except pywintypes.error as e:
            self._vgn_endpoint = None
            return QueryResult("error", wired=is_wired, raw=raw,
                               elapsed_ms=(time.perf_counter() - t0) * 1000,
                               error=f"{e.winerror}:{e.strerror}")
        except Exception as e:
            self._vgn_endpoint = None
            return QueryResult("error", wired=is_wired, raw=raw,
                               elapsed_ms=(time.perf_counter() - t0) * 1000,
                               error=repr(e))
        finally:
            if ev_w:
                try:
                    win32api.CloseHandle(ev_w)
                except Exception:
                    pass
            if ev_r:
                try:
                    win32api.CloseHandle(ev_r)
                except Exception:
                    pass
            if h:
                try:
                    win32file.CloseHandle(h)
                except Exception:
                    pass

    def query_razer(self):
        """Read Razer DeathAdder V3 Pro status from local daemon log with zero lag"""
        h = None
        try:
            pattern = os.path.expandvars(
                r"%LOCALAPPDATA%\Razer\RazerAppEngine\User Data\Logs\systray_systrayv2*.log"
            )
            files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
            if not files:
                return None

            h = win32file.CreateFile(
                files[0],
                win32file.GENERIC_READ,
                win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE,
                None,
                win32file.OPEN_EXISTING,
                0,
                None
            )
            size = win32file.GetFileSize(h)
            read_size = min(size, 65536)
            win32file.SetFilePointer(h, -read_size, win32file.FILE_END)
            hr, data = win32file.ReadFile(h, read_size)

            text = data.decode("utf-8", errors="ignore")
            import re
            m = re.findall(r'\"powerStatus\":\{\"chargingStatus\":\"([^\"]+)\",\"level\":(\d+)\}', text)
            if m:
                status, level = m[-1]
                lvl = int(level)
                ch = (status.lower() == "charging" or status.lower() == "on")
                return lvl, ch
        except Exception:
            pass
        finally:
            if h:
                try:
                    win32file.CloseHandle(h)
                except Exception:
                    pass
        return None

    def get_ordered_devices(self):
        if self.config.get("order", "mouse_first") == "keyboard_first":
            return [self.keyboard, self.mouse]
        return [self.mouse, self.keyboard]

    def _apply_kb_result(self, res):
        now = time.time()
        with self.lock:
            kb = self.keyboard
            kb.name = "VGN S99 (有线)" if res.wired else "VGN S99 (2.4G)"
            self._kb_wired = res.wired
            self._kb_last_result = res.state
            if res.state == "online":
                kb.battery = res.level
                kb.charging = res.charging
                kb.is_online = True
                kb.last_valid_battery = res.level
                kb.last_valid_time = now
                kb.asleep = False
                self._kb_asleep = False
                self._kb_last_awake = now
                self._state_dirty = True
            elif res.state == "asleep":
                self._kb_asleep = True
                kb.asleep = True
                kb.is_online = False
                kb.charging = False
                if kb.last_valid_battery >= 0:
                    kb.battery = kb.last_valid_battery
                self._state_dirty = True
            elif res.state == "no_endpoint":
                self._kb_asleep = False
                kb.asleep = False
                kb.is_online = False
                kb.charging = False
                if kb.last_valid_battery >= 0:
                    kb.battery = kb.last_valid_battery
                self._state_dirty = True
            # timeout / error: keep the last known state; 一次失败不等于休眠
        return res

    def probe_keyboard(self, reason, force=False):
        """The only path that sends an RF query. There is no background polling."""
        if not self.config.get("keyboard_enabled", True):
            return None
        now = time.time()
        if not force:
            if self._kb_asleep:
                return None
            if now - self._kb_last_query < 3.0:
                return None
        if not self._kb_probe_lock.acquire(blocking=False):
            return None
        try:
            self._kb_last_query = now
            res = self.query_vgn()
            self._apply_kb_result(res)
            self._kb_activity_pending = 0.0
            pid = self._vgn_endpoint[1] if self._vgn_endpoint else 0
            head = res.raw[:12].hex() if res.raw else "-"
            extra = f" err={res.error}" if res.error else ""
            log_event(
                f"kb-probe reason={reason} pid={pid:04X} state={res.state} "
                f"level={res.level} chg={int(res.charging)} "
                f"elapsed={res.elapsed_ms:.0f}ms raw={head}{extra}"
            )
            return res
        finally:
            self._kb_probe_lock.release()

    def refresh_endpoint(self, probe_on_change=True):
        """Cheap SetupAPI enumeration (access 0, no reports) to notice replug / cable mode."""
        if not self.config.get("keyboard_enabled", True):
            return
        eps = find_vgn_hid_endpoints()
        new = eps[0] if eps else None
        old_path = self._vgn_endpoint[0] if self._vgn_endpoint else None
        new_path = new[0] if new else None
        if new_path == old_path:
            if new:
                self._kb_wired = (new[1] == 0x5055)
            return
        self._vgn_endpoint = new
        if new is None:
            with self.lock:
                self._kb_wired = False
                self._kb_asleep = False
                self.keyboard.asleep = False
                self.keyboard.is_online = False
                self.keyboard.charging = False
                if self.keyboard.last_valid_battery >= 0:
                    self.keyboard.battery = self.keyboard.last_valid_battery
                self._state_dirty = True
            log_event("kb endpoint lost")
            return
        wired = (new[1] == 0x5055)
        with self.lock:
            self._kb_wired = wired
            self.keyboard.name = "VGN S99 (有线)" if wired else "VGN S99 (2.4G)"
            if wired:
                self._kb_asleep = False
                self.keyboard.asleep = False
            self._state_dirty = True
        log_event(f"kb endpoint changed pid={new[1]:04X} wired={int(wired)}")
        if probe_on_change:
            self.probe_keyboard("wired" if wired else "endpoint", force=True)

    def tick_keyboard(self):
        """Called by the worker every poll_interval_sec. It never polls RF by itself."""
        cfg = self.config
        if not cfg.get("keyboard_enabled", True):
            return
        now = time.time()

        if not self._vgn_endpoint:
            self.refresh_endpoint(probe_on_change=False)
            self._last_endpoint_check = now
        elif now - self._last_endpoint_check >= 60:
            self._last_endpoint_check = now
            self.refresh_endpoint(probe_on_change=True)

        if not self._startup_probe_done:
            self._startup_probe_done = True
            if cfg.get("kb_probe_on_start", True):
                self.probe_keyboard("startup")

        if self._kb_wired:
            if now - self._kb_last_query >= 60:
                # 有线时键盘由 USB 供电，查询不消耗电池；force 避免休眠标记卡死
                self.probe_keyboard("wired", force=True)
            return

        with self.lock:
            asleep = self._kb_asleep
            last_awake = self._kb_last_awake
            last_valid = self.keyboard.last_valid_time
            pending = self._kb_activity_pending
            last_query = self._kb_last_query
            last_result = self._kb_last_result

        # 有 Raw Input 活动检测时，长时间无按键就本地标记休眠（不发 RF 探测）
        if self._kb_activity_enabled and not asleep:
            base = last_awake or last_valid
            if base and now - base >= int(cfg.get("kb_sleep_hint_sec", 600)):
                with self.lock:
                    self._kb_asleep = True
                    self.keyboard.asleep = True
                    self.keyboard.is_online = False
                    self.keyboard.charging = False
                    if self.keyboard.last_valid_battery >= 0:
                        self.keyboard.battery = self.keyboard.last_valid_battery
                    self._state_dirty = True
                log_event("kb state -> sleep (no key activity)")
                asleep = True

        # 按键后 3 秒刷新一次。若上次查询不是成功在线（休眠/超时/错误/无缓存），
        # 允许绕过长冷却，否则一直没有新数据；成功在线时才用 30 分钟冷却。
        cooldown = int(cfg.get("kb_query_cooldown_sec", 1800))
        cooldown_ok = (last_result != "online") or (now - last_query >= cooldown)
        if (not asleep) and pending and (now - pending >= 3.0) and cooldown_ok:
            self.probe_keyboard("activity")

    def update_mouse(self):
        razer_res = self.query_razer()
        now = time.time()
        with self.lock:
            if razer_res is not None:
                pwr, ch = razer_res
                changed = (pwr != self.mouse.battery or ch != self.mouse.charging
                           or not self.mouse.is_online)
                self.mouse.battery = pwr
                self.mouse.charging = ch
                self.mouse.is_online = True
                self.mouse.last_valid_battery = pwr
                self.mouse.last_valid_time = now
                if changed:
                    self._state_dirty = True
            else:
                if self.mouse.is_online:
                    self.mouse.is_online = False
                    self._state_dirty = True
                if self.mouse.last_valid_battery >= 0:
                    self.mouse.battery = self.mouse.last_valid_battery
        self.check_notifications()

    def check_notifications(self):
        warn_th = self.config.get("low_battery_warn", 20)
        crit_th = self.config.get("low_battery_crit", 10)

        for dev in [self.mouse, self.keyboard]:
            if dev.icon_type == "keyboard" and not self.config.get("keyboard_enabled", True):
                continue
            if dev.battery < 0 or dev.charging:
                dev.notified_warn = False
                dev.notified_crit = False
                continue

            if dev.battery > warn_th:
                dev.notified_warn = False
                dev.notified_crit = False
            elif dev.battery <= crit_th:
                if not dev.notified_crit:
                    send_toast("严重低电量警报", f"{dev.name} 当前电量仅剩 {dev.battery}%，请立即连接充电器！", urgent=True)
                    dev.notified_crit = True
                    dev.notified_warn = True
            elif dev.battery <= warn_th:
                if not dev.notified_warn:
                    send_toast("低电量提醒", f"{dev.name} 电量已降至 {dev.battery}%，请注意及时充电。", urgent=True)
                    dev.notified_warn = True


# ---------------------------------------------------------------------------
# Passive keyboard activity detection via Raw Input.
# It only records that the VGN keyboard sent a key report; no key codes are
# stored, injected or intercepted. It never opens the keyboard HID collection
# (which Windows may deny) and never sends RF.
# ---------------------------------------------------------------------------
_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class _RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wintypes.USHORT),
        ("usUsage", wintypes.USHORT),
        ("dwFlags", wintypes.DWORD),
        ("hwndTarget", wintypes.HWND),
    ]


class _RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", wintypes.DWORD),
        ("dwSize", wintypes.DWORD),
        ("hDevice", wintypes.HANDLE),
        ("wParam", wintypes.WPARAM),
    ]


class _RAWKEYBOARD(ctypes.Structure):
    _fields_ = [
        ("MakeCode", wintypes.USHORT),
        ("Flags", wintypes.USHORT),
        ("Reserved", wintypes.USHORT),
        ("VKey", wintypes.USHORT),
        ("Message", wintypes.UINT),
        ("ExtraInformation", wintypes.ULONG),
    ]


class _RAWINPUT(ctypes.Structure):
    _fields_ = [
        ("header", _RAWINPUTHEADER),
        ("keyboard", _RAWKEYBOARD),
    ]


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HANDLE),
        ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_rawinput_api_ready = False

_WM_INPUT = 0x00FF
_WM_CLOSE = 0x0010
_WM_DESTROY = 0x0002
_RID_INPUT = 0x10000003
_RIM_TYPEKEYBOARD = 1
_RIDEV_INPUTSINK = 0x00000100
_RIDI_DEVICENAME = 0x20000007
_WS_EX_TOOLWINDOW = 0x00000080


def _init_rawinput_api():
    global _rawinput_api_ready
    if _rawinput_api_ready:
        return
    _user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
    _user32.RegisterClassW.restype = wintypes.WORD
    _user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE]
    _user32.UnregisterClassW.restype = wintypes.BOOL
    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HANDLE, wintypes.HANDLE, wintypes.LPVOID,
    ]
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.DestroyWindow.argtypes = [wintypes.HWND]
    _user32.DestroyWindow.restype = wintypes.BOOL
    _user32.RegisterRawInputDevices.argtypes = [
        ctypes.POINTER(_RAWINPUTDEVICE), wintypes.UINT, wintypes.UINT
    ]
    _user32.RegisterRawInputDevices.restype = wintypes.BOOL
    _user32.GetRawInputData.argtypes = [
        wintypes.HANDLE, wintypes.UINT, wintypes.LPVOID,
        ctypes.POINTER(wintypes.UINT), wintypes.UINT,
    ]
    _user32.GetRawInputData.restype = wintypes.UINT
    _user32.GetRawInputDeviceInfoW.argtypes = [
        wintypes.HANDLE, wintypes.UINT, wintypes.LPVOID, ctypes.POINTER(wintypes.UINT)
    ]
    _user32.GetRawInputDeviceInfoW.restype = wintypes.UINT
    _user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT
    ]
    _user32.GetMessageW.restype = ctypes.c_int
    _user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _user32.TranslateMessage.restype = wintypes.BOOL
    _user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _user32.DispatchMessageW.restype = ctypes.c_ssize_t
    _user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    ]
    _user32.DefWindowProcW.restype = ctypes.c_ssize_t
    _user32.PostQuitMessage.argtypes = [ctypes.c_int]
    _user32.PostQuitMessage.restype = None
    _user32.PostMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    ]
    _user32.PostMessageW.restype = wintypes.BOOL
    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetModuleHandleW.restype = wintypes.HANDLE
    _rawinput_api_ready = True


class KeyboardActivityWatcher:
    """Message-only window + RIDEV_INPUTSINK; records VGN key activity only."""

    def __init__(self, on_activity):
        self.on_activity = on_activity
        self.available = False
        self._thread = None
        self._running = False
        self._hwnd = None
        self._ready = threading.Event()
        self._names = {}
        self._wndproc_ref = None
        self._class_name = f"Win10BatteryMonitorRawInput_{id(self):X}"
        self._class_atom = 0
        self._last_error_log = 0.0
        self._wm_seen = False

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        _init_rawinput_api()
        self._running = True
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run, name="KbActivityWatcher", daemon=True
        )
        self._thread.start()
        self._ready.wait(1.5)

    def stop(self):
        self._running = False
        hwnd = self._hwnd
        if hwnd:
            try:
                _user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=1.5)
        self._thread = None

    def _run(self):
        hinst = None
        try:
            hinst = _kernel32.GetModuleHandleW(None)
            self._wndproc_ref = _WNDPROC(self._wndproc)
            wc = _WNDCLASSW()
            wc.lpfnWndProc = self._wndproc_ref
            wc.hInstance = hinst
            wc.lpszClassName = self._class_name
            atom = _user32.RegisterClassW(ctypes.byref(wc))
            if not atom:
                log_event(f"rawinput: RegisterClass failed err={ctypes.get_last_error()}")
                return
            self._class_atom = atom
            self._hwnd = _user32.CreateWindowExW(
                _WS_EX_TOOLWINDOW, self._class_name, "kb-activity", 0,
                0, 0, 0, 0, None, None, hinst, None
            )
            if not self._hwnd:
                log_event(f"rawinput: CreateWindow failed err={ctypes.get_last_error()}")
                return
            rid = _RAWINPUTDEVICE(0x01, 0x06, _RIDEV_INPUTSINK, self._hwnd)
            if not _user32.RegisterRawInputDevices(
                ctypes.byref(rid), 1, ctypes.sizeof(rid)
            ):
                log_event(
                    f"rawinput: RegisterRawInputDevices failed err={ctypes.get_last_error()}"
                )
                return
            self.available = True
            self._ready.set()
            log_event("rawinput watcher started")
            msg = wintypes.MSG()
            while self._running:
                ret = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret <= 0:
                    break
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            log_event(f"rawinput watcher error: {e!r}")
        finally:
            self.available = False
            self._ready.set()
            hwnd = self._hwnd
            self._hwnd = None
            if hwnd:
                try:
                    _user32.DestroyWindow(hwnd)
                except Exception:
                    pass
            if self._class_atom:
                try:
                    _user32.UnregisterClassW(self._class_name, hinst)
                except Exception:
                    pass
            log_event("rawinput watcher stopped")

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == _WM_INPUT:
                if not self._wm_seen:
                    self._wm_seen = True
                    log_event("rawinput: first WM_INPUT received")
                self._handle_input(lparam)
                return 0
            if msg == _WM_DESTROY:
                _user32.PostQuitMessage(0)
                return 0
        except Exception as e:
            now = time.time()
            if now - self._last_error_log > 30:
                self._last_error_log = now
                log_event(f"rawinput handler error: {e!r}")
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _handle_input(self, lparam):
        size = wintypes.UINT(0)
        header_size = ctypes.sizeof(_RAWINPUTHEADER)
        if _user32.GetRawInputData(
            wintypes.HANDLE(lparam), _RID_INPUT, None, ctypes.byref(size), header_size
        ) != 0:
            return
        if not size.value:
            return
        buf = ctypes.create_string_buffer(size.value)
        if _user32.GetRawInputData(
            wintypes.HANDLE(lparam), _RID_INPUT, buf, ctypes.byref(size), header_size
        ) != size.value:
            return
        ri = ctypes.cast(buf, ctypes.POINTER(_RAWINPUT)).contents
        if ri.header.dwType != _RIM_TYPEKEYBOARD:
            return
        if ri.keyboard.VKey in (0, 0xFF):
            return
        name = self._device_name(ri.header.hDevice)
        low = name.lower()
        # 无法确认设备名时宁可忽略，也不要因为别的键盘误触发 VGN 查询
        if not low:
            return
        if "vid_320f" not in low:
            return
        if "pid_5088" not in low and "pid_5055" not in low:
            return
        try:
            self.on_activity()
        except Exception:
            pass

    def _device_name(self, hdevice):
        try:
            key = int(getattr(hdevice, "value", hdevice) or 0)
        except Exception:
            key = 0
        if key in self._names:
            return self._names[key]
        name = ""
        try:
            size = wintypes.UINT(0)
            _user32.GetRawInputDeviceInfoW(
                hdevice, _RIDI_DEVICENAME, None, ctypes.byref(size)
            )
            if size.value:
                buf = ctypes.create_unicode_buffer(size.value)
                _user32.GetRawInputDeviceInfoW(
                    hdevice, _RIDI_DEVICENAME, buf, ctypes.byref(size)
                )
                name = buf.value or ""
        except Exception:
            name = ""
        if name and len(self._names) < 64:
            self._names[key] = name
        return name


# Tray Icon Generator
def create_tray_image(top_dev, bottom_dev, style="bars", taskbar_dark=True, warn_th=20):
    w, h = 64, 64
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    def get_fill_color(dev):
        if dev.charging:
            return (0, 230, 118, 255)      # Bright Green
        if (not dev.is_online and dev.battery >= 0) or getattr(dev, "asleep", False):
            return (142, 142, 147, 220)    # Dim Grey (Sleep)
        if 0 <= dev.battery <= warn_th:
            return (255, 69, 58, 255)      # Vivid Red
        if taskbar_dark:
            # Distinct subtle tint or crisp white for high contrast on dark taskbar
            return (79, 195, 247, 255) if dev.icon_type == "mouse" else (255, 255, 255, 255)
        else:
            return (0, 120, 215, 255) if dev.icon_type == "mouse" else (30, 30, 30, 255)

    outline_col = (235, 235, 235, 235) if taskbar_dark else (45, 45, 45, 235)
    bg_track = (40, 40, 40, 140) if taskbar_dark else (210, 210, 210, 140)

    if style == "bars":
        # Draw two bold, crisp horizontal battery bars filling the 64x64 canvas
        # Top bar: y=5..28 (height 23), Bottom bar: y=36..59 (height 23)
        bars = [
            (top_dev, 5, 28),
            (bottom_dev, 36, 59)
        ]
        for dev, y1, y2 in bars:
            # Battery body outline + inner dark track
            draw.rounded_rectangle([2, y1, 56, y2], radius=4, fill=bg_track, outline=outline_col, width=4)
            # Battery positive terminal nub on the right
            nub_y1 = y1 + 6
            nub_y2 = y2 - 6
            draw.rectangle([57, nub_y1, 62, nub_y2], fill=outline_col)

            # Fill level inside [7, y1+5 .. 51, y2-5] -> max inner width = 44px
            pct = max(0, min(100, dev.battery if dev.battery >= 0 else 0))
            if pct > 0:
                inner_max = 44
                fill_w = max(4, int(round(inner_max * pct / 100.0)))
                draw.rectangle([7, y1 + 5, 7 + fill_w, y2 - 5], fill=get_fill_color(dev))
    else:
        # Large bold digits without cramped prefix letters
        try:
            font = ImageFont.truetype("segoeuib.ttf", 31)
        except Exception:
            font = ImageFont.load_default()

        top_str = f"{top_dev.battery}" if top_dev.battery >= 0 else "--"
        bot_str = f"{bottom_dev.battery}" if bottom_dev.battery >= 0 else "--"

        draw.text((32, 15), top_str, fill=get_fill_color(top_dev), font=font, anchor="mm")
        draw.text((32, 49), bot_str, fill=get_fill_color(bottom_dev), font=font, anchor="mm")

    return img


# Win10 Native Flyout Window (runs on main Tk thread)
class Win10Flyout:
    def __init__(self, tk_root, hw_monitor, open_settings_cb):
        self.tk_root = tk_root
        self.hw = hw_monitor
        self.open_settings_cb = open_settings_cb
        self.win = None

    def toggle(self):
        if self.win and self.win.winfo_exists():
            self.win.destroy()
            self.win = None
            return
        self.show()

    def show(self):
        if self.win and self.win.winfo_exists():
            self.win.destroy()

        win = tk.Toplevel(self.tk_root)
        self.win = win
        win.title("电量中心")
        win.overrideredirect(True)
        win.attributes("-topmost", True)

        screen_w = win.winfo_screenwidth()
        screen_h = win.winfo_screenheight()
        win_w, win_h = 336, 268
        pos_x = screen_w - win_w - 12
        pos_y = screen_h - win_h - 52
        win.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")

        dark = is_app_dark(self.hw.config)
        bg = "#1F1F1F" if dark else "#F3F3F3"
        card_bg = "#2B2B2B" if dark else "#FFFFFF"
        border_col = "#3D3D3D" if dark else "#D6D6D6"
        fg = "#FFFFFF" if dark else "#111111"
        sub_fg = "#A0A0A0" if dark else "#666666"
        track_bg = "#3D3D3D" if dark else "#E5E5E5"

        win.configure(bg=border_col)
        container = tk.Frame(win, bg=bg)
        container.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        # Header
        hdr = tk.Frame(container, bg=bg)
        hdr.pack(fill=tk.X, padx=16, pady=(12, 6))
        tk.Label(hdr, text="2.4G 键鼠电量", font=("Segoe UI", 12, "bold"), fg=fg, bg=bg).pack(side=tk.LEFT)
        btn_close = tk.Label(hdr, text="✕", font=("Segoe UI", 10), fg=sub_fg, bg=bg, cursor="hand2")
        btn_close.pack(side=tk.RIGHT)
        btn_close.bind("<Button-1>", lambda e: self.close())

        # Device Cards
        cards = tk.Frame(container, bg=bg)
        cards.pack(fill=tk.BOTH, expand=True, padx=14, pady=4)

        warn_th = self.hw.config.get("low_battery_warn", 20)
        with self.hw.lock:
            devices = list(self.hw.get_ordered_devices())
            snapshots = [
                (
                    d.name,
                    d.icon_type,
                    d.battery,
                    d.charging,
                    d.is_online,
                    d.asleep,
                    d.last_valid_time
                )
                for d in devices
            ]

        for name, icon_type, bat, ch, online, asleep, last_time in snapshots:
            card = tk.Frame(cards, bg=card_bg, highlightbackground=border_col, highlightthickness=1)
            card.pack(fill=tk.X, pady=5, ipady=6)

            top_row = tk.Frame(card, bg=card_bg)
            top_row.pack(fill=tk.X, padx=12, pady=(4, 4))

            left_f = tk.Frame(top_row, bg=card_bg)
            left_f.pack(side=tk.LEFT, fill=tk.X, expand=True)

            tag = "🖱️" if icon_type == "mouse" else "⌨️"
            tk.Label(left_f, text=f"{tag} {name}", font=("Segoe UI", 10, "bold"), fg=fg, bg=card_bg, anchor="w").pack(fill=tk.X)

            upd = time.strftime("%H:%M", time.localtime(last_time)) if last_time else "--"
            if ch:
                state_text, state_col = f"⚡ 正在充电 · 更新 {upd}", "#00E676"
            elif online:
                state_text, state_col = f"● 2.4G 已连接 · 更新 {upd}", "#4FC3F7"
            elif asleep:
                if bat >= 0:
                    state_text, state_col = f"☾ 休眠 · 缓存 {bat}% · 更新 {upd}", sub_fg
                else:
                    state_text, state_col = "☾ 休眠（无缓存）", sub_fg
            elif bat >= 0:
                state_text, state_col = f"☾ 休眠 · 缓存 {bat}% · 更新 {upd}", sub_fg
            else:
                state_text, state_col = "○ 未连接", sub_fg
            tk.Label(left_f, text=state_text, font=("Segoe UI", 8), fg=state_col, bg=card_bg, anchor="w").pack(fill=tk.X)

            pwr_str = f"{bat}%" if bat >= 0 else "--"
            if ch:
                pwr_col = "#00E676"
            elif 0 <= bat <= warn_th:
                pwr_col = "#FF5252"
            elif not online:
                pwr_col = sub_fg
            else:
                pwr_col = fg

            tk.Label(top_row, text=pwr_str, font=("Segoe UI", 15, "bold"), fg=pwr_col, bg=card_bg).pack(side=tk.RIGHT)

            # Win10 Progress Bar inside card
            bar_canvas = tk.Canvas(card, height=6, bg=track_bg, highlightthickness=0)
            bar_canvas.pack(fill=tk.X, padx=12, pady=(2, 4))
            pct = max(0, min(100, bat if bat >= 0 else 0))
            bar_fill = "#00E676" if ch else ("#FF5252" if (0 <= bat <= warn_th) else ("#0078D7" if online else "#7A7A7A"))
            bar_canvas.update_idletasks()
            bar_w = 282
            fill_len = int(bar_w * pct / 100.0)
            if fill_len > 0:
                bar_canvas.create_rectangle(0, 0, fill_len, 6, fill=bar_fill, outline="")

        # Bottom action bar
        bottom = tk.Frame(container, bg=bg)
        bottom.pack(fill=tk.X, padx=14, pady=(6, 10))

        btn_refresh = tk.Label(bottom, text="⟳ 立即刷新", font=("Segoe UI", 9), fg="#00A2ED", bg=bg, cursor="hand2")
        btn_refresh.pack(side=tk.LEFT, padx=2)
        btn_refresh.bind("<Button-1>", self._on_refresh_click)

        btn_settings = tk.Label(bottom, text="⚙ 设置", font=("Segoe UI", 9), fg=sub_fg, bg=bg, cursor="hand2")
        btn_settings.pack(side=tk.RIGHT, padx=2)
        btn_settings.bind("<Button-1>", self._on_settings_click)

        win.bind("<FocusOut>", self._on_focus_out)
        win.focus_force()

    def _on_refresh_click(self, _event=None):
        def task():
            self.hw.update_mouse()
            self.hw.probe_keyboard("manual", force=True)
            self.tk_root.after(0, self.show)
        threading.Thread(target=task, daemon=True).start()

    def _on_settings_click(self, _event=None):
        self.close()
        self.open_settings_cb()

    def _on_focus_out(self, _event=None):
        def check():
            if not self.win or not self.win.winfo_exists():
                return
            focused = self.win.focus_get()
            if focused is None:
                self.close()
        self.tk_root.after(120, check)

    def close(self):
        if self.win and self.win.winfo_exists():
            self.win.destroy()
        self.win = None


# Win10 Native Style Settings Window (runs on main Tk thread)
class SettingsWindow:
    def __init__(self, tk_root, config, on_saved_cb):
        self.tk_root = tk_root
        self.config = config
        self.on_saved_cb = on_saved_cb
        self.win = None

    def show(self):
        if self.win and self.win.winfo_exists():
            self.win.deiconify()
            self.win.lift()
            self.win.focus_force()
            return

        sw = tk.Toplevel(self.tk_root)
        self.win = sw
        sw.title("2.4G 键鼠电量监控 - 设置")
        sw.geometry("480x680")
        sw.resizable(False, False)

        # Center window on screen
        sw.update_idletasks()
        sx = (sw.winfo_screenwidth() - 480) // 2
        sy = max(0, (sw.winfo_screenheight() - 680) // 2)
        sw.geometry(f"480x680+{sx}+{sy}")

        dark = is_app_dark(self.config)
        bg = "#1F1F1F" if dark else "#F3F3F3"
        card_bg = "#2B2B2B" if dark else "#FFFFFF"
        border_col = "#3A3A3A" if dark else "#E0E0E0"
        fg = "#FFFFFF" if dark else "#111111"
        sub_fg = "#A0A0A0" if dark else "#666666"
        input_bg = "#333333" if dark else "#F9F9F9"

        sw.configure(bg=bg)

        # Title Header (Win10 Settings style)
        hdr = tk.Frame(sw, bg=bg)
        hdr.pack(fill=tk.X, padx=24, pady=(18, 10))
        tk.Label(hdr, text="设置", font=("Segoe UI", 18, "bold"), fg=fg, bg=bg).pack(anchor="w")
        tk.Label(hdr, text="个性化托盘显示、低电量通知与启动选项", font=("Segoe UI", 9), fg=sub_fg, bg=bg).pack(anchor="w")

        body = tk.Frame(sw, bg=bg)
        body.pack(fill=tk.BOTH, expand=True, padx=24)

        def make_card(title):
            frame = tk.Frame(body, bg=card_bg, highlightbackground=border_col, highlightthickness=1)
            frame.pack(fill=tk.X, pady=5, ipady=6, ipadx=12)
            tk.Label(frame, text=title, font=("Segoe UI", 10, "bold"), fg=fg, bg=card_bg).pack(anchor="w", padx=10, pady=(4, 2))
            return frame

        # 1. Tray Style & Order
        c1 = make_card("托盘图标外观与顺序")
        style_var = tk.StringVar(value=self.config.get("style", "bars"))
        row1 = tk.Frame(c1, bg=card_bg)
        row1.pack(fill=tk.X, padx=10, pady=2)
        tk.Radiobutton(
            row1, text="双横向电池条 (推荐，清晰直观)", variable=style_var, value="bars",
            fg=fg, bg=card_bg, selectcolor=card_bg, activebackground=card_bg, activeforeground=fg
        ).pack(side=tk.LEFT, padx=(0, 12))
        tk.Radiobutton(
            row1, text="双行大号数字", variable=style_var, value="digits",
            fg=fg, bg=card_bg, selectcolor=card_bg, activebackground=card_bg, activeforeground=fg
        ).pack(side=tk.LEFT)

        order_var = tk.StringVar(value=self.config.get("order", "mouse_first"))
        row2 = tk.Frame(c1, bg=card_bg)
        row2.pack(fill=tk.X, padx=10, pady=2)
        tk.Label(row2, text="上下顺序：", font=("Segoe UI", 9), fg=sub_fg, bg=card_bg).pack(side=tk.LEFT)
        tk.Radiobutton(
            row2, text="上鼠下键", variable=order_var, value="mouse_first",
            fg=fg, bg=card_bg, selectcolor=card_bg, activebackground=card_bg, activeforeground=fg
        ).pack(side=tk.LEFT, padx=(4, 12))
        tk.Radiobutton(
            row2, text="上键下鼠", variable=order_var, value="keyboard_first",
            fg=fg, bg=card_bg, selectcolor=card_bg, activebackground=card_bg, activeforeground=fg
        ).pack(side=tk.LEFT)

        # 2. Theme Mode
        c2 = make_card("主题与外观")
        theme_var = tk.StringVar(value=self.config.get("theme_mode", "system"))
        row_t = tk.Frame(c2, bg=card_bg)
        row_t.pack(fill=tk.X, padx=10, pady=2)
        for text, val in [("跟随系统", "system"), ("深色", "dark"), ("浅色", "light")]:
            tk.Radiobutton(
                row_t, text=text, variable=theme_var, value=val,
                fg=fg, bg=card_bg, selectcolor=card_bg, activebackground=card_bg, activeforeground=fg
            ).pack(side=tk.LEFT, padx=(0, 16))

        # 3. Low Battery Notification
        c3 = make_card("低电量通知 (Win10 系统通知中心)")
        th_row = tk.Frame(c3, bg=card_bg)
        th_row.pack(fill=tk.X, padx=10, pady=4)

        tk.Label(th_row, text="提醒阈值：", font=("Segoe UI", 9), fg=sub_fg, bg=card_bg).pack(side=tk.LEFT)
        warn_spin = tk.Spinbox(th_row, from_=5, to=50, width=4, bg=input_bg, fg=fg, buttonbackground=card_bg, relief=tk.FLAT)
        warn_spin.delete(0, tk.END)
        warn_spin.insert(0, str(self.config.get("low_battery_warn", 20)))
        warn_spin.pack(side=tk.LEFT, padx=4)
        tk.Label(th_row, text="%", font=("Segoe UI", 9), fg=sub_fg, bg=card_bg).pack(side=tk.LEFT, padx=(0, 16))

        tk.Label(th_row, text="紧急阈值：", font=("Segoe UI", 9), fg=sub_fg, bg=card_bg).pack(side=tk.LEFT)
        crit_spin = tk.Spinbox(th_row, from_=3, to=30, width=4, bg=input_bg, fg=fg, buttonbackground=card_bg, relief=tk.FLAT)
        crit_spin.delete(0, tk.END)
        crit_spin.insert(0, str(self.config.get("low_battery_crit", 10)))
        crit_spin.pack(side=tk.LEFT, padx=4)
        tk.Label(th_row, text="%", font=("Segoe UI", 9), fg=sub_fg, bg=card_bg).pack(side=tk.LEFT)

        # 4. Polling & Autostart
        c4 = make_card("性能与省电设置")
        poll_row = tk.Frame(c4, bg=card_bg)
        poll_row.pack(fill=tk.X, padx=10, pady=2)
        tk.Label(poll_row, text="鼠标刷新：", font=("Segoe UI", 9), fg=sub_fg, bg=card_bg).pack(side=tk.LEFT)
        poll_spin = tk.Spinbox(poll_row, from_=2, to=60, width=4, bg=input_bg, fg=fg, buttonbackground=card_bg, relief=tk.FLAT)
        poll_spin.delete(0, tk.END)
        poll_spin.insert(0, str(self.config.get("poll_interval_sec", 4)))
        poll_spin.pack(side=tk.LEFT, padx=4)
        tk.Label(poll_row, text="秒", font=("Segoe UI", 9), fg=sub_fg, bg=card_bg).pack(side=tk.LEFT)

        kb_enabled_var = tk.BooleanVar(value=self.config.get("keyboard_enabled", True))
        kb_start_var = tk.BooleanVar(value=self.config.get("kb_probe_on_start", True))
        kb_activity_var = tk.BooleanVar(value=self.config.get("kb_activity_detect", True))

        tk.Checkbutton(
            c4, text="启用键盘电量查询（关闭后只显示缓存）", variable=kb_enabled_var,
            font=("Segoe UI", 9), fg=fg, bg=card_bg, selectcolor=card_bg,
            activebackground=card_bg, activeforeground=fg
        ).pack(anchor="w", padx=10, pady=(4, 2))
        tk.Checkbutton(
            c4, text="启动时查询键盘一次", variable=kb_start_var,
            font=("Segoe UI", 9), fg=fg, bg=card_bg, selectcolor=card_bg,
            activebackground=card_bg, activeforeground=fg
        ).pack(anchor="w", padx=10, pady=2)
        tk.Checkbutton(
            c4, text="按键后自动刷新（Raw Input，不记录按键内容）", variable=kb_activity_var,
            font=("Segoe UI", 9), fg=fg, bg=card_bg, selectcolor=card_bg,
            activebackground=card_bg, activeforeground=fg
        ).pack(anchor="w", padx=10, pady=2)
        tk.Label(
            c4, text="休眠时不会主动唤醒，只显示缓存值；日志 monitor.log（256KB 轮转）",
            font=("Segoe UI", 8), fg=sub_fg, bg=card_bg, anchor="w", justify=tk.LEFT
        ).pack(fill=tk.X, padx=12, pady=(2, 2))

        auto_var = tk.BooleanVar(value=self.config.get("start_with_windows", False))
        tk.Checkbutton(
            c4, text="随 Windows 开机自动静默启动", variable=auto_var, font=("Segoe UI", 9),
            fg=fg, bg=card_bg, selectcolor=card_bg, activebackground=card_bg, activeforeground=fg
        ).pack(anchor="w", padx=10, pady=(4, 2))

        # Bottom Action Buttons
        btn_bar = tk.Frame(sw, bg=bg)
        btn_bar.pack(fill=tk.X, padx=24, pady=16)

        def on_test_toast():
            send_toast("低电量提醒测试", "这是一条 Win10 系统原生通知中心测试弹窗。")

        tk.Button(
            btn_bar, text="测试弹窗通知", command=on_test_toast,
            bg="#333333" if dark else "#E1E1E1", fg=fg, relief=tk.FLAT, padx=12, pady=6, cursor="hand2"
        ).pack(side=tk.LEFT)

        def on_save():
            self.config["style"] = style_var.get()
            self.config["order"] = order_var.get()
            self.config["theme_mode"] = theme_var.get()
            try:
                self.config["low_battery_warn"] = max(5, min(50, int(warn_spin.get())))
            except Exception:
                pass
            try:
                self.config["low_battery_crit"] = max(3, min(30, int(crit_spin.get())))
            except Exception:
                pass
            try:
                self.config["poll_interval_sec"] = max(2, min(60, int(poll_spin.get())))
            except Exception:
                pass
            self.config["keyboard_enabled"] = kb_enabled_var.get()
            self.config["kb_probe_on_start"] = kb_start_var.get()
            self.config["kb_activity_detect"] = kb_activity_var.get()
            self.config.pop("kb_poll_interval_sec", None)
            self.config["start_with_windows"] = auto_var.get()
            save_config(self.config)
            self.set_autostart(auto_var.get())
            self.on_saved_cb()
            sw.destroy()
            self.win = None

        tk.Button(
            btn_bar, text="保存并应用", command=on_save,
            bg="#0078D7", fg="#FFFFFF", relief=tk.FLAT, padx=18, pady=6, cursor="hand2"
        ).pack(side=tk.RIGHT)

        sw.protocol("WM_DELETE_WINDOW", lambda: [sw.destroy(), setattr(self, "win", None)])
        sw.lift()
        sw.focus_force()

    def set_autostart(self, enable):
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        app_name = "Win10BatteryMonitor"
        py_dir = os.path.dirname(sys.executable)
        pythonw = os.path.join(py_dir, "pythonw.exe")
        runner = pythonw if os.path.exists(pythonw) else sys.executable
        exe_cmd = f'"{runner}" "{os.path.abspath(__file__)}"'
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
            if enable:
                winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, exe_cmd)
            else:
                try:
                    winreg.DeleteValue(key, app_name)
                except FileNotFoundError:
                    pass
            winreg.CloseKey(key)
        except Exception as e:
            print(f"Registry error: {e}")


# Application Controller
class BatteryApp:
    def __init__(self):
        self.config = load_config()
        self.hw = HardwareMonitor(self.config)
        self.icon = None
        self.watcher = None
        self._tray_sig = None
        self._tray_tip = None

        # Single dedicated Tk root on the main thread
        self.tk_root = tk.Tk()
        self.tk_root.withdraw()

        self.flyout = Win10Flyout(self.tk_root, self.hw, self.open_settings)
        self.settings_win = SettingsWindow(self.tk_root, self.config, self.on_config_updated)

    def _apply_kb_activity_watcher(self):
        want = bool(self.config.get("keyboard_enabled", True)
                    and self.config.get("kb_activity_detect", True))
        if want and self.watcher is None:
            watcher = KeyboardActivityWatcher(self.hw.note_kb_activity)
            watcher.start()
            if watcher.available:
                self.watcher = watcher
                self.hw.set_activity_enabled(True)
                log_event("keyboard activity detection enabled")
            else:
                self.hw.set_activity_enabled(False)
                log_event("keyboard activity detection unavailable; manual refresh only")
        elif not want and self.watcher is not None:
            self.watcher.stop()
            self.watcher = None
            self.hw.set_activity_enabled(False)
            log_event("keyboard activity detection disabled")

    def open_settings(self, _icon=None, _item=None):
        self.tk_root.after(0, self.settings_win.show)

    def on_left_click(self, _icon=None, _item=None):
        self.tk_root.after(0, self.flyout.toggle)

    def on_config_updated(self):
        self.hw.config = self.config
        self._apply_kb_activity_watcher()
        self.refresh_tray(force=True)

    def refresh_tray(self, force=False):
        if not self.icon:
            return
        with self.hw.lock:
            devices = self.hw.get_ordered_devices()
            top_dev, bot_dev = devices[0], devices[1]
            style = self.config.get("style", "bars")
            taskbar_dark = is_taskbar_dark(self.config)
            warn_th = self.config.get("low_battery_warn", 20)
            order = self.config.get("order", "mouse_first")
            sig = (style, taskbar_dark, warn_th, order,
                   tuple((d.name, d.battery, d.charging, d.is_online) for d in devices))
            img = None
            if force or sig != self._tray_sig:
                img = create_tray_image(
                    top_dev, bot_dev,
                    style=style,
                    taskbar_dark=taskbar_dark,
                    warn_th=warn_th
                )

            def fmt_line(pos_tag, d):
                name = "鼠标" if d.icon_type == "mouse" else "键盘"
                if d.charging:
                    st = "充电中"
                elif d.is_online:
                    st = "在线"
                elif getattr(d, "asleep", False):
                    st = "休眠·缓存" if d.battery >= 0 else "休眠"
                elif d.battery >= 0:
                    st = "休眠·缓存"
                else:
                    st = "未连接"
                val = f"{d.battery}%" if d.battery >= 0 else "--"
                upd = time.strftime("%H:%M", time.localtime(d.last_valid_time)) if d.last_valid_time else "--"
                return f"{name}({pos_tag}): {val} ({st}, 更新 {upd})"

            tip = f"{fmt_line('上', top_dev)}\n{fmt_line('下', bot_dev)}"

        if img is not None:
            self.icon.icon = img
            self._tray_sig = sig
        if tip != self._tray_tip:
            self.icon.title = tip
            self._tray_tip = tip

    def worker_loop(self):
        while self.hw.running:
            try:
                self.hw.update_mouse()
                self.hw.tick_keyboard()
                self.refresh_tray()
            except Exception as e:
                log_event(f"worker error: {e!r}")
            time.sleep(self.config.get("poll_interval_sec", 4))

    def run(self):
        log_event("app start")
        self.hw.update_mouse()

        menu = pystray.Menu(
            pystray.MenuItem("打开电量中心", self.on_left_click, default=True),
            pystray.MenuItem("设置", self.open_settings),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self.quit_app)
        )

        devices = self.hw.get_ordered_devices()
        initial_img = create_tray_image(
            devices[0],
            devices[1],
            style=self.config.get("style", "bars"),
            taskbar_dark=is_taskbar_dark(self.config),
            warn_th=self.config.get("low_battery_warn", 20)
        )
        self.icon = pystray.Icon("Win10BatteryMonitor", initial_img, "2.4G 键鼠电量监控", menu)
        self.refresh_tray(force=True)

        self._apply_kb_activity_watcher()

        # Run pystray in background thread, keep Tkinter mainloop on main thread
        t_tray = threading.Thread(target=self.icon.run, daemon=True)
        t_tray.start()

        t_worker = threading.Thread(target=self.worker_loop, daemon=True)
        t_worker.start()

        self.tk_root.mainloop()

    def quit_app(self, _icon=None, _item=None):
        self.hw.running = False
        if self.watcher:
            try:
                self.watcher.stop()
            except Exception:
                pass
            self.watcher = None
        if self.icon:
            self.icon.stop()
        try:
            self.tk_root.after(0, self.tk_root.quit)
        except Exception:
            pass
        log_event("app exit")
        os._exit(0)


if __name__ == "__main__":
    _already_running = False
    try:
        _mutex = win32event.CreateMutex(None, False, "Win10BatteryMonitor_SingleInstance_Mutex")
        _already_running = (win32api.GetLastError() == 183)  # ERROR_ALREADY_EXISTS
    except pywintypes.error as e:
        # 5 = access denied: the named mutex exists but this token cannot open it
        # with full access (e.g. another instance / elevated instance). Exit quietly.
        _already_running = (getattr(e, "winerror", None) == 5)
    if _already_running:
        log_event("single-instance: another instance is running, exit")
        try:
            ctypes.windll.user32.MessageBoxW(
                None,
                "检测到已有实例在运行（可能是旧版）。\n"
                "请先在任务栏托盘右键点击旧图标并选择“退出”，再重新启动新版。\n"
                "如果托盘里找不到旧图标，请重启电脑后再启动。",
                "2.4G 键鼠电量监控",
                0x00000040,  # MB_ICONINFORMATION
            )
        except Exception:
            pass
        sys.exit(0)
    app = BatteryApp()
    app.run()
