from __future__ import annotations

import ctypes
import faulthandler
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

import webview

APP_ID = "chaoxing.extractor.desktop"
APP_VERSION = "2.5.1-online-stats-layout"
WEBVIEW_PROFILE_SCHEMA = "3"
_MUTEX_NAME = r"Local\ChaoxingExtractor.Desktop.Singleton"
_ERROR_ALREADY_EXISTS = 183
_mutex_handle: int | None = None
_diagnostic_stream = None
_shutdown_started = threading.Event()
_shutdown_done = threading.Event()
_startup_completed = threading.Event()
_window_loaded = threading.Event()
_startup_lock = threading.Lock()
_main_window_hwnd: int | None = None
_startup_sentinel: Path | None = None
_webview_profile_path: Path | None = None
logger = logging.getLogger("chaoxing")


def resource_path(name: str) -> str:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return str(base / name)


def runtime_data_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent / ".runtime"


def reserve_local_http_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def window_geometry_for_system_dpi() -> tuple[int, int, tuple[int, int]]:
    """Return logical dimensions that fit the primary monitor work area."""
    if not sys.platform.startswith("win"):
        return 1220, 820, (980, 680)
    user32 = ctypes.windll.user32
    try:
        dpi = int(user32.GetDpiForSystem())
    except Exception:
        dpi = 96
    scale = max(1.0, dpi / 96.0)
    work_area = wintypes.RECT()
    if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work_area), 0):
        work_width = work_area.right - work_area.left
        work_height = work_area.bottom - work_area.top
    else:
        work_width = int(user32.GetSystemMetrics(0))
        work_height = int(user32.GetSystemMetrics(1))
    logical_work_width = round(work_width / scale)
    logical_work_height = round(work_height / scale)
    width = min(1220, max(980, logical_work_width - 52))
    height = min(820, max(680, logical_work_height - 52))
    min_width = min(980, width)
    min_height = min(680, height)
    logger.info(
        "Window geometry dpi=%s scale=%.2f work=%sx%s size=%sx%s",
        dpi,
        scale,
        work_width,
        work_height,
        width,
        height,
    )
    return width, height, (min_width, min_height)


def set_process_dpi_awareness() -> None:
    if not sys.platform.startswith("win"):
        return
    user32 = ctypes.windll.user32
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def select_webview_data_path() -> Path:
    """Reuse a clean private WebView profile and rebuild it after a crash."""
    global _webview_profile_path
    root = runtime_data_root() / "webview_data"
    root.mkdir(parents=True, exist_ok=True)
    profile = root / "profile"
    clean_marker = profile / ".clean-exit"
    legacy_profiles = list(root.glob("session-*")) + [root / "slot-a", root / "slot-b"]
    needs_process_cleanup = (
        any(path.exists() for path in legacy_profiles)
        or (profile.exists() and not clean_marker.is_file())
    )

    # Only terminate WebView2 processes whose command line points at this
    # application's private data root. Ordinary Edge windows are untouched.
    if needs_process_cleanup:
        try:
            import psutil

            needle = str(root.resolve(strict=False)).lower()
            targets = []
            for process in psutil.process_iter(["name", "cmdline"]):
                try:
                    name = str(process.info.get("name") or "").lower()
                    command = " ".join(process.info.get("cmdline") or []).lower()
                    if "msedgewebview2" in name and needle in command:
                        process.terminate()
                        targets.append(process)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            _gone, alive = psutil.wait_procs(targets, timeout=1.5)
            for process in alive:
                try:
                    process.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            if targets:
                logger.warning("Cleaned %s stale private WebView2 processes", len(targets))
        except Exception:
            logger.exception("Could not clean stale private WebView2 processes")

    for stale in legacy_profiles:
        if stale.is_dir():
            try:
                shutil.rmtree(stale)
            except OSError:
                logger.warning("Could not remove stale WebView session: %s", stale)
    (root / "active-slot.txt").unlink(missing_ok=True)

    schema_marker = profile / ".profile-schema"
    reusable = False
    if profile.is_dir():
        try:
            reusable = (
                clean_marker.is_file()
                and schema_marker.read_text(encoding="ascii").strip() == WEBVIEW_PROFILE_SCHEMA
            )
        except OSError:
            reusable = False
        if not reusable:
            logger.warning("Rebuilding WebView profile after unclean exit or schema change: %s", profile)
            try:
                shutil.rmtree(profile)
            except OSError:
                logger.warning("Could not remove unusable WebView profile: %s", profile)
                profile = root / f"session-{os.getpid()}"
                clean_marker = profile / ".clean-exit"
                schema_marker = profile / ".profile-schema"

    profile.mkdir(parents=True, exist_ok=True)
    try:
        schema_marker.write_text(WEBVIEW_PROFILE_SCHEMA, encoding="ascii")
    except OSError:
        logger.exception("Could not write WebView profile schema marker: %s", schema_marker)
    clean_marker.unlink(missing_ok=True)
    _webview_profile_path = profile
    logger.info("%s private WebView profile: %s", "Reusing" if reusable else "Created", profile)
    return profile


