#!/usr/bin/env python3
"""
超星学习通在线抓取后端（DrissionPage 版）

这里保留命令行可运行能力，同时给 pywebview 桌面界面复用。
账号、密码、课程 ID 均通过函数参数或运行时输入传入，不再硬编码。
"""

from __future__ import annotations

import getpass
import ctypes
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from ctypes import wintypes
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup
from DrissionPage import ChromiumOptions, ChromiumPage

from chaoxing_batch import parse_mhtml
from chaoxing_scraper import discover_courses, format_work, save_docx, task_url_to_detail_url


MHTML_DIR = "mhtml_files"
OUT_JSON = "output.json"
OUT_TEXT = "output_readable.txt"
OUT_DOCX = "output.docx"
SLEEP_SEC = 2.0


def default_output_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def chrome_tmp_root() -> Path:
    return default_output_root() / "chrome"


def _browser_choice_id(kind: str, path: Path) -> str:
    resolved = path.resolve(strict=False)
    if kind == "portable":
        try:
            relative = resolved.relative_to(default_output_root().resolve(strict=False))
            return f"portable:{relative.as_posix().lower()}"
        except ValueError:
            pass
    digest = hashlib.sha1(str(resolved).lower().encode("utf-8")).hexdigest()[:12]
    return f"{kind}:{digest}"


def _registry_browser_paths(executable_name: str) -> list[Path]:
    if not sys.platform.startswith("win"):
        return []
    try:
        import winreg
    except ImportError:
        return []

    paths: list[Path] = []
    key_path = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{executable_name}"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for access in (winreg.KEY_READ, winreg.KEY_READ | getattr(winreg, "KEY_WOW64_32KEY", 0)):
            try:
                with winreg.OpenKey(hive, key_path, 0, access) as key:
                    value, _kind = winreg.QueryValueEx(key, None)
                    if value:
                        paths.append(Path(str(value).strip('"')))
            except OSError:
                continue
    return paths


def detect_browser_choices() -> list[dict[str, str]]:
    """Return supported browser executables in deterministic preference order."""
    app_root = default_output_root()
    portable_candidates = [
        ("便携 Chromium", app_root / "browser" / "chrome.exe"),
        ("便携 Chromium", app_root / "browser" / "chrome-win64" / "chrome.exe"),
        ("便携 Chromium", app_root / "browser" / "chromium.exe"),
        ("便携 Microsoft Edge", app_root / "browser" / "msedge.exe"),
        ("便携 Microsoft Edge", app_root / "browser" / "edge" / "msedge.exe"),
    ]
    chrome_candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        *_registry_browser_paths("chrome.exe"),
    ]
    edge_candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        *_registry_browser_paths("msedge.exe"),
    ]

    choices: list[dict[str, str]] = []
    seen: set[str] = set()
    groups = [
        ("portable", portable_candidates),
        ("chrome", [("Google Chrome", path) for path in chrome_candidates]),
        ("edge", [("Microsoft Edge", path) for path in edge_candidates]),
    ]
    for kind, candidates in groups:
        for label, path in candidates:
            try:
                if not path.is_file():
                    continue
                resolved = path.resolve(strict=False)
            except OSError:
                continue
            key = str(resolved).lower()
            if key in seen:
                continue
            seen.add(key)
            choices.append({
                "id": _browser_choice_id(kind, resolved),
                "name": label,
                "path": str(resolved),
                "kind": kind,
            })
    return choices


def resolve_browser_choice(choice_id: str = "auto") -> dict[str, str]:
    choices = detect_browser_choices()
    requested = str(choice_id or "auto")
    if requested != "auto":
        for choice in choices:
            if choice["id"] == requested:
                return choice
    if choices:
        return choices[0]
    raise RuntimeError("未检测到可用的 Chrome、Edge 或便携 Chromium 浏览器")


def set_browser_window_visible(page: ChromiumPage, visible: bool) -> bool:
    """Show or hide the headed browser without breaking its CDP session."""
    try:
        window = page.set.window
        if visible:
            window.show()
        else:
            window.hide()
        return True
    except Exception:
        pass

    if not sys.platform.startswith("win"):
        return False
    try:
        process_id = int(getattr(page, "process_id", 0) or getattr(page.browser, "process_id", 0) or 0)
        if not process_id:
            return False
        user32 = ctypes.windll.user32
        handles: list[int] = []
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def collect(hwnd: int, _lparam: int) -> bool:
            window_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
            if int(window_pid.value) == process_id:
                handles.append(int(hwnd))
            return True

        user32.EnumWindows(collect, 0)
        command = 5 if visible else 0  # SW_SHOW / SW_HIDE
        for hwnd in handles:
            user32.ShowWindow(wintypes.HWND(hwnd), command)
        return bool(handles)
    except Exception:
        return False


