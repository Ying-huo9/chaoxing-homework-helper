#!/usr/bin/env python3
"""
超星学习通 健康评估作业题目抓取脚本
从 mhtml 文件解析作业列表，再通过 requests（带登录 cookie）
逐一抓取每份作业详情页，提取题目、选项、答案、得分。

使用方式
--------
1. 把你的浏览器 Cookie 粘贴到下方 COOKIES 字典（见注释）
2. 把两个 mhtml 文件放在同目录，或修改 MHTML_* 路径
3. python chaoxing_scraper.py
4. 结果输出到 output.json、output_readable.txt 和 output.docx
"""

import email
import json
import re
import sys
import time
from pathlib import Path

import io
import requests
from bs4 import BeautifulSoup
from docx import Document as DocxDocument
from docx.shared import Pt, RGBColor, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

# ──────────────────────────────────────────────────────────────────────────────
# 配置区（必填）
# ──────────────────────────────────────────────────────────────────────────────

# 把浏览器 F12 → Application → Cookies → mooc1.chaoxing.com 里的值粘进来
# 最关键的是 UID 和 JSESSIONID（或 cx_p_token），其他可选
COOKIES: dict[str, str] = {
    # 示例（替换成你自己的）：
    # "UID": "123456789",
    # "JSESSIONID": "abcdef123456",
    # "cx_p_token": "xxxxxx",
}

# mhtml 文件路径（与脚本同目录时直接用文件名即可）
MHTML_LIST   = "健康评估2026春.mhtml"    # 包含作业列表的页面
# MHTML_DETAIL = "作业详情.mhtml"          # 只用于本地调试，正常抓取不需要

# 每次请求之间等待秒数（礼貌爬虫）
SLEEP_SEC = 1.5

# 输出文件
OUT_JSON  = "output.json"
OUT_TEXT  = "output_readable.txt"
OUT_DOCX  = "output.docx"

# ──────────────────────────────────────────────────────────────────────────────
# 第一步：从 mhtml 解析作业列表
# ──────────────────────────────────────────────────────────────────────────────

def parse_task_list(mhtml_path: str) -> list[dict]:
    """从作业列表 mhtml 提取所有作业的标题、状态、URL。"""
    with open(mhtml_path, "rb") as f:
        msg = email.message_from_binary_file(f)

    tasks = []
    for part in msg.walk():
        if part.get_content_type() != "text/html":
            continue
        loc = part.get("Content-Location", "")
        if "work/list" not in loc and "mycourse/stu" not in loc:
            continue
        html = part.get_payload(decode=True).decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")
        for li in soup.find_all("li", attrs={"data": True}):
            url = li.get("data", "").strip()
            if not url.startswith("http"):
                continue
            title_p  = li.find("p", class_="overHidden2")
            status_p = li.find("p", class_="status")
            tasks.append({
                "title":  title_p.get_text(strip=True)  if title_p  else "",
                "status": status_p.get_text(strip=True) if status_p else "",
                "url":    url,
            })

    print(f"[列表] 共找到 {len(tasks)} 份作业")
    return tasks


def _first_query_value(qs: dict[str, list[str]], *names: str) -> str:
    lowered = {key.lower(): value for key, value in qs.items()}
    for name in names:
        values = lowered.get(name.lower())
        if values:
            return values[0]
    return ""


