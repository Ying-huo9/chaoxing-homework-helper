from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any
from ctypes import wintypes

import webview


_SETTINGS_VERSION = 3
_SETTINGS_LOCK = threading.RLock()
logger = logging.getLogger("chaoxing")

_BROWSER_CLOSED_MESSAGE = "浏览器已关闭，请重新点击登录"
_BROWSER_VERIFICATION_MESSAGE = "登录状态失效，请在浏览器完成验证后点击「我已完成」"


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_filename(value: str, fallback: str = "未命名") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", value or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned or fallback)[:120]


def _config_path() -> Path:
    return _app_root() / "settings.json"


def _legacy_config_path() -> Path:
    base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    return base / "ChaoxingExtractor" / "settings.json"


def _app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _prepare_config_path() -> Path:
    """Return the portable config path and migrate the former AppData file."""
    portable = _config_path()
    if not getattr(sys, "frozen", False):
        return portable

    legacy = _legacy_config_path()
    if not legacy.exists():
        return portable

    try:
        portable.parent.mkdir(parents=True, exist_ok=True)
        if not portable.exists():
            raw = legacy.read_bytes()
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("旧设置文件不是有效对象")
            tmp = portable.with_suffix(".json.migrating")
            tmp.write_bytes(raw)
            os.replace(tmp, portable)
            logger.info("Migrated settings from %s to %s", legacy, portable)

        # The portable copy is now authoritative; remove the old C-drive copy.
        legacy.unlink(missing_ok=True)
        try:
            legacy.parent.rmdir()
        except OSError:
            pass
    except Exception:
        logger.exception("Failed to migrate settings from %s to %s", legacy, portable)
    return portable


def _default_output_root() -> Path:
    return _app_root()


def _legacy_output_root() -> Path:
    return Path.home() / "Documents" / "超星抓取结果"


def _is_legacy_output_root(value: Any) -> bool:
    if not value:
        return False
    try:
        return Path(str(value)).expanduser().resolve(strict=False) == _legacy_output_root().resolve(strict=False)
    except Exception:
        return str(value).strip() == str(_legacy_output_root())


def _settings_defaults() -> dict[str, Any]:
    return {
        "settings_version": _SETTINGS_VERSION,
        "remember": True,
        "username": "",
        "password": "",
        "active_mode": "local",
        "theme": "dark",
        "online_content_mode": "homework",
        "local_folder": "",
        "local_with_images": True,
        "local_course_info": False,
        "local_course_name": "",
        "local_class_name": "",
        "local_student_name": "",
        "online_with_images": True,
        "online_resume": True,
        "online_browser_choice": "auto",
        "online_browser_visible": False,
        "online_output_root": str(_default_output_root()),
        "online_output_custom": False,
        "default_output_root": str(_default_output_root()),
    }