def cleanup_chrome_tmp_processes(timeout: float = 2.0) -> int:
    """Kill only Chrome/Chromium processes using this app's temp profile root."""
    root = str(chrome_tmp_root()).lower()
    killed = 0
    try:
        import psutil
    except Exception:
        return 0

    targets = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if not any(part in name for part in ("chrome", "chromium", "msedge")):
                continue
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
            if root not in cmdline:
                continue
            proc.terminate()
            targets.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    try:
        gone, alive = psutil.wait_procs(targets, timeout=timeout)
    except (psutil.Error, OSError):
        gone = []
        alive = []
        for proc in targets:
            try:
                proc.wait(timeout=0)
                gone.append(proc)
            except psutil.TimeoutExpired:
                alive.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
    killed += len(gone)
    for proc in alive:
        try:
            proc.kill()
            killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            pass
    return killed


def make_page(show_browser: bool = True, browser_choice: str = "auto") -> ChromiumPage:
    cleanup_chrome_tmp_processes()
    tmp_root = chrome_tmp_root()
    tmp_root.mkdir(parents=True, exist_ok=True)
    selected_browser = resolve_browser_choice(browser_choice)
    co = ChromiumOptions()
    co.set_browser_path(selected_browser["path"])
    co.set_tmp_path(tmp_root)
    co.auto_port(True)
    co.set_argument("--no-first-run")
    co.set_argument("--no-default-browser-check")
    if not show_browser:
        co.headless()
    return ChromiumPage(co)


def login(
    page: ChromiumPage,
    username: str,
    password: str,
    wait_for_manual: bool = True,
) -> dict:
    print("[1/4] 自动登录...")
    page.get("https://passport2.chaoxing.com/login?refer=https://i.chaoxing.com")

    try:
        tab_btn = page.ele("text:账号登录", timeout=5)
        if tab_btn:
            tab_btn.click()
            time.sleep(0.5)
    except Exception:
        pass

    phone_input = page.ele("css:input#phone,input[name='phone'],input[placeholder*='账号'],input[placeholder*='手机']")
    password_input = page.ele("css:input[type='password']")
    try:
        phone_input.clear()
    except Exception:
        pass
    try:
        password_input.clear()
    except Exception:
        pass
    phone_input.input(username)
    password_input.input(password)
    page.ele("css:button[type='submit'],.loginBtn,#loginBtn").click()

    page.wait.url_change("passport", timeout=20, raise_err=False)
    if "passport" in page.url:
        if not wait_for_manual:
            return {"ok": True, "verification_required": True, "url": page.url}
        print("    需要验证码，请在 Chrome 窗口手动完成后按回车...")
        input()

    if "passport" in page.url:
        return {"ok": False, "verification_required": True, "url": page.url}

    print(f"    [OK] 登录成功：{page.url}")
    return {"ok": True, "verification_required": False, "url": page.url}


def _extract_tasks(html: str, base_url: str = "") -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    tasks = []
    for li in soup.find_all("li", attrs={"data": True}):
        url = li.get("data", "").strip()
        if "work/task" not in url:
            continue
        url = urljoin(base_url, url)
        title_p = li.find("p", class_="overHidden2")
        status_p = li.find("p", class_="status")
        tasks.append({
            "title": title_p.get_text(strip=True) if title_p else "",
            "status": status_p.get_text(strip=True) if status_p else "",
            "url": url,
        })
    return tasks


def _task_sources(page: ChromiumPage) -> list[tuple[object, str, str]]:
    sources: list[tuple[object, str, str]] = []
    try:
        sources.append((page, page.html, page.url))
    except Exception:
        pass

    try:
        frames = page.get_frames()
    except Exception:
        frames = []

    for frame in frames:
        try:
            sources.append((frame, frame.html, getattr(frame, "url", "") or page.url))
        except Exception:
            continue
    return sources


def _current_task_page(page: ChromiumPage) -> tuple[list[dict], object | None]:
    best_tasks: list[dict] = []
    best_source: object | None = None
    for source, html, base_url in _task_sources(page):
        tasks = _extract_tasks(html, base_url)
        if len(tasks) > len(best_tasks):
            best_tasks = tasks
            best_source = source
    return best_tasks, best_source


def _current_task_page_legacy(page: ChromiumPage) -> tuple[list[dict], object | None]:
    # Keep the compatibility name for callers, but always choose the source
    # containing the largest task list. The top document can retain a stale
    # copy of page 1 while the live list changes inside an iframe.
    return _current_task_page(page)