def _clean_course_name(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    text = re.sub(r"(进入课程|课程门户|展开|收起|移动到|退课|置顶)", " ", text)
    text = re.sub(r"任务点进度[:：]?\s*\d+\s*/\s*\d+\s*\d*%?", " ", text)
    text = re.sub(r"\b\d+\s*/\s*\d+\b|\b\d{1,3}%\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -_/|·")
    return text.strip()


def _is_probable_course_name(text: str) -> bool:
    if not (2 <= len(text) <= 80):
        return False
    if not re.search(r"[\u4e00-\u9fffA-Za-z]", text):
        return False
    noisy_words = ("移动到", "退课", "置顶", "任务点进度", "进入课程", "课程门户")
    return not any(word in text for word in noisy_words)


def _guess_course_name(link) -> str:
    candidates = [
        link.get("title") or "",
        link.get_text(" ", strip=True),
    ]

    parent = link.parent
    for _ in range(6):
        if not parent:
            break
        for cls in ("course-name", "courseName", "coursename", "title", "name", "Mconright", "color1"):
            el = parent.find(class_=cls)
            if el:
                text = _clean_course_name(el.get_text(" ", strip=True))
                if _is_probable_course_name(text):
                    return text
        parent = parent.parent

    parent = link.parent
    for _ in range(6):
        if not parent:
            break
        text = parent.get_text(" ", strip=True)
        if text:
            candidates.append(text)
        parent = parent.parent

    for text in candidates:
        text = _clean_course_name(text)
        if _is_probable_course_name(text):
            return text

        parts = re.split(r"(?:移动到|退课|置顶|任务点进度|进入课程|课程门户|展开|收起|\d+\s*/\s*\d+|\d{1,3}%)", text)
        for part in parts:
            part = _clean_course_name(part)
            if _is_probable_course_name(part):
                return part
    return "未命名课程"


def discover_courses(page) -> list[dict]:
    """
    Discover courses from the logged-in Chaoxing personal space.

    The course list is often inside iframes. The stable signal is a link whose
    href contains ``stucoursemiddle`` plus course identifiers, not the visible
    link text.
    """
    from urllib.parse import parse_qs, urljoin, urlparse

    page.get("https://i.chaoxing.com/base")
    try:
        page.wait.doc_loaded()
    except Exception:
        pass
    time.sleep(2)

    html_sources = []
    try:
        html_sources.append(page.html)
    except Exception:
        pass

    try:
        frames = page.get_frames()
    except Exception:
        frames = []

    for frame in frames:
        try:
            html_sources.append(frame.html)
        except Exception:
            continue

    courses_by_key: dict[tuple[str, str, str], dict] = {}
    for html in html_sources:
        soup = BeautifulSoup(html, "html.parser")
        for link in soup.find_all("a", href=True):
            href = link.get("href", "").strip()
            if "stucoursemiddle" not in href:
                continue

            middle_url = urljoin("https://mooc1.chaoxing.com/", href)
            parsed = urlparse(middle_url)
            qs = parse_qs(parsed.query, keep_blank_values=True)
            course_id = _first_query_value(qs, "courseid", "courseId", "courseId")
            clazz_id = _first_query_value(qs, "clazzid", "classid", "clazzId", "classId")
            cpi = _first_query_value(qs, "cpi")
            if not course_id:
                continue

            key = (course_id, clazz_id, cpi)
            name = _guess_course_name(link)
            existing = courses_by_key.get(key)
            if existing:
                current_name = existing.get("name") or ""
                if current_name == "未命名课程" and name != "未命名课程":
                    existing["name"] = name
                elif len(name) < len(current_name) and _is_probable_course_name(name):
                    existing["name"] = name
                continue

            courses_by_key[key] = {
                "name": name,
                "course_id": course_id,
                "clazz_id": clazz_id,
                "cpi": cpi,
                "middle_url": middle_url,
            }

    return list(courses_by_key.values())


# ──────────────────────────────────────────────────────────────────────────────
# 第二步：把 work/task URL 转换为作业详情页 URL
# ──────────────────────────────────────────────────────────────────────────────

def task_url_to_detail_url(task_url: str) -> str:
    """
    work/task 页面会 JS 重定向到 work/doHomeWorkNew 详情页。
    直接构造目标 URL，省掉一次重定向。

    task URL 格式：
      https://mooc1.chaoxing.com/mooc-ans/mooc2/work/task
        ?courseId=...&classId=...&cpi=...&workId=...&answerId=...&enc=...

    detail URL 格式：
      https://mooc1.chaoxing.com/mooc-ans/mooc2/work/doHomeWorkNew
        ?courseId=...&classId=...&cpi=...&id=<workId>&answerId=...&enc=...
    """
    from urllib.parse import urlparse, parse_qs, urlencode

    parsed = urlparse(task_url)
    qs = parse_qs(parsed.query, keep_blank_values=True)

    def first(key):
        return qs.get(key, [""])[0]

    new_params = {
        "courseId":  first("courseId"),
        "classId":   first("classId"),
        "cpi":       first("cpi"),
        "id":        first("workId"),    # 注意：workId → id
        "answerId":  first("answerId"),
        "enc":       first("enc"),
        "isView":    "1",                # 查看已提交模式
        "type":      "view",
    }
    base = "https://mooc1.chaoxing.com/mooc-ans/mooc2/work/doHomeWorkNew"
    return f"{base}?{urlencode(new_params)}"


# ──────────────────────────────────────────────────────────────────────────────
# 第三步：抓取并解析作业详情页
# ──────────────────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://mooc1.chaoxing.com/",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def fetch_detail_html(detail_url: str, session: requests.Session) -> str | None:
    """GET 作业详情页，返回 HTML 字符串，失败返回 None。"""
    try:
        resp = session.get(detail_url, headers=HEADERS, timeout=15,
                           allow_redirects=True)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        return resp.text
    except Exception as e:
        print(f"  [错误] 请求失败: {e}")
        return None


def _extract_images(el) -> list[str]:
    """提取元素内所有图片的 src（含懒加载 data-original）。"""
    if not el:
        return []
    imgs = []
    for img in el.find_all("img"):
        src = img.get("src") or img.get("data-original") or img.get("data-src")
        if src:
            if src.startswith("//"):
                src = "https:" + src
            imgs.append(src)
    return imgs


def _text_with_image_placeholder(el) -> str:
    """获取元素文本，图片替换为 [图片N] 占位符，便于在纯文本/docx中定位。"""
    if not el:
        return ""
    el = BeautifulSoup(str(el), "html.parser")
    for i, img in enumerate(el.find_all("img"), 1):
        img.replace_with(f"[图片{i}]")
    return el.get_text(strip=True)


def convert_answer_to_letter(answer: str, options: list[str], q_type: str) -> str:
    """
    把文字答案转换为选项字母（参考 chaoxing-question-extractor 项目的转换逻辑）。
    例：正确答案是"心房波为连续规则的大锯齿波" → 转换为 "B"
    判断题统一转换为 "对"/"错"。
    """
    if not answer:
        return answer
    answer = answer.strip()

    # 判断题
    if "判断" in q_type:
        if any(k in answer for k in ("对", "正确", "√", "True", "T")):
            return "对"
        if any(k in answer for k in ("错", "错误", "×", "False", "F")):
            return "错"
        if answer == "A":
            return "对"
        if answer == "B":
            return "错"

    # 已经是字母（如 "B" 或 "AC"），直接返回
    if re.fullmatch(r"[A-Za-z,，;；\s]+", answer) and len(answer) < 10:
        return answer.upper().replace("，", ",").replace("；", ",").replace(";", ",")

    # 文字答案 → 匹配选项内容，转换为字母
    if options:
        parts = re.split(r"[,，;；]", answer)
        letters = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            matched = None
            for i, opt in enumerate(options):
                # 选项格式一般是 "A、xxx" 或 "A. xxx" 或 "A xxx"
                opt_body = re.sub(r"^[A-Za-z][\.\、\s]*", "", opt).strip()
                if opt_body == part or (len(opt_body) > 2 and (opt_body in part or part in opt_body)):
                    matched = chr(65 + i)
                    break
            letters.append(matched if matched else part)
        if letters:
            if all(re.fullmatch(r"[A-Z]", l) for l in letters):
                return "".join(sorted(letters))
            return ",".join(letters)

    return answer


def _parse_area_format1(area) -> dict | None:
    """格式1：作业详情页（mark_name / mark_letter / stuAnswerContent）"""
    h3 = area.find("h3", class_="mark_name")
    if not h3:
        return None
    q = {}
    raw_h3 = h3.get_text(" ", strip=True)
    num_match = re.match(r"(\d+)\.", raw_h3)
    q["序号"] = int(num_match.group(1)) if num_match else None

    type_span = h3.find("span", class_="colorShallow")
    q["题型"] = type_span.get_text(strip=True) if type_span else ""

    content_span = h3.find("span", class_="qtContent")
    q["题干"] = content_span.get_text(strip=True) if content_span else ""
    q["题干图片"] = _extract_images(content_span)

    ul = area.find("ul", class_="mark_letter")
    if ul:
        opts, opt_imgs = [], []
        for li in ul.find_all("li"):
            opts.append(li.get_text(strip=True))
            opt_imgs.append(_extract_images(li))
        q["选项"] = opts
        q["选项图片"] = opt_imgs
    else:
        q["选项"] = []
        q["选项图片"] = []

    my_ans_el    = area.find("span", class_="stuAnswerContent")
    right_ans_el = area.find("span", class_="rightAnswerContent")
    q["我的答案"] = my_ans_el.get_text(strip=True)    if my_ans_el    else ""
    q["正确答案"] = right_ans_el.get_text(strip=True) if right_ans_el else ""

    score_el = area.find("div", class_="totalScore")
    if score_el:
        try:
            q["得分"] = float(score_el.get_text(strip=True).replace("分", "").strip())
        except ValueError:
            q["得分"] = None
    else:
        q["得分"] = None

    dui = area.find("span", class_="marking_dui")
    cuo = area.find("span", class_="marking_cuo")
    q["是否正确"] = True if dui else (False if cuo else None)

    analysis_el = area.find("span", class_="qtAnalysis")
    q["答案解析"] = analysis_el.get_text(strip=True) if analysis_el else ""
    q["解析图片"] = _extract_images(analysis_el)

    return q


def _parse_area_format2(area, idx: int) -> dict | None:
    """格式2：学习页面内嵌作业（TiMu / Zy_TItle / newAnswerBx）"""
    timu = area.find("div", class_="TiMu")
    if not timu:
        return None
    q = {}
    q["序号"] = idx

    title_div = timu.find("div", class_="Zy_TItle")
    if title_div:
        type_span = title_div.find("span", class_="newZy_TItle")
        q["题型"] = type_span.get_text(strip=True) if type_span else ""
        q["题干图片"] = _extract_images(title_div)
        if type_span:
            type_span.decompose()
        q["题干"] = title_div.get_text(strip=True)
    else:
        q["题型"] = ""
        q["题干"] = ""
        q["题干图片"] = []

    ul = timu.find("ul", class_="Zy_ulTop")
    if ul:
        opts, opt_imgs = [], []
        for li in ul.find_all("li"):
            opt_imgs.append(_extract_images(li))
            letter = li.find("i")
            letter_text = letter.get_text(strip=True) if letter else ""
            if letter:
                letter.decompose()
            body = li.get_text(strip=True)
            opts.append(f"{letter_text}{body}")
        q["选项"] = opts
        q["选项图片"] = opt_imgs
    else:
        q["选项"] = []
        q["选项图片"] = []

    ans_bx = area.find("div", class_="newAnswerBx")
    if ans_bx:
        my_div = ans_bx.find("div", class_="answerCon")
        q["我的答案"] = my_div.get_text(strip=True) if my_div else ""

        right_div = ans_bx.find("div", class_="rightAnswerCon")
        q["正确答案"] = right_div.get_text(strip=True) if right_div else q["我的答案"]

        score_el = ans_bx.find("span", class_="scoreNum")
        try:
            q["得分"] = float(score_el.get_text(strip=True)) if score_el else None
        except ValueError:
            q["得分"] = None

        dui = ans_bx.find("span", class_="marking_dui")
        cuo = ans_bx.find("span", class_="marking_cuo")
        q["是否正确"] = True if dui else (False if cuo else None)
    else:
        q["我的答案"] = ""
        q["正确答案"] = ""
        q["得分"] = None
        q["是否正确"] = None

    analysis_el = area.find("div", class_="aiAnalysis") or area.find("span", class_="qtAnalysis")
    q["答案解析"] = analysis_el.get_text(strip=True) if analysis_el else ""
    q["解析图片"] = _extract_images(analysis_el)

    return q


def parse_detail_html(html: str) -> list[dict]:
    """从作业详情 HTML 提取所有题目，支持两种页面格式，并补充图片与字母化答案。"""
    soup = BeautifulSoup(html, "html.parser")
    questions = []
    fmt2_idx = 1

    for area in soup.find_all("div", class_="aiAreaContent"):
        h3 = area.find("h3", class_="mark_name")
        if h3:
            q = _parse_area_format1(area)
        else:
            q = _parse_area_format2(area, fmt2_idx)
            if q:
                fmt2_idx += 1

        if not q:
            continue

        # 答案字母化（仅在没有正确答案文字、或正确答案非字母时才转换，保留原始信息）
        q["正确答案_字母"] = convert_answer_to_letter(q.get("正确答案", ""), q.get("选项", []), q.get("题型", ""))
        q["我的答案_字母"]  = convert_answer_to_letter(q.get("我的答案", ""),  q.get("选项", []), q.get("题型", ""))

        questions.append(q)

    return questions


# ──────────────────────────────────────────────────────────────────────────────
# 第四步：本地 mhtml 也可以解析（离线调试用）
# ──────────────────────────────────────────────────────────────────────────────

def parse_detail_mhtml(mhtml_path: str) -> list[dict]:
    """从已保存的作业详情 mhtml 提取题目（不需要网络）。"""
    with open(mhtml_path, "rb") as f:
        msg = email.message_from_binary_file(f)
    for part in msg.walk():
        if part.get_content_type() != "text/html":
            continue
        html = part.get_payload(decode=True).decode("utf-8", errors="replace")
        qs = parse_detail_html(html)
        if qs:
            return qs
    return []


# ──────────────────────────────────────────────────────────────────────────────
# 第五步：格式化输出
# ──────────────────────────────────────────────────────────────────────────────

def format_question(q: dict) -> str:
    lines = []
    correct_mark = "OK" if q.get("是否正确") else ("X" if q.get("是否正确") is False else "?")
    lines.append(f"第{q.get('序号','')}题 {q.get('题型','')}  {correct_mark}  得分：{q.get('得分','')}")
    lines.append(f"  {q.get('题干','')}")
    for opt in q.get("选项", []):
        lines.append(f"    {opt}")
    lines.append(f"  我的答案：{q.get('我的答案','')}　正确答案：{q.get('正确答案','')}")
    if q.get("答案解析") and q["答案解析"] not in ("无。", "无", ""):
        lines.append(f"  解析：{q['答案解析']}")
    return "\n".join(lines)


def format_work(work: dict) -> str:
    sep = "=" * 60
    lines = [sep, f"【{work['title']}】  {work['status']}"]
    if work.get("error"):
        lines.append(f"  !! 获取失败: {work['error']}")
    else:
        for q in work.get("questions", []):
            lines.append("")
            lines.append(format_question(q))
    lines.append(sep)
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# 第六步：保存为 Word 文档
# ──────────────────────────────────────────────────────────────────────────────

def _set_cell_bg(cell, hex_color: str):
    """给表格单元格设置背景色。"""
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    tcPr.append(shd)


def _add_run(para, text: str, bold=False, color: str | None = None, size_pt: int = 11):
    run = para.add_run(text)
    run.bold = bold
    run.font.size = Pt(size_pt)
    if color:
        r, g, b = int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
        run.font.color.rgb = RGBColor(r, g, b)
    return run


_IMAGE_CACHE: dict = {}

def _download_image(url: str) -> bytes | None:
    """下载图片并缓存，避免重复请求。"""
    if url in _IMAGE_CACHE:
        return _IMAGE_CACHE[url]
    try:
        resp = requests.get(url, timeout=10, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://mooc1.chaoxing.com/",
        })
        resp.raise_for_status()
        data = resp.content
        _IMAGE_CACHE[url] = data
        return data
    except Exception:
        _IMAGE_CACHE[url] = None
        return None