def mark_webview_profile_clean() -> None:
    profile = _webview_profile_path
    if profile is None or not _window_loaded.is_set():
        return
    try:
        (profile / ".clean-exit").write_text("clean", encoding="ascii")
        logger.info("Marked WebView profile as cleanly closed: %s", profile)
    except OSError:
        logger.exception("Could not mark WebView profile as cleanly closed: %s", profile)
        return


def setup_diagnostics() -> None:
    global _diagnostic_stream
    log_dir = runtime_data_root() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "app.log"

    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = RotatingFileHandler(log_path, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
    logger.addHandler(handler)

    _diagnostic_stream = (log_dir / "hang-stacks.log").open("a", encoding="utf-8")
    try:
        faulthandler.enable(_diagnostic_stream, all_threads=True)
    except Exception:
        pass

    def log_exception(exc_type, exc_value, exc_traceback) -> None:
        logger.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_traceback))

    sys.excepthook = log_exception
    if hasattr(threading, "excepthook"):
        threading.excepthook = lambda args: logger.critical(
            "Unhandled thread exception in %s",
            args.thread.name if args.thread else "unknown",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
    logger.info("Starting version=%s pid=%s frozen=%s exe=%s", APP_VERSION, os.getpid(), getattr(sys, "frozen", False), sys.executable)


def _bring_existing_window_forward() -> None:
    if not sys.platform.startswith("win"):
        return
    current_pid = os.getpid()
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def callback(hwnd: int, _lparam: int) -> bool:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == current_pid or not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        if title.value == "超星作业抓取助手":
            user32.ShowWindow(hwnd, 9)
            user32.SetForegroundWindow(hwnd)
            return False
        return True

    user32.EnumWindows(callback, 0)


def acquire_single_instance() -> bool:
    global _mutex_handle
    if not sys.platform.startswith("win"):
        return True
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel32.CreateMutexW(None, True, _MUTEX_NAME)
    if not handle:
        logger.warning("CreateMutexW failed error=%s", kernel32.GetLastError())
        return True
    if kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        _bring_existing_window_forward()
        logger.info("Existing instance detected; exiting the duplicate launcher")
        return False
    _mutex_handle = int(handle)
    return True


def release_single_instance() -> None:
    global _mutex_handle
    if _mutex_handle and sys.platform.startswith("win"):
        try:
            ctypes.windll.kernel32.ReleaseMutex(wintypes.HANDLE(_mutex_handle))
            ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(_mutex_handle))
        except Exception:
            pass
    _mutex_handle = None


def set_windows_app_id() -> None:
    if not sys.platform.startswith("win"):
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


def apply_windows_window_icon() -> None:
    """Apply the bundled icon to every top-level window owned by this process."""
    if not sys.platform.startswith("win"):
        return
    icon_path = resource_path("logo.ico")
    if not Path(icon_path).exists():
        return

    try:
        user32 = ctypes.windll.user32
        image_icon = 1
        load_from_file = 0x0010
        wm_seticon = 0x0080
        icon_small = 0
        icon_big = 1
        small_size = max(16, int(user32.GetSystemMetrics(49)))
        big_size = max(32, int(user32.GetSystemMetrics(11)))

        user32.LoadImageW.restype = wintypes.HANDLE
        small_handle = user32.LoadImageW(
            None, icon_path, image_icon, small_size, small_size, load_from_file
        )
        big_handle = user32.LoadImageW(
            None, icon_path, image_icon, big_size, big_size, load_from_file
        )
        if not small_handle and not big_handle:
            return

        current_pid = os.getpid()
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def set_icon(hwnd: int, _lparam: int) -> bool:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value != current_pid:
                return True
            if small_handle:
                user32.SendMessageW(hwnd, wm_seticon, icon_small, small_handle)
            if big_handle:
                user32.SendMessageW(hwnd, wm_seticon, icon_big, big_handle)
            return True

        user32.EnumWindows(set_icon, 0)
    except Exception:
        pass