def _task_signature(tasks: list[dict]) -> tuple[str, ...]:
    return tuple(task.get("url", "") for task in tasks)


def _click_next_task_page(source: object) -> bool:
    for selector in ("css:#page .xl-nextPage", "css:.pageDiv .xl-nextPage", "css:.xl-nextPage"):
        try:
            el = source.ele(selector, timeout=1)
            if not el:
                continue
            cls = el.attr("class") or ""
            if "disabled" in cls:
                continue
            el.click()
            return True
        except Exception:
            continue

    try:
        lis = source.eles("css:#page li")
    except Exception:
        lis = []
    active_index = None
    for index, li in enumerate(lis):
        try:
            if "xl-active" in (li.attr("class") or ""):
                active_index = index
                break
        except Exception:
            continue
    if active_index is not None:
        for li in lis[active_index + 1:]:
            try:
                cls = li.attr("class") or ""
                text = (li.text or "").strip()
                if "disabled" not in cls and text.isdigit():
                    li.click()
                    return True
            except Exception:
                continue

    script = r"""
(() => {
  const els = Array.from(document.querySelectorAll('a,button,li,span'));
  const isVisible = el => {
    const style = getComputedStyle(el);
    return style.display !== 'none' && style.visibility !== 'hidden' && el.getClientRects().length > 0;
  };
  const textOf = el => (el.textContent || '').trim();
  const nameOf = el => [
    el.className || '',
    el.parentElement?.className || '',
    el.getAttribute('title') || '',
    el.getAttribute('aria-label') || '',
    el.getAttribute('rel') || ''
  ].join(' ').toLowerCase();
  const disabled = el => {
    const name = nameOf(el);
    return el.disabled || el.getAttribute('disabled') !== null ||
      el.getAttribute('aria-disabled') === 'true' ||
      /disabled|disable|prev|previous|上.?一|左/.test(name);
  };
  const active = el => {
    const name = nameOf(el);
    return /(^|[\s_-])(active|current|curr|selected|on)([\s_-]|$)/.test(name) ||
      el.getAttribute('aria-current') === 'page';
  };

  const pages = els
    .filter(el => isVisible(el) && /^\d+$/.test(textOf(el)))
    .map(el => ({ el, num: Number(textOf(el)) }))
    .sort((a, b) => a.num - b.num);
  const current = pages.find(item => active(item.el));
  if (current) {
    const nextNumber = pages.find(item => item.num === current.num + 1 && !disabled(item.el));
    if (nextNumber) {
      nextNumber.el.scrollIntoView({ block: 'center', inline: 'center' });
      nextNumber.el.click();
      return true;
    }
  }

  const nextEls = els.filter(el => {
    if (!isVisible(el) || disabled(el)) return false;
    const text = textOf(el);
    const name = nameOf(el);
    return /next|btn-next|page-next|layui-laypage-next|ant-pagination-next|ivu-page-next|下一/.test(name) ||
      /^(>|›|»)$/.test(text);
  });
  for (const el of nextEls) {
    el.scrollIntoView({ block: 'center', inline: 'center' });
    el.click();
    return true;
  }
  return false;
})()
"""
    try:
        return bool(source.run_js(script, timeout=3))
    except Exception:
        return False