def _add_images_to_doc(doc, image_urls: list[str], max_width_cm: float = 10):
    """把图片列表插入文档（居中显示），下载失败的跳过。"""
    for url in image_urls:
        data = _download_image(url)
        if not data:
            continue
        try:
            img_para = doc.add_paragraph()
            img_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
            img_para.paragraph_format.space_before = Pt(2)
            img_para.paragraph_format.space_after = Pt(4)
            run = img_para.add_run()
            run.add_picture(io.BytesIO(data), width=Cm(max_width_cm))
        except Exception:
            continue


def _write_question_simple(doc, q: dict, number: int, with_images: bool):
    """
    按参考样式写入单道题：
      <题干>
      A. xxx
      B. xxx
      正确答案：X
      解析：xxx（仅当存在解析时才写）
    不显示题号前缀、不显示"我的答案"、不显示得分、不加颜色高亮——风格干净统一。
    """
    q_para = doc.add_paragraph()
    q_para.paragraph_format.space_before = Pt(6)
    _add_run(q_para, q.get("题干", ""), bold=True, size_pt=11)

    if with_images and q.get("题干图片"):
        _add_images_to_doc(doc, q["题干图片"])

    options  = q.get("选项", [])
    opt_imgs = q.get("选项图片", [])

    for i, opt in enumerate(options):
        opt_para = doc.add_paragraph()
        opt_para.paragraph_format.space_before = Pt(1)
        # 参考样式选项用 "A. " 而不是 "A、"，统一格式
        normalized = re.sub(r"^([A-Za-z])[、\.]?\s*", r"\1. ", opt, count=1)
        _add_run(opt_para, normalized, size_pt=10.5)
        if with_images and i < len(opt_imgs) and opt_imgs[i]:
            _add_images_to_doc(doc, opt_imgs[i], max_width_cm=6)

    right_ans = q.get("正确答案_字母") or q.get("正确答案", "")
    if right_ans:
        ans_para = doc.add_paragraph()
        ans_para.paragraph_format.space_before = Pt(2)
        _add_run(ans_para, f"正确答案：{right_ans}", bold=True, size_pt=10.5)

    analysis = q.get("答案解析", "")
    if analysis and analysis not in ("无。", "无", ""):
        a_para = doc.add_paragraph()
        a_para.paragraph_format.space_before = Pt(2)
        _add_run(a_para, f"解析：{analysis}", color="555555", size_pt=10)
        if with_images and q.get("解析图片"):
            _add_images_to_doc(doc, q["解析图片"], max_width_cm=8)

    doc.add_paragraph()  # 题目间空行