def schedule_window_icon_refresh() -> None:
    """Refresh twice because WebView2 can replace the native window icon late."""
    def worker() -> None:
        for delay in (0.0, 0.8, 2.0):
            if delay and _shutdown_started.wait(delay):
                return
            if _shutdown_started.is_set():
                return
            apply_windows_window_icon()

    threading.Thread(target=worker, daemon=True, name="window-icon").start()


def request_shutdown(api: Any) -> None:
    """Start cleanup away from the native UI thread and return immediately."""
    if _shutdown_started.is_set():
        return
    _shutdown_started.set()

    def worker() -> None:
        logger.info("Background shutdown started")
        try:
            api.shutdown()
        except Exception:
            logger.exception("Background shutdown failed")
        finally:
            _shutdown_done.set()
            logger.info("Background shutdown request completed")

    threading.Thread(target=worker, daemon=True, name="app-shutdown").start()


def _find_own_window(visible_only: bool = True) -> int | None:
    if not sys.platform.startswith("win"):
        return None
    user32 = ctypes.windll.user32
    current_pid = os.getpid()
    found: list[int] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def callback(hwnd: int, _lparam: int) -> bool:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == current_pid and (not visible_only or user32.IsWindowVisible(hwnd)):
            found.append(int(hwnd))
            return False
        return True

    user32.EnumWindows(callback, 0)
    return found[0] if found else None


def start_ui_watchdog() -> None:
    if not sys.platform.startswith("win"):
        return

    def worker() -> None:
        user32 = ctypes.windll.user32
        wm_null = 0x0000
        smto_abort_if_hung = 0x0002
        consecutive_timeouts = 0
        while not _shutdown_started.wait(3):
            hwnd = _find_own_window()
            if not hwnd:
                continue
            result = ctypes.c_size_t()
            ok = user32.SendMessageTimeoutW(
                wintypes.HWND(hwnd),
                wm_null,
                0,
                0,
                smto_abort_if_hung,
                1200,
                ctypes.byref(result),
            )
            consecutive_timeouts = 0 if ok else consecutive_timeouts + 1
            if consecutive_timeouts >= 2:
                logger.error("Native window did not answer WM_NULL twice; dumping Python stacks")
                try:
                    faulthandler.dump_traceback(file=_diagnostic_stream, all_threads=True)
                    _diagnostic_stream.flush()
                except Exception:
                    pass
                consecutive_timeouts = 0

    threading.Thread(target=worker, daemon=True, name="ui-watchdog").start()


def install_startup_click_through(window: Any) -> None:
    """Make the native form ignore mouse input without disabling WebView2."""
    global _main_window_hwnd
    if not sys.platform.startswith("win") or _startup_completed.is_set():
        return
    try:
        from webview.platforms.winforms import BrowserView

        form = BrowserView.instances.get(window.uid)
        if form is None:
            raise RuntimeError("WinForms window instance is unavailable")
        hwnd = int(form.Handle.ToInt64())
        user32 = ctypes.windll.user32
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        style = user32.GetWindowLongPtrW(wintypes.HWND(hwnd), -20)
        user32.SetWindowLongPtrW(wintypes.HWND(hwnd), -20, style | 0x20)
        _main_window_hwnd = hwnd
        logger.info("Startup mouse click-through enabled before first show")
    except Exception:
        logger.exception("Could not enable startup mouse click-through")


def remove_startup_click_through() -> None:
    hwnd = _main_window_hwnd
    if not hwnd or not sys.platform.startswith("win"):
        return
    try:
        user32 = ctypes.windll.user32
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        style = user32.GetWindowLongPtrW(wintypes.HWND(hwnd), -20)
        user32.SetWindowLongPtrW(wintypes.HWND(hwnd), -20, style & ~0x20)
        user32.SetWindowPos(
            wintypes.HWND(hwnd), None, 0, 0, 0, 0,
            0x0001 | 0x0002 | 0x0004 | 0x0020,
        )
        user32.SetForegroundWindow(wintypes.HWND(hwnd))
    except Exception:
        logger.exception("Could not remove startup mouse click-through")


def on_window_loaded(window: Any, ready_callback) -> None:
    # WebView2/WinForms can deadlock if the user starts a native title-bar drag
    # while the controller is still being initialized. This window has no
    # native frame, and the HTML title bar remains hidden until settings load.
    _window_loaded.set()
    logger.info("Window content loaded; waiting for frontend startup handshake")