def _blob_from_bytes(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    return blob, buffer


def _protect_text(value: str) -> str:
    if not value:
        return ""
    data = value.encode("utf-8")
    blob_in, _buffer = _blob_from_bytes(data)
    blob_out = _DataBlob()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError("无法加密保存密码")
    try:
        protected = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return base64.b64encode(protected).decode("ascii")


def _unprotect_text(value: str) -> str:
    if not value:
        return ""
    try:
        data = base64.b64decode(value)
    except Exception:
        return ""
    blob_in, _buffer = _blob_from_bytes(data)
    blob_out = _DataBlob()
    description = wintypes.LPWSTR()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        ctypes.byref(description),
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    )
    if not ok:
        return ""
    try:
        plain = ctypes.string_at(blob_out.pbData, blob_out.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        if description:
            ctypes.windll.kernel32.LocalFree(description)
    return plain


def _stats(results: list[dict[str, Any]]) -> dict[str, Any]:
    total_q = sum(len(w.get("questions", [])) for w in results)
    total_ok = sum(
        sum(1 for q in w.get("questions", []) if q.get("是否正确") is True)
        for w in results
    )
    total_score = sum(
        sum(q.get("得分") or 0 for q in w.get("questions", []))
        for w in results
    )
    return {
        "work_count": len(results),
        "question_count": total_q,
        "correct_count": total_ok,
        "total_score": round(float(total_score), 1),
    }


def _normalized_work_title(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _work_content_fingerprint(work: dict[str, Any]) -> str:
    questions = work.get("questions") or []
    payload = json.dumps(questions, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest() if questions else ""


class Api:
    def __init__(self) -> None:
        self.window: webview.Window | None = None
        self.frontend_ready_callback = None
        self.startup_unlock_scheduled = False
        self.startup_unlock_lock = threading.Lock()
        self.jobs: dict[str, dict[str, Any]] = {}
        self.jobs_lock = threading.Lock()
        self.page = None
        self.page_lock = threading.Lock()
        self.active_browser_choice = ""
        self.browser_always_visible = False
        self.browser_window_hidden = False
        self.browser_verification_required = False
        self.content_lock = threading.Lock()
        self.content_generation = 0
        self.active_content_job_id: str | None = None
        self.courses: list[dict[str, Any]] = []
        self.tasks_by_course: dict[str, list[dict[str, Any]]] = {}

    def bind_window(self, window: webview.Window) -> None:
        self.window = window

    def bind_frontend_ready(self, callback) -> None:
        self.frontend_ready_callback = callback

    def _schedule_startup_unlock(self) -> None:
        with self.startup_unlock_lock:
            if self.startup_unlock_scheduled:
                return
            self.startup_unlock_scheduled = True

        def unlock() -> None:
            time.sleep(0.35)
            callback = self.frontend_ready_callback
            if callback is not None:
                logger.info("Unlocking startup after settings API became available")
                callback()

        threading.Thread(target=unlock, daemon=True, name="settings-startup-unlock").start()

    def frontend_ready(self) -> dict[str, Any]:
        logger.info("Backend received frontend_ready API call")
        callback = self.frontend_ready_callback
        if callback is not None:
            callback()
        return {"ok": True}

    def startup_ready(self) -> dict[str, Any]:
        logger.info("Backend received startup_ready API call")
        callback = self.frontend_ready_callback
        if callback is not None:
            callback()
        return {"ok": True}

    def window_minimize(self) -> dict[str, Any]:
        if self.window is not None:
            self.window.minimize()
        return {"ok": True}

    def window_set_maximized(self, maximized: bool) -> dict[str, Any]:
        if self.window is not None:
            if maximized:
                self.window.maximize()
            else:
                self.window.restore()
        return {"ok": True}

    def window_close(self) -> dict[str, Any]:
        window = self.window
        if window is not None:
            def close_later() -> None:
                time.sleep(0.05)
                window.destroy()
            threading.Thread(target=close_later, daemon=True, name="window-close").start()
        return {"ok": True}

    def _choose_directory(self, title: str) -> str:
        if self.window is None:
            return ""
        try:
            result = self.window.create_file_dialog(
                webview.FileDialog.FOLDER,
                allow_multiple=False,
            )
        except Exception:
            result = self.window.create_file_dialog(
                webview.FOLDER_DIALOG,
                allow_multiple=False,
            )
        if not result:
            return ""
        return str(result[0])

    def get_initial_settings(self) -> dict[str, Any]:
        logger.info("Backend received native initial settings request")
        return self._read_settings()

    def get_settings(self) -> dict[str, Any]:
        logger.info("Backend received frontend get_settings API call")
        self._schedule_startup_unlock()
        return self._read_settings()

    def _read_settings(self) -> dict[str, Any]:
        path = _prepare_config_path()
        defaults = _settings_defaults()
        with _SETTINGS_LOCK:
            if not path.exists():
                return {"ok": True, "settings": defaults}
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
                remember = bool(data.get("remember", defaults["remember"]))
                settings = {**defaults, **data}
                settings["settings_version"] = _SETTINGS_VERSION
                settings["remember"] = remember
                settings["username"] = str(data.get("username") or "") if remember else ""
                settings["password"] = _unprotect_text(data.get("password") or "") if remember else ""
                settings["active_mode"] = "online" if data.get("active_mode") == "online" else "local"
                settings["theme"] = "light" if data.get("theme") == "light" else "dark"
                settings["online_content_mode"] = "chapter" if data.get("online_content_mode") == "chapter" else "homework"
                settings["local_folder"] = str(data.get("local_folder") or "")
                settings["local_with_images"] = bool(data.get("local_with_images", defaults["local_with_images"]))
                settings["local_course_info"] = bool(data.get("local_course_info", defaults["local_course_info"]))
                settings["local_course_name"] = str(data.get("local_course_name") or "")
                settings["local_class_name"] = str(data.get("local_class_name") or "")
                settings["local_student_name"] = str(data.get("local_student_name") or "")
                settings["online_with_images"] = bool(data.get("online_with_images", defaults["online_with_images"]))
                settings["online_resume"] = bool(data.get("online_resume", defaults["online_resume"]))
                settings["online_browser_choice"] = str(data.get("online_browser_choice") or "auto")
                settings["online_browser_visible"] = bool(data.get("online_browser_visible", defaults["online_browser_visible"]))

                # A default output directory follows the executable when it is moved.
                # Older builds stored their temporary dist directory as if it were custom.
                stored_root = data.get("online_output_root")
                output_custom = bool(data.get("online_output_custom", False))
                if not output_custom or _is_legacy_output_root(stored_root):
                    settings["online_output_root"] = defaults["online_output_root"]
                    settings["online_output_custom"] = False
                else:
                    settings["online_output_root"] = str(stored_root or defaults["online_output_root"])
                    settings["online_output_custom"] = True
                settings["default_output_root"] = defaults["default_output_root"]
                return {
                    "ok": True,
                    "settings": settings,
                }
            except Exception as exc:
                return {"ok": False, "error": f"读取账号设置失败：{exc}"}

    def save_settings(
        self,
        username: str = "",
        password: str = "",
        remember: bool = False,
        preferences: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        path = _prepare_config_path()
        with _SETTINGS_LOCK:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                defaults = _settings_defaults()
                old: dict[str, Any] = {}
                if path.exists():
                    try:
                        old = json.loads(path.read_text(encoding="utf-8"))
                        if not isinstance(old, dict):
                            old = {}
                    except Exception:
                        try:
                            backup = path.with_suffix(".json.bak")
                            backup.write_bytes(path.read_bytes())
                        except Exception:
                            pass
                        old = {}

                prefs = preferences if isinstance(preferences, dict) else {}
                effective_remember = bool(remember)
                credential_action = str(prefs.get("credential_action") or "").lower()
                if bool(prefs.get("clear_credentials")):
                    credential_action = "clear"
                elif credential_action not in {"keep", "save", "clear"}:
                    # Compatibility with older callers that supplied credentials
                    # but did not yet send an explicit action.
                    credential_action = "save" if effective_remember and (username or password) else "keep"

                # Ordinary preference saves never erase credentials. Clearing is
                # a separate explicit action emitted only when the user turns the
                # remember switch off (or logs in with it disabled).
                payload: dict[str, Any] = {**defaults, **old}
                for key, value in prefs.items():
                    if key not in {"clear_credentials", "credential_action"}:
                        payload[key] = value

                if credential_action == "clear":
                    payload["remember"] = False
                    payload["username"] = ""
                    payload["password"] = ""
                elif credential_action == "save":
                    payload["remember"] = True
                    supplied_username = (username or "").strip()
                    if supplied_username:
                        payload["username"] = supplied_username
                    if password:
                        try:
                            payload["password"] = _protect_text(password)
                        except OSError as exc:
                            return {"ok": False, "error": f"密码加密失败：{exc}"}
                else:
                    payload["remember"] = bool(old.get("remember", defaults["remember"]))

                payload["settings_version"] = _SETTINGS_VERSION
                payload["active_mode"] = "online" if payload.get("active_mode") == "online" else "local"
                payload["theme"] = "light" if payload.get("theme") == "light" else "dark"
                payload["online_content_mode"] = "chapter" if payload.get("online_content_mode") == "chapter" else "homework"
                payload["local_folder"] = str(payload.get("local_folder") or "")
                payload["local_with_images"] = bool(payload.get("local_with_images", defaults["local_with_images"]))
                payload["local_course_info"] = bool(payload.get("local_course_info", defaults["local_course_info"]))
                payload["local_course_name"] = str(payload.get("local_course_name") or "")
                payload["local_class_name"] = str(payload.get("local_class_name") or "")
                payload["local_student_name"] = str(payload.get("local_student_name") or "")
                payload["online_with_images"] = bool(payload.get("online_with_images", defaults["online_with_images"]))
                payload["online_resume"] = bool(payload.get("online_resume", defaults["online_resume"]))
                payload["online_browser_choice"] = str(payload.get("online_browser_choice") or "auto")
                payload["online_browser_visible"] = bool(payload.get("online_browser_visible", defaults["online_browser_visible"]))

                output_custom = bool(payload.get("online_output_custom", False))
                output_root = payload.get("online_output_root")
                if not output_custom or _is_legacy_output_root(output_root):
                    payload["online_output_root"] = str(_default_output_root())
                    payload["online_output_custom"] = False
                else:
                    payload["online_output_root"] = str(output_root or _default_output_root())
                    payload["online_output_custom"] = True
                payload["default_output_root"] = str(_default_output_root())

                text = json.dumps(payload, ensure_ascii=False, indent=2)
                tmp = path.with_suffix(".json.tmp")
                last_exc: Exception | None = None
                for attempt in range(4):
                    try:
                        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
                            handle.write(text)
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.replace(tmp, path)
                        return {"ok": True, "saved_at": _now_ms()}
                    except OSError as exc:
                        last_exc = exc
                        time.sleep(0.08 * (attempt + 1))
                return {"ok": False, "error": f"保存账号设置失败：{last_exc}"}
            except Exception as exc:
                return {"ok": False, "error": f"保存账号设置失败：{exc}"}

    def save_state(self) -> dict[str, Any]:
        """Return save metadata so the UI can show a 'last saved' indicator."""
        path = _prepare_config_path()
        info: dict[str, Any] = {"ok": True, "exists": path.exists(), "path": str(path)}
        if path.exists():
            try:
                stat = path.stat()
                info["modified_at"] = int(stat.st_mtime * 1000)
                info["size"] = stat.st_size
            except OSError:
                pass
        return info

    def choose_folder(self) -> dict[str, Any]:
        folder = self._choose_directory("选择包含 .mhtml 文件的文件夹")
        if not folder:
            return {"ok": False, "cancelled": True}
        return self.scan_local_folder(str(folder))

    def scan_local_folder(self, folder: str) -> dict[str, Any]:
        path = Path(folder)
        if not path.exists() or not path.is_dir():
            return {"ok": False, "error": "请选择有效文件夹"}

        files = sorted(path.glob("*.mhtml"))
        return {
            "ok": True,
            "folder": str(path),
            "output_dir": str(path / "产出文件"),
            "count": len(files),
            "files": [
                {
                    "name": f.name,
                    "path": str(f),
                    "size_kb": max(1, f.stat().st_size // 1024),
                    "status": "pending",
                    "meta": "等待处理",
                }
                for f in files
            ],
        }

    def start_local_processing(self, folder: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        options = options or {}
        scan = self.scan_local_folder(folder)
        if not scan.get("ok"):
            return scan
        if scan["count"] == 0:
            return {"ok": False, "error": "该文件夹下没有找到 .mhtml 文件"}

        job_id = f"local-{_now_ms()}"
        job = {
            "id": job_id,
            "kind": "local",
            "state": "running",
            "message": "准备解析",
            "progress": 0,
            "folder": scan["folder"],
            "output_dir": scan["output_dir"],
            "files": scan["files"],
            "stats": {"work_count": 0, "question_count": 0, "correct_count": 0, "total_score": 0},
            "failures": [],
            "outputs": {},
        }
        with self.jobs_lock:
            self.jobs[job_id] = job

        thread = threading.Thread(
            target=self._run_local_job,
            args=(job_id, Path(scan["folder"]), options),
            daemon=True,
        )
        thread.start()
        return {"ok": True, "job_id": job_id}

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.jobs_lock:
            job = self.jobs.get(job_id)
            if not job:
                return {"ok": False, "error": "任务不存在或已过期"}
            return {"ok": True, "job": json.loads(json.dumps(job, ensure_ascii=False))}

    def _update_job(self, job_id: str, **changes: Any) -> None:
        with self.jobs_lock:
            job = self.jobs.get(job_id)
            if job:
                changes.setdefault("updated_at", _now_ms())
                job.update(changes)

    @staticmethod
    def _is_browser_disconnect_error(exc: BaseException) -> bool:
        name = exc.__class__.__name__.lower()
        message = str(exc).lower()
        return (
            "pagedisconnected" in name
            or "browserdisconnected" in name
            or "page disconnected" in message
            or "browser disconnected" in message
            or "target page, context or browser has been closed" in message
            or "connection is disconnected" in message
            or "与页面的连接已断开" in str(exc)
        )

    @staticmethod
    def _page_is_alive(page: Any) -> bool:
        if page is None:
            return False
        try:
            # Reading the URL can be cached by browser wrappers. A tiny CDP
            # request confirms that the controlled Chrome process still exists.
            getattr(page, "url")
            run_cdp = getattr(page, "run_cdp", None)
            if callable(run_cdp):
                result = run_cdp("Browser.getVersion")
                return result is not None
            return True
        except Exception:
            return False

    @staticmethod
    def _browser_requires_attention(page: Any) -> bool:
        try:
            url = str(getattr(page, "url", "") or "").lower()
        except Exception:
            return False
        return "passport2.chaoxing.com" in url or "/login" in url

    def _set_browser_window_visible_locked(self, visible: bool) -> bool:
        page = self.page
        if page is None:
            self.browser_window_hidden = False
            return False
        if self.browser_window_hidden == (not visible):
            return True
        try:
            from chaoxing_auto import set_browser_window_visible

            changed = set_browser_window_visible(page, visible)
            if changed:
                self.browser_window_hidden = not visible
            return changed
        except Exception:
            logger.exception("Could not change controlled browser visibility")
            return False

    def _show_browser_for_attention_locked(self) -> None:
        self.browser_verification_required = True
        self._set_browser_window_visible_locked(True)

    def _prepare_browser_for_background_locked(self, page: Any) -> None:
        if self._browser_requires_attention(page):
            self._show_browser_for_attention_locked()
            raise RuntimeError(_BROWSER_VERIFICATION_MESSAGE)
        self.browser_verification_required = False
        self._set_browser_window_visible_locked(self.browser_always_visible)

    def online_browser_choices(self) -> dict[str, Any]:
        try:
            from chaoxing_auto import detect_browser_choices

            choices = detect_browser_choices()
            return {
                "ok": True,
                "choices": choices,
                "active_choice": self.active_browser_choice or "auto",
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc), "choices": []}

    def online_set_browser_visibility(self, always_visible: bool = False) -> dict[str, Any]:
        self.browser_always_visible = bool(always_visible)
        if not self.page_lock.acquire(timeout=0.35):
            return {"ok": True, "busy": True, "message": "当前操作完成后生效"}
        try:
            if self.page is None or not self._page_is_alive(self.page):
                return {"ok": True, "alive": False}
            visible = self.browser_always_visible or self.browser_verification_required
            changed = self._set_browser_window_visible_locked(visible)
            return {
                "ok": True,
                "alive": True,
                "visible": visible,
                "changed": changed,
            }
        finally:
            self.page_lock.release()

    def _reset_browser_state(self) -> None:
        self.courses = []
        self.tasks_by_course.clear()
        self.active_browser_choice = ""
        self.browser_window_hidden = False
        self.browser_verification_required = False
        with self.content_lock:
            self.content_generation += 1
            active_job_id = self.active_content_job_id
            self.active_content_job_id = None
        if active_job_id:
            self._update_job(
                active_job_id,
                state="failed",
                message=_BROWSER_CLOSED_MESSAGE,
                browser_closed=True,
            )

    def _discard_page_locked(self, cleanup_processes: bool = True) -> Any:
        """Detach the current page while page_lock is held."""
        page = self.page
        self.page = None
        self._reset_browser_state()
        if cleanup_processes:
            try:
                from chaoxing_auto import cleanup_chrome_tmp_processes

                cleanup_chrome_tmp_processes()
            except Exception:
                logger.exception("Failed to clean the private Chrome profile")
        return page

    def _invalidate_browser(self, expected_page: Any = None) -> None:
        with self.page_lock:
            if self.page is None:
                self._reset_browser_state()
                return
            if expected_page is not None and self.page is not expected_page:
                return
            self._discard_page_locked(cleanup_processes=True)

    def online_browser_status(self) -> dict[str, Any]:
        """Report browser state without interrupting an active scrape."""
        if not self.page_lock.acquire(blocking=False):
            return {"ok": True, "alive": True, "busy": True}
        try:
            if self.page is None:
                return {"ok": True, "alive": False, "message": _BROWSER_CLOSED_MESSAGE}
            if self._page_is_alive(self.page):
                return {
                    "ok": True,
                    "alive": True,
                    "busy": False,
                    "hidden": self.browser_window_hidden,
                    "verification_required": self.browser_verification_required,
                    "active_choice": self.active_browser_choice or "auto",
                }
            self._discard_page_locked(cleanup_processes=True)
            return {"ok": True, "alive": False, "message": _BROWSER_CLOSED_MESSAGE}
        finally:
            self.page_lock.release()

    def _run_local_job(self, job_id: str, folder: Path, options: dict[str, Any]) -> None:
        from chaoxing_batch import parse_mhtml
        from chaoxing_scraper import format_work, save_docx

        files = sorted(folder.glob("*.mhtml"))
        output_dir = folder / "产出文件"
        output_dir.mkdir(exist_ok=True)
        results: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []

        try:
            for index, mhtml in enumerate(files, 1):
                self._set_file_state(job_id, index - 1, "processing", "解析中")
                self._update_job(
                    job_id,
                    message=f"正在处理 {index}/{len(files)}：{mhtml.name}",
                    progress=round((index - 1) / len(files) * 100, 1),
                )
                try:
                    work = parse_mhtml(str(mhtml))
                    if work:
                        q_count = len(work.get("questions", []))
                        results.append(work)
                        self._set_file_state(job_id, index - 1, "ok", f"{q_count}题")
                    else:
                        failures.append({"file": mhtml.name, "error": "未解析到题目"})
                        self._set_file_state(job_id, index - 1, "failed", "无题目")
                except Exception as exc:
                    logger.exception("Failed to parse local MHTML: %s", mhtml)
                    failures.append({"file": mhtml.name, "error": str(exc)})
                    self._set_file_state(job_id, index - 1, "failed", "解析失败")

                self._update_job(job_id, stats=_stats(results), failures=failures)

            if not results:
                self._update_job(
                    job_id,
                    state="failed",
                    message="没有解析到任何题目",
                    progress=100,
                    failures=failures,
                )
                return

            output_stem = _safe_filename(str(options.get("course_name") or folder.name), "课程")
            out_json = output_dir / f"{output_stem}.json"
            out_text = output_dir / f"{output_stem}.txt"
            out_docx = output_dir / f"{output_stem}.docx"

            out_json.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            out_text.write_text("\n\n".join(format_work(work) for work in results), encoding="utf-8")
            save_docx(
                results,
                str(out_docx),
                with_images=bool(options.get("with_images", True)),
                course_name=str(options.get("course_name") or ""),
                class_name=str(options.get("class_name") or ""),
                student_name=str(options.get("student_name") or ""),
            )

            self._update_job(
                job_id,
                state="done",
                message=f"完成：成功 {len(results)} 份，失败 {len(failures)} 份",
                progress=100,
                stats=_stats(results),
                failures=failures,
                outputs={
                    "json": str(out_json),
                    "text": str(out_text),
                    "docx": str(out_docx),
                    "folder": str(output_dir),
                },
            )
        except Exception as exc:
            logger.exception("Local processing job failed job_id=%s", job_id)
            self._update_job(
                job_id,
                state="failed",
                message=str(exc),
                traceback=traceback.format_exc(),
            )

    def _set_file_state(self, job_id: str, index: int, status: str, meta: str) -> None:
        with self.jobs_lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            if 0 <= index < len(job["files"]):
                job["files"][index]["status"] = status
                job["files"][index]["meta"] = meta

    def online_login(
        self,
        username: str,
        password: str,
        remember: bool = False,
        browser_choice: str = "auto",
        browser_always_visible: bool = False,
    ) -> dict[str, Any]:
        if not username or not password:
            return {"ok": False, "error": "请输入账号和密码"}
        browser_choice = str(browser_choice or "auto")
        browser_always_visible = bool(browser_always_visible)
        saved = self.save_settings(
            username,
            password,
            bool(remember),
            {
                "active_mode": "online",
                "online_browser_choice": browser_choice,
                "online_browser_visible": browser_always_visible,
                "credential_action": "save" if remember else "clear",
            },
        )
        if not saved.get("ok"):
            return saved
        job_id = f"login-{_now_ms()}"
        with self.jobs_lock:
            self.jobs[job_id] = {
                "id": job_id,
                "kind": "login",
                "state": "running",
                "message": "准备登录...",
                "progress": 0,
            }
        thread = threading.Thread(
            target=self._run_login_job,
            args=(job_id, username, password, browser_choice, browser_always_visible),
            daemon=True,
        )
        thread.start()
        return {"ok": True, "job_id": job_id, "status": "pending"}

    def _run_login_job(
        self,
        job_id: str,
        username: str,
        password: str,
        browser_choice: str = "auto",
        browser_always_visible: bool = False,
    ) -> None:
        page = None
        try:
            from chaoxing_auto import login, make_page
            from chaoxing_scraper import discover_courses

            with self.page_lock:
                if self.page is not None and not self._page_is_alive(self.page):
                    logger.info("Controlled browser was closed; recreating it for login")
                    self._discard_page_locked(cleanup_processes=True)
                if (
                    self.page is not None
                    and self.active_browser_choice
                    and self.active_browser_choice != browser_choice
                ):
                    logger.info("Browser choice changed from %s to %s", self.active_browser_choice, browser_choice)
                    self._discard_page_locked(cleanup_processes=True)
                if self.page is None:
                    self._update_job(job_id, message="正在启动浏览器...")
                    self.page = make_page(show_browser=True, browser_choice=browser_choice)
                    self._reset_browser_state()
                    self.active_browser_choice = browser_choice
                page = self.page
                self.browser_always_visible = browser_always_visible
                self.browser_verification_required = True
                self._set_browser_window_visible_locked(True)
                self._update_job(job_id, message="正在登录超星...")
                login_result = login(page, username, password, wait_for_manual=False)
                if login_result.get("verification_required"):
                    self._show_browser_for_attention_locked()
                    self._update_job(
                        job_id,
                        state="verification_required",
                        message="需要验证码，请在浏览器完成后点击「我已完成」",
                    )
                    return
                if not login_result.get("ok"):
                    raise RuntimeError(str(login_result.get("error") or "登录失败"))

                self._update_job(job_id, message="正在读取课程列表...")
                self.courses = discover_courses(page)
                self.browser_verification_required = False
                self._set_browser_window_visible_locked(self.browser_always_visible)
                self._update_job(
                    job_id,
                    state="done",
                    message=f"已登录，找到 {len(self.courses)} 门课程",
                    courses=self.courses,
                    progress=100,
                )
        except Exception as exc:
            logger.exception("Online login job failed job_id=%s", job_id)
            browser_closed = self._is_browser_disconnect_error(exc) or (page is not None and not self._page_is_alive(page))
            if browser_closed:
                self._invalidate_browser(page)
            self._update_job(
                job_id,
                state="failed",
                message=_BROWSER_CLOSED_MESSAGE if browser_closed else str(exc),
                browser_closed=browser_closed,
                traceback=traceback.format_exc(),
            )

    def online_continue_after_verification(self) -> dict[str, Any]:
        status = self.online_browser_status()
        if not status.get("alive"):
            return {"ok": False, "error": _BROWSER_CLOSED_MESSAGE, "browser_closed": True}
        job_id = f"login-{_now_ms()}"
        with self.jobs_lock:
            self.jobs[job_id] = {
                "id": job_id,
                "kind": "login",
                "state": "running",
                "message": "正在确认登录状态...",
                "progress": 0,
            }
        thread = threading.Thread(target=self._run_continue_login_job, args=(job_id,), daemon=True)
        thread.start()
        return {"ok": True, "job_id": job_id, "status": "pending"}

    def _run_continue_login_job(self, job_id: str) -> None:
        page = None
        try:
            from chaoxing_scraper import discover_courses

            with self.page_lock:
                page = self.page
                if not self._page_is_alive(page):
                    raise RuntimeError(_BROWSER_CLOSED_MESSAGE)
                self._show_browser_for_attention_locked()
                if "passport" in getattr(page, "url", ""):
                    self._update_job(
                        job_id,
                        state="verification_required",
                        message="仍停留在登录页，请先完成验证码",
                    )
                    return
                self._update_job(job_id, message="正在读取课程列表...")
                self.courses = discover_courses(page)
                self.browser_verification_required = False
                self._set_browser_window_visible_locked(self.browser_always_visible)
                self._update_job(
                    job_id,
                    state="done",
                    message=f"已登录，找到 {len(self.courses)} 门课程",
                    courses=self.courses,
                    progress=100,
                )
        except Exception as exc:
            logger.exception("Login verification continuation failed job_id=%s", job_id)
            browser_closed = self._is_browser_disconnect_error(exc) or str(exc) == _BROWSER_CLOSED_MESSAGE
            if browser_closed:
                self._invalidate_browser(page)
            self._update_job(
                job_id,
                state="failed",
                message=_BROWSER_CLOSED_MESSAGE if browser_closed else str(exc),
                browser_closed=browser_closed,
                traceback=traceback.format_exc(),
            )

    def online_load_tasks(self, course: dict[str, Any]) -> dict[str, Any]:
        return self.online_load_content(course, "homework")

    def online_load_content(self, course: dict[str, Any], content_mode: str = "homework") -> dict[str, Any]:
        browser_status = self.online_browser_status()
        if not browser_status.get("alive"):
            return {"ok": False, "error": _BROWSER_CLOSED_MESSAGE, "browser_closed": True}
        content_mode = "chapter" if content_mode == "chapter" else "homework"
        content_name = "章节练习" if content_mode == "chapter" else "作业"
        job_id = f"content-{_now_ms()}"
        course_key = self._course_key(course)
        with self.content_lock:
            active_job_id = self.active_content_job_id
            if active_job_id:
                with self.jobs_lock:
                    active_job = self.jobs.get(active_job_id) or {}
                    active_state = active_job.get("state")
                    active_course = (active_job.get("course") or {}).get("name") or "另一门课程"
                if active_state == "running":
                    return {
                        "ok": False,
                        "busy": True,
                        "error": f"正在读取{active_course}，请等待当前读取完成后再选择课程",
                        "job_id": active_job_id,
                    }
                self.active_content_job_id = None
            self.content_generation += 1
            generation = self.content_generation
            self.active_content_job_id = job_id
        with self.jobs_lock:
            self.jobs[job_id] = {
                "id": job_id,
                "kind": "content",
                "state": "running",
                "message": f"正在读取{content_name}列表：{course.get('name') or '未命名课程'}",
                "progress": 0,
                "course": course,
                "course_key": course_key,
                "content_mode": content_mode,
                "tasks": [],
                "generation": generation,
                "updated_at": _now_ms(),
            }
        thread = threading.Thread(
            target=self._run_load_content_job,
            args=(job_id, course, content_mode, generation),
            daemon=True,
            name=f"content-list-{generation}",
        )
        thread.start()
        return {"ok": True, "job_id": job_id}

    def _course_key(self, course: dict[str, Any]) -> str:
        return "|".join([
            str(course.get("course_id") or course.get("courseId") or ""),
            str(course.get("clazz_id") or course.get("class_id") or course.get("classId") or ""),
            str(course.get("cpi") or ""),
        ])

    def _run_load_content_job(
        self,
        job_id: str,
        course: dict[str, Any],
        content_mode: str,
        generation: int,
    ) -> None:
        page = None
        try:
            from chaoxing_auto import get_chapter_exercise_list, get_task_list

            content_name = "章节练习" if content_mode == "chapter" else "作业"

            def on_progress(done: int, total: int, message: str) -> None:
                with self.content_lock:
                    if generation != self.content_generation:
                        return
                progress = round(done / max(1, total) * 100, 1)
                self._update_job(job_id, message=message, progress=progress)

            with self.page_lock:
                with self.content_lock:
                    if generation != self.content_generation or self.active_content_job_id != job_id:
                        self._update_job(job_id, state="failed", message="已取消旧的课程读取任务", cancelled=True)
                        return
                page = self.page
                if not self._page_is_alive(page):
                    raise RuntimeError(_BROWSER_CLOSED_MESSAGE)
                self._prepare_browser_for_background_locked(page)
                if content_mode == "chapter":
                    tasks = get_chapter_exercise_list(page, course, progress_callback=on_progress)
                else:
                    tasks = get_task_list(page, course, progress_callback=on_progress)
                self._prepare_browser_for_background_locked(page)
            with self.content_lock:
                if generation != self.content_generation or self.active_content_job_id != job_id:
                    self._update_job(job_id, state="failed", message="已取消旧的课程读取任务", cancelled=True)
                    return
            key = self._course_key(course)
            self.tasks_by_course[f"{content_mode}|{key}"] = tasks
            self._update_job(
                job_id,
                state="done",
                message=f"已读取 {len(tasks)} 份{content_name}",
                tasks=tasks,
                course_key=key,
                content_mode=content_mode,
                progress=100,
            )
        except Exception as exc:
            logger.exception("Content list job failed job_id=%s mode=%s", job_id, content_mode)
            verification_required = str(exc) == _BROWSER_VERIFICATION_MESSAGE
            browser_closed = self._is_browser_disconnect_error(exc) or str(exc) == _BROWSER_CLOSED_MESSAGE
            if browser_closed:
                self._invalidate_browser(page)
            self._update_job(
                job_id,
                state="verification_required" if verification_required else "failed",
                message=_BROWSER_CLOSED_MESSAGE if browser_closed else str(exc),
                browser_closed=browser_closed,
                browser_attention=verification_required,
                traceback=traceback.format_exc(),
            )
        finally:
            with self.content_lock:
                if self.active_content_job_id == job_id:
                    self.active_content_job_id = None

    def choose_output_root(self) -> dict[str, Any]:
        folder = self._choose_directory("选择在线抓取结果保存位置")
        if not folder:
            return {"ok": False, "cancelled": True}
        return {"ok": True, "folder": str(folder)}

    def start_online_capture(
        self,
        courses: list[dict[str, Any]],
        output_root: str,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        browser_status = self.online_browser_status()
        if not browser_status.get("alive"):
            return {"ok": False, "error": _BROWSER_CLOSED_MESSAGE, "browser_closed": True}
        if not courses:
            return {"ok": False, "error": "请至少选择一门课程"}
        root = Path(output_root) if output_root else _default_output_root()
        root.mkdir(parents=True, exist_ok=True)

        job_id = f"online-{_now_ms()}"
        job = {
            "id": job_id,
            "kind": "online",
            "state": "running",
            "message": "准备抓取",
            "progress": 0,
            "output_dir": str(root),
            "files": [],
            "stats": {"work_count": 0, "question_count": 0, "correct_count": 0, "total_score": 0},
            "failures": [],
            "outputs": {},
        }
        with self.jobs_lock:
            self.jobs[job_id] = job

        thread = threading.Thread(
            target=self._run_online_job,
            args=(job_id, courses, root, options or {}),
            daemon=True,
        )
        thread.start()
        return {"ok": True, "job_id": job_id}

    def _run_online_job(
        self,
        job_id: str,
        courses: list[dict[str, Any]],
        output_root: Path,
        options: dict[str, Any],
    ) -> None:
        page = None
        try:
            from chaoxing_auto import get_chapter_exercise_list, get_task_list, save_as_mhtml
            from chaoxing_batch import parse_mhtml
            from chaoxing_scraper import format_work, save_docx

            all_results: list[dict[str, Any]] = []
            failures: list[dict[str, str]] = []
            total_steps = max(1, len(courses))
            completed_steps = 0
            content_mode = "chapter" if options.get("content_mode") == "chapter" else "homework"
            content_name = "章节练习" if content_mode == "chapter" else "作业"

            for course in courses:
                course_name = str(course.get("name") or "未命名课程")
                course_dir = output_root / _safe_filename(course_name, "课程")
                if content_mode == "chapter":
                    course_dir = course_dir / "章节练习"
                mhtml_dir = course_dir / "mhtml_files"
                output_dir = course_dir / "产出文件"
                mhtml_dir.mkdir(parents=True, exist_ok=True)
                output_dir.mkdir(parents=True, exist_ok=True)

                self._update_job(job_id, message=f"读取{content_name}列表：{course_name}")

                def on_chapter_progress(done: int, total: int, message: str) -> None:
                    course_progress = done / max(1, total)
                    self._update_job(
                        job_id,
                        message=f"{course_name}：{message}",
                        progress=round((completed_steps + course_progress * 0.35) / total_steps * 100, 1),
                    )

                with self.page_lock:
                    page = self.page
                    if not self._page_is_alive(page):
                        raise RuntimeError(_BROWSER_CLOSED_MESSAGE)
                    self._prepare_browser_for_background_locked(page)
                    if content_mode == "chapter":
                        tasks = get_chapter_exercise_list(page, course, progress_callback=on_chapter_progress)
                    else:
                        tasks = get_task_list(page, course)
                    self._prepare_browser_for_background_locked(page)
                if not tasks:
                    failures.append({"file": course_name, "error": f"未找到{content_name}"})
                    completed_steps += 1
                    self._update_job(job_id, failures=failures, progress=round(completed_steps / total_steps * 100, 1))
                    continue

                course_results: list[dict[str, Any]] = []
                course_files: list[tuple[Path, str, str]] = []
                seen_result_titles: set[str] = set()
                seen_result_content: set[str] = set()
                for index, task in enumerate(tasks, 1):
                    title = str(task.get("title") or f"{content_name}{index:02d}")
                    self._update_job(
                        job_id,
                        message=f"{course_name}：抓取{content_name} {index}/{len(tasks)} {title}",
                        progress=round((completed_steps + (index - 1) / len(tasks)) / total_steps * 100, 1),
                    )
                    if content_mode == "homework" and "answerId" not in str(task.get("url", "")):
                        failures.append({"file": title, "error": "未作答或无答案详情，已跳过"})
                        continue

                    save_path = mhtml_dir / f"{index:02d}_{_safe_filename(title, content_name)}.mhtml"
                    if save_path.exists() and options.get("resume", True):
                        try:
                            if parse_mhtml(str(save_path)):
                                course_files.append((save_path, title, str(task.get("status") or "")))
                                continue
                        except Exception:
                            pass
                        save_path.unlink(missing_ok=True)

                    if not save_path.exists() or not options.get("resume", True):
                        with self.page_lock:
                            page = self.page
                            if not self._page_is_alive(page):
                                raise RuntimeError(_BROWSER_CLOSED_MESSAGE)
                            self._prepare_browser_for_background_locked(page)
                            ok = save_as_mhtml(page, str(task["url"]), str(save_path))
                            self._prepare_browser_for_background_locked(page)
                        if not ok:
                            failures.append({"file": title, "error": "保存 mhtml 失败"})
                            continue
                    course_files.append((save_path, title, str(task.get("status") or "")))

                for mhtml, task_title, task_status in sorted(course_files, key=lambda item: item[0]):
                    try:
                        work = parse_mhtml(str(mhtml))
                        if work:
                            # Detail pages often use generic titles such as
                            # "查看已批阅作业". The course list/tree title is
                            # the name the student actually sees on the website.
                            work["title"] = task_title
                            if task_status:
                                work["status"] = task_status
                            if content_mode == "chapter":
                                title_key = _normalized_work_title(task_title)
                                content_key = _work_content_fingerprint(work)
                                if title_key in seen_result_titles or (
                                    content_key and content_key in seen_result_content
                                ):
                                    logger.info("Skipped duplicate chapter result: %s", task_title)
                                    continue
                                seen_result_titles.add(title_key)
                                if content_key:
                                    seen_result_content.add(content_key)
                            course_results.append(work)
                    except Exception as exc:
                        logger.exception("Failed to parse captured MHTML: %s", mhtml)
                        failures.append({"file": mhtml.name, "error": str(exc)})

                if course_results:
                    output_stem = _safe_filename(course_name, "课程")
                    out_json = output_dir / f"{output_stem}.json"
                    out_text = output_dir / f"{output_stem}.txt"
                    out_docx = output_dir / f"{output_stem}.docx"
                    out_json.write_text(json.dumps(course_results, ensure_ascii=False, indent=2), encoding="utf-8")
                    out_text.write_text("\n\n".join(format_work(work) for work in course_results), encoding="utf-8")
                    save_docx(
                        course_results,
                        str(out_docx),
                        with_images=bool(options.get("with_images", True)),
                        course_name=course_name,
                        class_name=str(course.get("class_name") or course.get("clazz_name") or ""),
                        student_name=str(options.get("student_name") or ""),
                    )
                    all_results.extend(course_results)

                completed_steps += 1
                self._update_job(
                    job_id,
                    progress=round(completed_steps / total_steps * 100, 1),
                    stats=_stats(all_results),
                    failures=failures,
                )

            self._update_job(
                job_id,
                state="done",
                message=f"在线抓取完成：成功 {len(all_results)} 份，失败 {len(failures)} 项",
                progress=100,
                stats=_stats(all_results),
                failures=failures,
                outputs={"folder": str(output_root)},
            )
        except Exception as exc:
            logger.exception("Online capture job failed job_id=%s", job_id)
            verification_required = str(exc) == _BROWSER_VERIFICATION_MESSAGE
            browser_closed = self._is_browser_disconnect_error(exc) or str(exc) == _BROWSER_CLOSED_MESSAGE
            if browser_closed:
                self._invalidate_browser(page)
            self._update_job(
                job_id,
                state="verification_required" if verification_required else "failed",
                message=_BROWSER_CLOSED_MESSAGE if browser_closed else str(exc),
                browser_closed=browser_closed,
                browser_attention=verification_required,
                traceback=traceback.format_exc(),
            )

    def close_browser(self) -> dict[str, Any]:
        """Tear down the browser without blocking the caller.

        DrissionPage's ``page.quit()`` can hang for many seconds when the
        browser is busy, which previously made the window look frozen on
        close. We release the page slot synchronously and run the actual
        quit + cleanup in daemon threads.
        """
        page = None
        if self.page_lock.acquire(timeout=1):
            try:
                if self.page is not None:
                    page = self.page
                    self.page = None
                    self._reset_browser_state()
            finally:
                self.page_lock.release()
        else:
            if self.page is not None:
                page = self.page
                self.page = None
                self._reset_browser_state()

        if page is not None:
            def _do_quit() -> None:
                try:
                    page.quit()
                except Exception:
                    pass
            threading.Thread(target=_do_quit, daemon=True, name="browser-quit").start()

        def _do_cleanup() -> None:
            try:
                from chaoxing_auto import cleanup_chrome_tmp_processes
                cleanup_chrome_tmp_processes()
            except Exception:
                pass
        threading.Thread(target=_do_cleanup, daemon=True, name="chrome-cleanup").start()
        return {"ok": True}

    def shutdown(self) -> dict[str, Any]:
        self.close_browser()
        return {"ok": True}