# 题型归类顺序（与参考文档一致：单选→多选→判断→填空→其他）
_TYPE_GROUPS = [
    ("单选", "单选题"),
    ("多选", "多选题"),
    ("判断", "判断题"),
    ("填空", "填空题"),
    ("简答", "简答 / 论述题"),
    ("论述", "简答 / 论述题"),
    ("名词解释", "名词解释"),
]

_CN_NUM = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]


def _group_by_type(questions: list[dict]) -> dict:
    """按题型分组，未匹配到的归入"其他题"。"""
    groups = {}
    order = []
    for keyword, label in _TYPE_GROUPS:
        if label not in groups:
            groups[label] = []
            order.append(label)
    groups["其他题"] = []
    order.append("其他题")

    for q in questions:
        qtype = q.get("题型", "")
        placed = False
        for keyword, label in _TYPE_GROUPS:
            if keyword in qtype:
                groups[label].append(q)
                placed = True
                break
        if not placed:
            groups["其他题"].append(q)

    return {label: groups[label] for label in order if groups[label]}


def _type_summary(groups: dict) -> str:
    """生成"题量：N道题（5道单选题、3道多选题）"格式的概述文字。"""
    total = sum(len(v) for v in groups.values())
    parts = [f"{len(v)}道{label}" for label, v in groups.items()]
    return f"题量：{total}道题（{'、'.join(parts)}）"