def start_external_startup_watchdog(webview_profile: Path) -> None:
    """Use another process because WebView2 initialization can hold this process's GIL."""
    global _startup_sentinel
    if not getattr(sys, "frozen", False):
        return
    sentinel_dir = runtime_data_root() / "logs"
    sentinel_dir.mkdir(parents=True, exist_ok=True)
    _startup_sentinel = sentinel_dir / f"startup-{os.getpid()}.ready"
    _startup_sentinel.unlink(missing_ok=True)
    retries = int(os.environ.get("CHAOXING_STARTUP_RETRY", "0") or 0)
    subprocess.Popen(
        [
            sys.executable,
            "--startup-watchdog",
            str(os.getpid()),
            str(_startup_sentinel),
            str(retries),
            str(webview_profile),
        ],
        cwd=str(Path(sys.executable).resolve().parent),
        creationflags=0x08000000,
        close_fds=True,
    )
    logger.info("External startup watchdog started attempt=%s timeout=24s", retries + 1)


def terminate_process_tree(root_pid: int, excluded_pid: int) -> None:
    """Terminate only descendants of this app, while keeping the watchdog alive."""
    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    parent_by_pid: dict[int, int] = {}
    if snapshot not in (0, ctypes.c_void_p(-1).value):
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(ProcessEntry)
        if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                parent_by_pid[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
        kernel32.CloseHandle(snapshot)

    descendants = {int(root_pid)}
    changed = True
    while changed:
        changed = False
        for pid, parent_pid in parent_by_pid.items():
            if parent_pid in descendants and pid not in descendants and pid != excluded_pid:
                descendants.add(pid)
                changed = True
    descendants.discard(excluded_pid)
    for pid in sorted(descendants, reverse=True):
        handle = kernel32.OpenProcess(0x0001 | 0x00100000, False, pid)
        if handle:
            kernel32.TerminateProcess(wintypes.HANDLE(handle), 75)
            kernel32.WaitForSingleObject(wintypes.HANDLE(handle), 3000)
            kernel32.CloseHandle(wintypes.HANDLE(handle))


def remove_failed_webview_profile(profile_arg: str) -> None:
    profile = Path(profile_arg).resolve(strict=False)
    expected_root = (Path(sys.executable).resolve().parent / "webview_data").resolve(strict=False)
    try:
        profile.relative_to(expected_root)
    except ValueError:
        return
    if profile.name not in {"profile", "slot-a", "slot-b"} and not re.fullmatch(r"session-\d+", profile.name):
        return
    for _attempt in range(6):
        try:
            shutil.rmtree(profile, ignore_errors=False)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.35)


def run_startup_watchdog(parent_pid: int, sentinel_arg: str, retries: int, profile_arg: str) -> int:
    """Independent frozen-process entry point; never imports or starts pywebview."""
    if not sys.platform.startswith("win"):
        return 1
    sentinel = Path(sentinel_arg)
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x00100000, False, int(parent_pid))
    if not handle:
        return 0
    deadline = time.monotonic() + 24.0
    while time.monotonic() < deadline:
        if sentinel.exists():
            sentinel.unlink(missing_ok=True)
            kernel32.CloseHandle(wintypes.HANDLE(handle))
            return 0
        if kernel32.WaitForSingleObject(wintypes.HANDLE(handle), 200) == 0:
            sentinel.unlink(missing_ok=True)
            kernel32.CloseHandle(wintypes.HANDLE(handle))
            return 0

    if sentinel.exists():
        sentinel.unlink(missing_ok=True)
        kernel32.CloseHandle(wintypes.HANDLE(handle))
        return 0
    kernel32.CloseHandle(wintypes.HANDLE(handle))
    terminate_process_tree(parent_pid, os.getpid())
    remove_failed_webview_profile(profile_arg)
    sentinel.unlink(missing_ok=True)
    time.sleep(0.8)
    if retries < 2:
        env = os.environ.copy()
        env["CHAOXING_STARTUP_RETRY"] = str(retries + 1)
        subprocess.Popen(
            [sys.executable],
            cwd=str(Path(sys.executable).resolve().parent),
            env=env,
            creationflags=0x08000000,
            close_fds=True,
        )
        return 75
    ctypes.windll.user32.MessageBoxW(
        None,
        "界面组件连续三次启动失败，请重新打开程序。",
        "超星作业抓取助手",
        0x10,
    )
    return 1