def _collect_tasks_with_pagination(
    page: ChromiumPage,
    first_tasks: list[dict],
    first_source: object | None,
    max_pages: int = 50,
) -> list[dict]:
    collected: list[dict] = []
    seen: set[str] = set()
    tasks = first_tasks
    source = first_source

    for page_index in range(1, max_pages + 1):
        for task in tasks:
            key = task.get("url") or task.get("title") or json.dumps(task, ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            collected.append(task)

        if not source:
            break

        before = _task_signature(tasks)
        if not _click_next_task_page(source):
            break

        changed = False
        next_tasks: list[dict] = []
        next_source: object | None = None
        deadline = time.time() + 6
        while time.time() < deadline:
            time.sleep(0.5)
            next_tasks, next_source = _current_task_page(page)
            after = _task_signature(next_tasks)
            if after and after != before:
                changed = True
                break
        if not changed:
            break
        tasks, source = next_tasks, next_source

    return collected


def _find_work_href(page: ChromiumPage) -> str:
    try:
        link = page.ele("css:a[href*='work/list']", timeout=5)
        if link:
            return link.attr("href") or ""
    except Exception:
        pass

    try:
        frames = page.get_frames()
    except Exception:
        frames = []

    for frame in frames:
        try:
            link = frame.ele("css:a[href*='work/list']", timeout=1)
            if link:
                return link.attr("href") or ""
        except Exception:
            continue
    return ""


def _click_work_entry(page: ChromiumPage) -> bool:
    candidates = ["作业", "章节作业"]
    for text in candidates:
        try:
            btn = page.ele(f"text:{text}", timeout=2)
            if btn:
                btn.click()
                time.sleep(2)
                if len(page.tab_ids) > 1:
                    page.to_tab(page.tab_ids[-1])
                    time.sleep(1.5)
                return True
        except Exception:
            pass

    try:
        frames = page.get_frames()
    except Exception:
        frames = []
    for frame in frames:
        for text in candidates:
            try:
                btn = frame.ele(f"text:{text}", timeout=1)
                if btn:
                    btn.click()
                    time.sleep(2)
                    if len(page.tab_ids) > 1:
                        page.to_tab(page.tab_ids[-1])
                        time.sleep(1.5)
                    return True
            except Exception:
                continue
    return False


def get_task_list(page: ChromiumPage, course: dict, progress_callback=None) -> list[dict]:
    course_id = str(course.get("course_id") or course.get("courseId") or "")
    clazz_id = str(course.get("clazz_id") or course.get("class_id") or course.get("classId") or "")
    cpi = str(course.get("cpi") or "")
    middle_url = str(course.get("middle_url") or "")
    if not middle_url and course_id:
        middle_url = (
            "https://mooc1.chaoxing.com/visit/stucoursemiddle"
            f"?courseid={course_id}&clazzid={clazz_id}&cpi={cpi}&ismooc2=1&v=2"
        )
    if not middle_url:
        raise ValueError("课程信息缺少 middle_url/course_id")

    if progress_callback:
        progress_callback(0, 4, "正在打开课程主页...")
    print("[2/4] 进入课程...")
    page.get(middle_url, timeout=20)
    page.wait.url_change("stucoursemiddle", timeout=15, raise_err=False)
    time.sleep(2)
    print(f"    课程主页：{page.url}")

    if progress_callback:
        progress_callback(1, 4, "正在查找作业入口...")
    print("[3/4] 查找作业入口...")
    work_href = _find_work_href(page)
    if work_href:
        page.get(urljoin(page.url, work_href), timeout=20)
    elif _click_work_entry(page):
        pass
    elif course_id:
        page.get(
            "https://mooc1.chaoxing.com/mooc-ans/mooc2/work/list"
            f"?courseId={course_id}&classId={clazz_id}&cpi={cpi}",
            timeout=20,
        )
    else:
        raise RuntimeError("未找到作业入口")

    if progress_callback:
        progress_callback(2, 4, "正在等待作业列表加载...")
    time.sleep(2)

    first_tasks: list[dict] = []
    first_source: object | None = None
    deadline = time.time() + 6
    while time.time() < deadline:
        first_tasks, first_source = _current_task_page(page)
        if first_tasks:
            break
        time.sleep(0.5)

    if progress_callback:
        progress_callback(3, 4, "正在读取全部分页作业...")
    tasks = _collect_tasks_with_pagination(page, first_tasks, first_source) if first_tasks else []

    print(f"    [OK] 找到 {len(tasks)} 份作业")
    if progress_callback:
        progress_callback(4, 4, f"已读取 {len(tasks)} 份作业")
    return tasks


def _course_identifiers(course: dict) -> tuple[str, str, str]:
    return (
        str(course.get("course_id") or course.get("courseId") or ""),
        str(course.get("clazz_id") or course.get("class_id") or course.get("classId") or ""),
        str(course.get("cpi") or ""),
    )


def _chapter_tree_url(course: dict) -> str:
    course_id, clazz_id, cpi = _course_identifiers(course)
    if not course_id:
        raise ValueError("课程信息缺少 course_id")
    return (
        "https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse?"
        + urlencode({"courseid": course_id, "clazzid": clazz_id, "cpi": cpi, "ut": "s", "t": int(time.time() * 1000)})
    )


def _clean_chapter_title(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip(" \t\r\n-·")


_CHAPTER_EXERCISE_KEYWORDS = ("练习", "习题", "测验", "测试", "作业", "考试", "测评")


def _is_exercise_chapter(chapter: dict) -> bool:
    title = _clean_chapter_title(str(chapter.get("title") or ""))
    return any(keyword in title for keyword in _CHAPTER_EXERCISE_KEYWORDS)


def _extract_chapters(html: str, course: dict) -> list[dict]:
    """Parse available leaf chapters from the course chapter tree."""
    soup = BeautifulSoup(html, "html.parser")
    course_id, clazz_id, cpi = _course_identifiers(course)
    chapters: list[dict] = []
    seen: set[str] = set()

    candidates = soup.select(".chapter_item[id^='cur'], .chapter_unit li, li[onclick], [onclick*='getTeacherAjax']")
    for element in candidates:
        if "chapter_item" in (element.get("class") or []):
            container = element
        else:
            container = element if getattr(element, "name", "") == "li" else element.find_parent("li") or element
        title_el = container.select_one("a.clicktitle, .catalog_name a, .catalog_name")
        if not title_el:
            continue
        title = _clean_chapter_title(title_el.get_text(" ", strip=True))
        if not title:
            continue

        onclick_parts: list[str] = []
        for clickable in container.select("[onclick]"):
            onclick_parts.append(str(clickable.get("onclick") or ""))
        if container.get("onclick"):
            onclick_parts.insert(0, str(container.get("onclick") or ""))
        href = str(title_el.get("href") or "") if getattr(title_el, "get", None) else ""

        knowledge_id = ""
        container_id = str(container.get("id") or "")
        id_match = re.fullmatch(r"cur(\d+)", container_id)
        if id_match:
            knowledge_id = id_match.group(1)
        if not knowledge_id:
            checkbox = container.select_one("input[name='checkbox'][value], input[value]")
            checkbox_value = str(checkbox.get("value") or "") if checkbox else ""
            if checkbox_value.isdigit():
                knowledge_id = checkbox_value
        for onclick in onclick_parts:
            if knowledge_id:
                break
            quoted = re.findall(r"['\"](\d+)['\"]", onclick)
            # Chaoxing's getTeacherAjax stores knowledgeId in the second quoted argument.
            if len(quoted) >= 2:
                knowledge_id = quoted[1]
                break
            if quoted:
                knowledge_id = quoted[-1]
                break
        if not knowledge_id and href:
            qs = parse_qs(urlparse(urljoin("https://mooc1.chaoxing.com/", href)).query)
            lowered = {key.lower(): values for key, values in qs.items()}
            for key in ("chapterid", "knowledgeid"):
                values = lowered.get(key)
                if values:
                    knowledge_id = str(values[0])
                    break
        if not knowledge_id or knowledge_id in seen:
            continue
        seen.add(knowledge_id)

        unit = container.find_parent(class_="chapter_unit")
        unit_title = ""
        if unit:
            unit_title_el = unit.select_one(":scope > div .catalog_name") or unit.select_one(".catalog_name")
            if unit_title_el:
                unit_title = _clean_chapter_title(unit_title_el.get_text(" ", strip=True))
        depth = max(0, len(container.find_parents("li")))
        task_el = container.select_one(".catalog_task")
        task_text = _clean_chapter_title(task_el.get_text(" ", strip=True)) if task_el else ""
        status = task_text or "可访问"
        chapters.append({
            "title": title,
            "unit_title": unit_title,
            "knowledge_id": knowledge_id,
            "chapter_id": knowledge_id,
            "course_id": course_id,
            "clazz_id": clazz_id,
            "cpi": cpi,
            "depth": depth,
            "status": status,
        })
    return chapters


def _extract_assigned_json(html: str, variable: str = "mArg") -> dict:
    """Extract a JSON object assigned to a JavaScript variable using balanced braces."""
    match = re.search(rf"\b{re.escape(variable)}\s*=\s*", html)
    if not match:
        return {}
    start = html.find("{", match.end())
    if start < 0:
        return {}
    depth = 0
    in_string = False
    escaped = False
    quote = ""
    for index in range(start, len(html)):
        char = html[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                in_string = False
            continue
        if char in {'"', "'"}:
            in_string = True
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                raw = html[start:index + 1]
                try:
                    data = json.loads(raw)
                    return data if isinstance(data, dict) else {}
                except json.JSONDecodeError:
                    return {}
    return {}


def _first_mapping_value(mapping: dict, *names: str) -> str:
    lowered = {str(key).lower(): value for key, value in mapping.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return str(value)
    return ""


def _chapter_work_url(course: dict, chapter: dict, work_id: str, job_id: str = "") -> str:
    course_id, clazz_id, cpi = _course_identifiers(course)
    return "https://mooc1.chaoxing.com/mooc-ans/api/work?" + urlencode({
        "api": "1",
        "workId": work_id,
        "jobid": job_id,
        "needRedirect": "true",
        "knowledgeid": chapter.get("knowledge_id") or chapter.get("chapter_id") or "",
        "courseId": course_id,
        "clazzId": clazz_id,
        "cpi": cpi,
        "ut": "s",
        "type": "",
    })


def _url_query_mapping(url: str) -> dict[str, str]:
    query = parse_qs(urlparse(url).query)
    return {str(key).lower(): str(values[0]) for key, values in query.items() if values}


def _chapter_exercise_key(item: dict) -> str:
    """Identify one chapter exercise across entry, frame and reviewed URLs."""
    title = _clean_chapter_title(str(item.get("chapter_title") or item.get("title") or ""))
    if title:
        # Chaoxing can expose two different pre-redirect work identifiers for
        # the same chapter card. The chapter tree title is the stable identity
        # the student sees, and chapter mode intentionally exports one quiz per
        # exercise chapter.
        return f"title|{title.casefold()}"
    work_id = str(item.get("work_id") or "")
    knowledge_id = str(item.get("knowledge_id") or item.get("chapter_id") or "")
    if work_id:
        return f"{knowledge_id}|{work_id}"
    return str(item.get("url") or item.get("title") or "")


def _merge_chapter_exercises(target: list[dict], index_by_key: dict[str, int], items: list[dict]) -> int:
    """Merge duplicate representations and prefer the reviewed answer page."""
    added = 0
    for item in items:
        if not item:
            continue
        key = _chapter_exercise_key(item)
        existing_index = index_by_key.get(key)
        if existing_index is None:
            index_by_key[key] = len(target)
            target.append(item)
            added += 1
            continue
        current = target[existing_index]
        if item.get("completed_detail") and not current.get("completed_detail"):
            target[existing_index] = {**current, **item}
    return added


def _exercise_from_url(raw_url: str, base_url: str, course: dict, chapter: dict, card_index: int) -> dict | None:
    if not raw_url:
        return None
    absolute = urljoin(base_url, raw_url)
    lowered_url = absolute.lower()
    if not any(marker in lowered_url for marker in (
        "selectworkquestionyipiyue",
        "/api/work",
        "/work/",
        "modules/work",
    )):
        return None

    values = _url_query_mapping(absolute)
    work_id = values.get("workid") or values.get("work_id") or ""
    if not work_id:
        return None
    job_id = values.get("jobid") or values.get("job_id") or ""
    answer_id = values.get("workanswerid") or values.get("answerid") or ""
    knowledge_id = values.get("knowledgeid") or values.get("chapterid") or str(
        chapter.get("knowledge_id") or chapter.get("chapter_id") or ""
    )
    chapter_title = str(chapter.get("title") or "未命名章节")
    completed = "selectworkquestionyipiyue" in lowered_url or bool(answer_id)
    return {
        "title": chapter_title,
        "chapter_title": chapter_title,
        "unit_title": chapter.get("unit_title") or "",
        "chapter_id": chapter.get("chapter_id") or knowledge_id,
        "knowledge_id": knowledge_id,
        "course_id": values.get("courseid") or _course_identifiers(course)[0],
        "clazz_id": values.get("classid") or values.get("clazzid") or _course_identifiers(course)[1],
        "work_id": work_id,
        "answer_id": answer_id,
        "work_answer_id": answer_id,
        "job_id": job_id,
        "enc": values.get("enc") or "",
        "cpi": values.get("cpi") or _course_identifiers(course)[2],
        "card_index": card_index,
        "status": "已批阅" if completed else "章节练习",
        "source_type": "chapter",
        "url": absolute,
        "completed_detail": completed,
    }


def _extract_chapter_exercises(html: str, base_url: str, course: dict, chapter: dict, card_index: int) -> list[dict]:
    data = _extract_assigned_json(html)
    defaults = data.get("defaults") if isinstance(data.get("defaults"), dict) else {}
    attachments = data.get("attachments") if isinstance(data.get("attachments"), list) else []
    exercises: list[dict] = []
    index_by_key: dict[str, int] = {}

    def store(item: dict | None) -> None:
        if not item:
            return
        _merge_chapter_exercises(exercises, index_by_key, [item])

    # In current Chaoxing pages the completed exercise is a nested frame. A
    # frame's URL is authoritative even when its HTML contains no source link.
    store(_exercise_from_url(base_url, base_url, course, chapter, card_index))

    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        prop = attachment.get("property")
        if isinstance(prop, str):
            try:
                prop = json.loads(prop)
            except json.JSONDecodeError:
                prop = {}
        if not isinstance(prop, dict):
            prop = {}
        work_id = _first_mapping_value(prop, "workid", "workId") or _first_mapping_value(attachment, "workid", "workId")
        attachment_type = _first_mapping_value(attachment, "type").lower()
        if not work_id or ("work" not in attachment_type and not any(str(key).lower() == "workid" for key in prop)):
            continue
        job_id = _first_mapping_value(attachment, "jobid", "jobId", "_jobid") or _first_mapping_value(prop, "jobid", "jobId", "_jobid")
        exercise_name = _clean_chapter_title(
            _first_mapping_value(prop, "title", "name") or _first_mapping_value(attachment, "title", "name") or "章节测验"
        )
        chapter_title = str(chapter.get("title") or "未命名章节")
        store({
            "title": chapter_title,
            "exercise_name": exercise_name,
            "chapter_title": chapter_title,
            "unit_title": chapter.get("unit_title") or "",
            "chapter_id": chapter.get("chapter_id") or chapter.get("knowledge_id") or "",
            "knowledge_id": chapter.get("knowledge_id") or _first_mapping_value(defaults, "knowledgeid") or "",
            "work_id": work_id,
            "job_id": job_id,
            "card_index": card_index,
            "status": "章节练习",
            "source_type": "chapter",
            "url": _chapter_work_url(course, chapter, work_id, job_id),
            "completed_detail": False,
        })

    # Older and newer pages may expose the work URL on different element/data attributes.
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["a", "iframe", "object", "embed"]):
        for attr in ("href", "src", "data", "data-url", "data-src"):
            store(_exercise_from_url(str(tag.get(attr) or ""), base_url, course, chapter, card_index))
    return exercises


def get_chapter_exercise_list(page: ChromiumPage, course: dict, progress_callback=None) -> list[dict]:
    """Read all chapter cards and return the embedded chapter quizzes."""
    course_id, clazz_id, cpi = _course_identifiers(course)
    page.get(_chapter_tree_url(course), timeout=25)
    page.wait.doc_loaded()
    time.sleep(1)
    all_chapters: list[dict] = []
    for _source, source_html, _base_url in _task_sources(page):
        parsed = _extract_chapters(source_html, course)
        if len(parsed) > len(all_chapters):
            all_chapters = parsed
    chapters = [chapter for chapter in all_chapters if _is_exercise_chapter(chapter)]
    if not chapters:
        return []

    exercises: list[dict] = []
    exercise_index: dict[str, int] = {}

    def append_exercises(items: list[dict]) -> int:
        return _merge_chapter_exercises(exercises, exercise_index, items)

    def cards_url_for(chapter: dict, card_index: int) -> str:
        return "https://mooc1.chaoxing.com/mooc-ans/knowledge/cards?" + urlencode({
            "clazzid": clazz_id,
            "courseid": course_id,
            "knowledgeid": chapter.get("knowledge_id") or "",
            "num": card_index,
            "ut": "s",
            "cpi": cpi,
            "v": "2025-0424-1038-3",
            "mooc2": "1",
            "isMicroCourse": "false",
            "editorPreview": "0",
        })

    def read_card(chapter: dict, card_index: int) -> int:
        cards_url = cards_url_for(chapter, card_index)
        try:
            page.get(cards_url, timeout=20)
            page.wait.doc_loaded()
        except Exception as exc:
            print(f"    [WARN] 章节卡片读取失败 {chapter.get('knowledge_id')}/{card_index}: {exc}")
            return 0
        time.sleep(0.4)
        added = 0
        for _source, source_html, source_url in _task_sources(page):
            added += append_exercises(_extract_chapter_exercises(
                source_html,
                source_url or cards_url,
                course,
                chapter,
                card_index,
            ))
        return added

    for chapter_index, chapter in enumerate(chapters, 1):
        if progress_callback:
            progress_callback(chapter_index - 1, len(chapters), f"读取练习 {chapter_index}/{len(chapters)}：{chapter.get('title')}")

        # Exercise chapters in current Chaoxing courses normally place the
        # quiz on card 0. Read it directly and avoid opening every course card.
        if read_card(chapter, 0):
            continue

        # Compatibility fallback for templates where the quiz is on a later card.
        ajax_url = "https://mooc1.chaoxing.com/mycourse/studentstudyAjax?" + urlencode({
            "courseId": course_id,
            "clazzid": clazz_id,
            "chapterId": chapter.get("knowledge_id") or "",
            "cpi": cpi,
            "verificationcode": "",
            "mooc2": "1",
        })
        try:
            page.get(ajax_url, timeout=20)
            page.wait.doc_loaded()
        except Exception as exc:
            print(f"    [WARN] 章节读取失败 {chapter.get('knowledge_id')}: {exc}")
            continue
        card_count = 0
        ajax_sources = _task_sources(page)
        for _source, source_html, source_url in ajax_sources:
            soup = BeautifulSoup(source_html, "html.parser")
            card_input = soup.select_one("input#cardcount")
            try:
                card_count = max(card_count, int(card_input.get("value") or 0) if card_input else 0)
            except (TypeError, ValueError):
                pass
            append_exercises(_extract_chapter_exercises(
                source_html,
                source_url or ajax_url,
                course,
                chapter,
                -1,
            ))
        for card_index in range(1, max(1, card_count)):
            read_card(chapter, card_index)
    if progress_callback:
        progress_callback(len(chapters), len(chapters), f"已扫描 {len(chapters)} 个练习章节，找到 {len(exercises)} 份章节练习")
    return exercises


def _page_has_questions(page: ChromiumPage, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            title = (getattr(page, "title", "") or "").strip()
            html = page.html or ""
            if title == "404" or "<title>404</title>" in html or "(404)" in html:
                return False
            if "passport2.chaoxing.com" in (page.url or ""):
                return False
            if page.ele("css:.aiAreaContent,.mark_item,.questionLi,.TiMu", timeout=1):
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def save_as_mhtml(page: ChromiumPage, detail_url: str, save_path: str) -> bool:
    """打开详情页，等加载完，用 CDP 保存为 mhtml。"""
    candidates = [detail_url]
    if "work/task" in detail_url:
        converted = task_url_to_detail_url(detail_url)
        if converted and converted not in candidates:
            candidates.append(converted)

    for url in candidates:
        page.get(url, timeout=20)
        page.wait.doc_loaded()
        time.sleep(1)

        if not _page_has_questions(page, timeout=15):
            continue

        result = page.run_cdp("Page.captureSnapshot", format="mhtml")
        mhtml_data = result.get("data", "")
        if not mhtml_data:
            continue

        if isinstance(mhtml_data, bytes):
            Path(save_path).write_bytes(mhtml_data)
        else:
            Path(save_path).write_bytes(mhtml_data.encode("utf-8"))
        return True

    return False


def run_course(
    page: ChromiumPage,
    course: dict,
    output_root: str | Path | None = None,
    with_images: bool = True,
) -> dict:
    course_name = course.get("name") or "未命名课程"
    safe_name = "".join(c if c not in r'\/:*?"<>|' else "_" for c in course_name).strip() or "课程"
    course_dir = Path(output_root) / safe_name if output_root else default_output_root() / safe_name
    mhtml_dir = course_dir / MHTML_DIR
    output_dir = course_dir / "产出文件"
    mhtml_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    tasks = get_task_list(page, course)
    saved_files = []
    for idx, task in enumerate(tasks, 1):
        title = task.get("title") or f"作业{idx:02d}"
        print(f"\n  [{idx}/{len(tasks)}] {title} ({task.get('status', '')})")
        if "answerId" not in task.get("url", ""):
            print("    [SKIP] 未作答")
            continue

        safe_title = "".join(c for c in title if c not in r'\/:*?"<>|')
        save_path = mhtml_dir / f"{idx:02d}_{safe_title}.mhtml"
        if save_path.exists():
            if parse_mhtml(str(save_path)):
                print("    [SKIP] 已存在")
                saved_files.append(str(save_path))
                continue
            save_path.unlink(missing_ok=True)

        ok = save_as_mhtml(page, task["url"], str(save_path))
        if ok:
            print(f"    [OK] 已保存: {save_path.name}")
            saved_files.append(str(save_path))
        else:
            print("    [FAIL] 保存失败")
        time.sleep(SLEEP_SEC)

    results = []
    for fpath in sorted(saved_files):
        work = parse_mhtml(fpath)
        if work:
            results.append(work)

    if not results:
        return {"ok": False, "error": "没有解析到任何题目", "output_dir": str(output_dir)}

    output_stem = "".join(c if c not in r'\/:*?"<>|' else "_" for c in course_name).strip() or "课程"
    out_json = output_dir / f"{output_stem}.json"
    out_text = output_dir / f"{output_stem}.txt"
    out_docx = output_dir / f"{output_stem}.docx"
    out_json.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    out_text.write_text("\n\n".join(format_work(work) for work in results), encoding="utf-8")
    save_docx(results, str(out_docx), with_images=with_images, course_name=course_name)
    return {"ok": True, "count": len(results), "output_dir": str(output_dir), "docx": str(out_docx)}


def main() -> None:
    username = input("账号/手机号：").strip()
    password = getpass.getpass("密码：")
    page = make_page(show_browser=True)
    try:
        result = login(page, username, password, wait_for_manual=True)
        if not result.get("ok"):
            print(f"登录失败：{result}")
            return

        courses = discover_courses(page)
        if not courses:
            print("未发现课程")
            return
        for idx, course in enumerate(courses, 1):
            print(f"{idx:03d}. {course['name']}  course_id={course['course_id']}")
        choice = int(input("选择课程序号：").strip())
        course = courses[choice - 1]
        default_root = default_output_root()
        output_root = input(f"输出根目录（留空为 {default_root}）：").strip() or str(default_root)
        print(run_course(page, course, output_root=output_root))
    finally:
        page.quit()


if __name__ == "__main__":
    main()