def _set_outline_level(paragraph, level: int) -> None:
    """Add a Word outline level without changing the paragraph's visual style."""
    p_pr = paragraph._p.get_or_add_pPr()
    outline = p_pr.find(qn("w:outlineLvl"))
    if outline is None:
        outline = OxmlElement("w:outlineLvl")
        p_pr.append(outline)
    outline.set(qn("w:val"), str(level))


def save_docx(results: list[dict], path: str, with_images: bool = True,
              course_name: str = "", class_name: str = "", student_name: str = ""):
    """
    把所有作业题目写入一份排版简洁的 Word 文档（参考样式：作业N：标题 → 题量/满分 → 分类 → 题干/选项/答案/解析）。

    with_images:  是否下载并嵌入题目/选项/解析中的图片
    course_name / class_name / student_name: 可选，填入后会在文档开头生成"课程信息"区块
    """
    doc = DocxDocument()

    style = doc.styles["Normal"]
    style.font.name = "Arial"
    style.font.size = Pt(11)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")

    section = doc.sections[0]
    section.page_width  = Cm(21)
    section.page_height = Cm(29.7)
    section.left_margin = section.right_margin = Cm(2.5)
    section.top_margin  = section.bottom_margin = Cm(2.5)

    valid_works = [w for w in results if w.get("questions")]

    # ── 主标题 ───────────────────────────────────────────────────────────────
    title_para = doc.add_paragraph()
    title_prefix = f"{course_name.strip()}" if course_name and course_name.strip() else ""
    _add_run(title_para, f"{title_prefix}作业题目汇总（共{len(valid_works)}个作业）", bold=True, size_pt=18)

    sub_para = doc.add_paragraph()
    _add_run(sub_para, f"{title_prefix}作业题目汇总", bold=True, size_pt=14)
    _set_outline_level(sub_para, 0)

    # ── 课程信息区块（可选） ─────────────────────────────────────────────────
    if course_name or class_name or student_name:
        info_title = doc.add_paragraph()
        _add_run(info_title, "课程信息", bold=True, size_pt=12)
        _set_outline_level(info_title, 1)
        if course_name:
            doc.add_paragraph().add_run(f"课程名称：{course_name}")
        if class_name:
            doc.add_paragraph().add_run(f"班级：{class_name}")
        if student_name:
            doc.add_paragraph().add_run(f"学生：{student_name}")
        doc.add_paragraph().add_run(f"作业总数：{len(valid_works)}个")

    doc.add_paragraph()

    # ── 逐份作业 ─────────────────────────────────────────────────────────────
    for idx, work in enumerate(valid_works, 1):
        questions = work["questions"]
        groups = _group_by_type(questions)

        h = doc.add_paragraph()
        _add_run(h, f"作业{idx}：{work['title']}", bold=True, size_pt=12)
        _set_outline_level(h, 1)

        summary_para = doc.add_paragraph()
        _add_run(summary_para, _type_summary(groups), size_pt=10.5)

        total_score = sum(q.get("得分") or 0 for q in questions)
        full_score_known = all(q.get("得分") is not None for q in questions)
        score_para = doc.add_paragraph()
        if full_score_known:
            _add_run(score_para, f"本次得分：{total_score:.1f}分", size_pt=10.5)
        else:
            _add_run(score_para, "满分：100分", size_pt=10.5)

        for gi, (label, qs) in enumerate(groups.items(), 1):
            cn = _CN_NUM[gi - 1] if gi <= len(_CN_NUM) else str(gi)
            gp = doc.add_paragraph()
            gp.paragraph_format.space_before = Pt(8)
            _add_run(gp, f"{cn}、{label}（共{len(qs)}题）", bold=True, size_pt=11)
            _set_outline_level(gp, 2)
            for q in qs:
                _write_question_simple(doc, q, q.get("序号", "?"), with_images)

        doc.add_paragraph()

    doc.save(path)
    print(f"[完成] Word 文档已保存到 {path}")