def complete_startup(window: Any = None) -> None:
    with _startup_lock:
        if _startup_completed.is_set():
            return
        logger.info("Frontend startup handshake received")
        if _startup_sentinel is not None:
            try:
                _startup_sentinel.write_text("ready", encoding="ascii")
            except OSError:
                logger.exception("Could not notify the external startup watchdog")
        _startup_completed.set()
    if window is not None:
        try:
            window.show()
            logger.info("Initialized window is now visible")
        except Exception:
            logger.exception("Could not show initialized window")
    remove_startup_click_through()
    schedule_window_icon_refresh()
    with _startup_lock:
        logger.info("Frontend ready; frameless window controls unlocked")
    start_ui_watchdog()


def run_packaged_self_test(output_path: str) -> int:
    """Exercise deferred imports and the offline parse/export path."""
    import json
    import tempfile
    import traceback
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    report: dict[str, Any] = {"ok": False, "version": APP_VERSION}
    try:
        from DrissionPage import ChromiumOptions
        from chaoxing_auto import chrome_tmp_root
        from chaoxing_batch import parse_mhtml
        from chaoxing_scraper import save_docx

        html = """<!doctype html><html><head><title>离线自检作业</title></head><body>
        <div class="aiAreaContent">
          <h3 class="mark_name">1. <span class="colorShallow">单选题</span><span class="qtContent">稳定版自检题目</span></h3>
          <ul class="mark_letter"><li>A. 正确选项</li><li>B. 其他选项</li></ul>
          <span class="stuAnswerContent">A</span><span class="rightAnswerContent">A</span>
          <div class="totalScore">1分</div><span class="marking_dui"></span>
        </div></body></html>"""

        selftest_root = runtime_data_root()
        selftest_root.mkdir(parents=True, exist_ok=True)
        expected_chrome_root = selftest_root / "chrome"
        if chrome_tmp_root().resolve(strict=False) != expected_chrome_root.resolve(strict=False):
            raise RuntimeError("Chrome 临时目录没有指向程序目录")
        with tempfile.TemporaryDirectory(prefix="chaoxing-selftest-", dir=selftest_root) as temp_dir:
            temp = Path(temp_dir)
            message = MIMEMultipart("related")
            part = MIMEText(html, "html", "utf-8")
            part.add_header("Content-Location", "https://mooc1.chaoxing.com/work/doHomeWorkNew")
            message.attach(part)
            mhtml_path = temp / "selftest.mhtml"
            mhtml_path.write_bytes(message.as_bytes())

            work = parse_mhtml(str(mhtml_path))
            if not work or len(work.get("questions", [])) != 1:
                raise RuntimeError("离线 MHTML 解析自检失败")
            docx_path = temp / "selftest.docx"
            save_docx([work], str(docx_path), with_images=False)
            if not docx_path.exists() or docx_path.stat().st_size < 1000:
                raise RuntimeError("Word 生成自检失败")

            options = ChromiumOptions()
            report.update(
                ok=True,
                parsed_questions=1,
                docx_bytes=docx_path.stat().st_size,
                drission_options=options.__class__.__name__,
                chrome_root=str(chrome_tmp_root()),
            )
    except Exception as exc:
        report.update(error=str(exc), traceback=traceback.format_exc())

    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report.get("ok") else 1


def run_packaged_browser_self_test(output_path: str) -> int:
    """Verify the packaged browser path and the real Chaoxing login endpoint."""
    import json
    import traceback

    report: dict[str, Any] = {"ok": False, "version": APP_VERSION}
    page = None
    try:
        from chaoxing_auto import chrome_tmp_root, cleanup_chrome_tmp_processes, make_page

        page = make_page(show_browser=False)
        page.get("https://passport2.chaoxing.com/login?refer=https://i.chaoxing.com")
        current_url = str(page.url or "")
        title = str(page.title or "")
        if "chaoxing.com" not in current_url:
            raise RuntimeError(f"超星登录页访问失败：{current_url}")
        report.update(
            ok=True,
            url=current_url,
            title=title,
            chrome_root=str(chrome_tmp_root()),
        )
    except Exception as exc:
        report.update(error=str(exc), traceback=traceback.format_exc())
    finally:
        if page is not None:
            try:
                page.quit()
            except Exception:
                pass
        try:
            from chaoxing_auto import cleanup_chrome_tmp_processes

            cleanup_chrome_tmp_processes()
        except Exception:
            pass
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report.get("ok") else 1