def main():
    # 1. 解析作业列表
    if not Path(MHTML_LIST).exists():
        print(f"[错误] 找不到文件: {MHTML_LIST}")
        sys.exit(1)
    tasks = parse_task_list(MHTML_LIST)

    # 2. 初始化 session
    session = requests.Session()
    session.cookies.update(COOKIES)

    results = []

    for idx, task in enumerate(tasks, 1):
        print(f"\n[{idx}/{len(tasks)}] {task['title']}  ({task['status']})")

        work_entry = {
            "title":     task["title"],
            "status":    task["status"],
            "task_url":  task["url"],
            "questions": [],
            "error":     None,
        }

        # 如果未完成，跳过（没有 answerId）
        if "answerId" not in task["url"]:
            print("  [SKIP] 未作答")
            work_entry["error"] = "未作答"
            results.append(work_entry)
            continue

        detail_url = task_url_to_detail_url(task["url"])
        work_entry["detail_url"] = detail_url
        print(f"  -> {detail_url}")

        # ── 如果没有配置 Cookie，尝试本地 mhtml（仅第一份） ────────
        if not COOKIES and idx == 1:
            local = Path("作业详情.mhtml")
            if local.exists():
                print("  -> Cookie 未配置，改用本地 mhtml（仅演示第一份）")
                qs = parse_detail_mhtml(str(local))
                work_entry["questions"] = qs
                results.append(work_entry)
                _print_summary(qs)
                continue

        html = fetch_detail_html(detail_url, session)
        if not html:
            work_entry["error"] = "请求失败"
            results.append(work_entry)
            continue

        # 检测是否被重定向到登录页
        if "登录" in html[:500] or "login" in html[:500].lower():
            print("  [警告] 疑似被重定向到登录页，Cookie 可能已过期")
            work_entry["error"] = "未登录或 Cookie 失效"
            results.append(work_entry)
            break

        qs = parse_detail_html(html)
        if not qs:
            print("  [警告] 未解析到题目，可能页面结构有变")
            work_entry["error"] = "解析失败"
        else:
            work_entry["questions"] = qs
            _print_summary(qs)

        results.append(work_entry)
        time.sleep(SLEEP_SEC)

    # 3. 保存结果
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n[完成] JSON 已保存到 {OUT_JSON}")

    with open(OUT_TEXT, "w", encoding="utf-8") as f:
        for work in results:
            f.write(format_work(work) + "\n\n")
    print(f"[完成] 可读文本已保存到 {OUT_TEXT}")

    save_docx(results, OUT_DOCX)


def _print_summary(qs: list[dict]):
    total = len(qs)
    correct = sum(1 for q in qs if q.get("是否正确") is True)
    score = sum(q.get("得分") or 0 for q in qs)
    print(f"  -> {total} 题，答对 {correct} 题，总得分 {score:.1f}")


# ──────────────────────────────────────────────────────────────────────────────
# 也可以单独解析本地 mhtml（不需要网络）
# ──────────────────────────────────────────────────────────────────────────────

def demo_local():
    """仅解析本地两个 mhtml，不发网络请求，方便验证输出格式。"""
    print("=== 本地 Demo 模式（无需 Cookie）===\n")
    tasks = parse_task_list(MHTML_LIST)

    # 只解析已保存的那份作业详情
    detail_mhtml = "作业详情.mhtml"
    if not Path(detail_mhtml).exists():
        print(f"找不到 {detail_mhtml}，请提供该文件")
        return

    qs = parse_detail_mhtml(detail_mhtml)

    # 找对应的任务标题
    title = tasks[0]["title"] if tasks else "（未知）"
    work = {"title": title, "status": "已完成", "questions": qs}

    print(format_work(work))

    with open(OUT_TEXT, "w", encoding="utf-8") as f:
        f.write(format_work(work))
    print(f"\n已保存到 {OUT_TEXT}")

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump([{**work, "task_url": tasks[0]["url"] if tasks else ""}],
                  f, ensure_ascii=False, indent=2)
    print(f"已保存到 {OUT_JSON}")

    save_docx([{**work, "task_url": tasks[0]["url"] if tasks else ""}], OUT_DOCX)


if __name__ == "__main__":
    if "--demo" in sys.argv or not COOKIES:
        demo_local()
    else:
        main()