def run_packaged_browser_visibility_self_test(output_path: str) -> int:
    """Verify that a packaged headed browser can hide, stay connected, and show again."""
    import json
    import time
    import traceback

    report: dict[str, Any] = {"ok": False, "version": APP_VERSION}
    page = None
    try:
        from chaoxing_auto import (
            cleanup_chrome_tmp_processes,
            detect_browser_choices,
            make_page,
            set_browser_window_visible,
        )

        choices = detect_browser_choices()
        page = make_page(show_browser=True, browser_choice="auto")
        page.get("https://passport2.chaoxing.com/login?refer=https://i.chaoxing.com")
        hidden = set_browser_window_visible(page, False)
        time.sleep(0.6)
        hidden_alive = bool(page.run_cdp("Browser.getVersion")) and "chaoxing.com" in str(page.url)
        shown = set_browser_window_visible(page, True)
        time.sleep(0.35)
        report.update(
            ok=bool(hidden and hidden_alive and shown),
            detected=[choice.get("name", "") for choice in choices],
            hidden=hidden,
            hidden_alive=hidden_alive,
            shown=shown,
        )
    except Exception as exc:
        report.update(error=str(exc), traceback=traceback.format_exc())
    finally:
        if page is not None:
            try:
                page.quit()
            except Exception:
                pass
        try:
            from chaoxing_auto import cleanup_chrome_tmp_processes

            cleanup_chrome_tmp_processes()
        except Exception:
            pass
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report.get("ok") else 1


def main() -> None:
    setup_diagnostics()
    if not acquire_single_instance():
        return
    set_process_dpi_awareness()
    set_windows_app_id()
    try:
        # Import the processing backend only after the single-instance check so
        # a duplicate launch stays cheap and exits without loading heavy modules.
        from backend import Api

        api = Api()
        initial_settings = api.get_initial_settings()
        if not initial_settings.get("ok"):
            logger.warning("Initial settings load failed: %s", initial_settings.get("error"))
        webview_data = select_webview_data_path()
        local_http_port = reserve_local_http_port()
        logger.info("Reserved local UI port=%s", local_http_port)
        theme = "light" if initial_settings.get("settings", {}).get("theme") == "light" else "dark"
        app_url = resource_path("app.html")
        window_width, window_height, minimum_size = window_geometry_for_system_dpi()
        window = webview.create_window(
            "超星作业抓取助手",
            app_url,
            js_api=api,
            width=window_width,
            height=window_height,
            min_size=minimum_size,
            frameless=True,
            easy_drag=False,
            hidden=True,
            background_color="#f5f2eb" if theme == "light" else "#111115",
        )
        api.bind_window(window)
        ready_callback = lambda: complete_startup(window)
        api.bind_frontend_ready(ready_callback)

        window.events.before_show += install_startup_click_through
        window.events.loaded += lambda: on_window_loaded(window, ready_callback)
        window.events.closing += lambda: request_shutdown(api)
        window.events.closed += lambda: request_shutdown(api)

        start_external_startup_watchdog(webview_data)
        webview.start(
            gui="edgechromium",
            debug=False,
            private_mode=False,
            storage_path=str(webview_data),
            http_port=local_http_port,
            icon=resource_path("logo.ico"),
        )
        mark_webview_profile_clean()
    finally:
        if "api" in locals():
            request_shutdown(api)
            # The window is already gone here, so a short cleanup wait cannot
            # make the UI appear hung. Never force-kill the app or its bootloader.
            _shutdown_done.wait(timeout=1.5)
        logger.info("Application exit")
        release_single_instance()


if __name__ == "__main__":
    if len(sys.argv) >= 6 and sys.argv[1] == "--startup-watchdog":
        raise SystemExit(
            run_startup_watchdog(int(sys.argv[2]), sys.argv[3], int(sys.argv[4]), sys.argv[5])
        )
    if len(sys.argv) >= 3 and sys.argv[1] == "--self-test":
        raise SystemExit(run_packaged_self_test(sys.argv[2]))
    if len(sys.argv) >= 3 and sys.argv[1] == "--browser-self-test":
        raise SystemExit(run_packaged_browser_self_test(sys.argv[2]))
    if len(sys.argv) >= 3 and sys.argv[1] == "--browser-visibility-self-test":
        raise SystemExit(run_packaged_browser_visibility_self_test(sys.argv[2]))
    main()
