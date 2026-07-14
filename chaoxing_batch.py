#!/usr/bin/env python3
"""
超星学习通 本地 mhtml 批量解析脚本
------------------------------------
把所有作业详情的 mhtml 文件放到同一个文件夹，
脚本自动识别、解析、汇总，输出到一个 output.docx。

使用方式：
  1. 把所有 mhtml 文件放到脚本同目录下（或修改 MHTML_DIR）
  2. python chaoxing_batch.py
  3. 结果输出到 output.docx / output.json / output_readable.txt

支持两种 mhtml 类型：
  - 作业详情页（含 aiAreaContent，直接解析题目）
  - 作业列表页（含 li[data]，提取作业标题）
"""

import email
import json
import sys
from pathlib import Path

from bs4 import BeautifulSoup

from chaoxing_scraper import (
    parse_detail_html,
    format_work,
    save_docx,
)

# ──────────────────────────────────────────────────────────────────────────────
# 配置
# ──────────────────────────────────────────────────────────────────────────────

# mhtml 文件所在目录（默认脚本同目录）
MHTML_DIR = "."

# 输出文件
OUT_JSON = "output.json"
OUT_TEXT = "output_readable.txt"
OUT_DOCX = "output.docx"

# 是否下载并嵌入题目/选项/解析中的图片（会增加耗时，取决于图片数量和网速）
WITH_IMAGES = True

# 可选：课程信息（留空则不显示课程信息区块）
COURSE_NAME  = ""
CLASS_NAME   = ""
STUDENT_NAME = ""

# ──────────────────────────────────────────────────────────────────────────────
# 从单个 mhtml 提取 HTML 内容
# ──────────────────────────────────────────────────────────────────────────────

def extract_html_parts(mhtml_path: str) -> list[str]:
    """提取 mhtml 里所有 text/html part 的内容。"""
    raw = Path(mhtml_path).read_bytes()
    # CDP snapshots already contain CRLF. If they were saved through Windows
    # text mode, "\n" may have been expanded again to "\r\r\n".
    raw = raw.replace(b"\r\r\n", b"\r\n")
    msg = email.message_from_bytes(raw)
    parts = []
    for part in msg.walk():
        if part.get_content_type() == 'text/html':
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or 'utf-8'
                parts.append(payload.decode(charset, errors='replace'))
    return parts

# ──────────────────────────────────────────────────────────────────────────────
# 判断 mhtml 类型并解析
# ──────────────────────────────────────────────────────────────────────────────

def parse_mhtml(mhtml_path: str) -> dict | None:
    """
    解析单个 mhtml 文件，返回：
    {
        "title": "作业名称",
        "status": "已完成",
        "source": "文件名",
        "questions": [...],
        "error": None
    }
    返回 None 表示不是作业详情页。
    扫描所有 html part，合并所有题目（应对学习页面内嵌多个作业的情况）。
    """
    fname = Path(mhtml_path).name
    parts = extract_html_parts(mhtml_path)

    all_questions = []
    best_title = None

    for html in parts:
        soup = BeautifulSoup(html, 'html.parser')
        areas = soup.find_all('div', class_='aiAreaContent')
        if not areas:
            continue

        questions = parse_detail_html(html)
        if not questions:
            continue

        # 取第一个有题目的 part 的标题
        if best_title is None:
            best_title = _guess_title(soup, fname)

        all_questions.extend(questions)

    if not all_questions:
        return None

    return {
        "title":     best_title or Path(fname).stem,
        "status":    "已完成",
        "source":    fname,
        "questions": all_questions,
        "error":     None,
    }


def _guess_title(soup: BeautifulSoup, fallback: str) -> str:
    """尽量从页面提取作业名称。"""
    # 1. <title> 标签
    t = soup.find('title')
    if t and t.text.strip() and t.text.strip() not in ('作业详情', '提示', ''):
        return t.text.strip()
    # 2. 页面顶部的 h1/h2
    for tag in ('h1', 'h2', 'h3'):
        el = soup.find(tag)
        if el and el.text.strip():
            return el.text.strip()
    # 3. 文件名去掉扩展名
    return Path(fallback).stem

# ──────────────────────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────────────────────

def main():
    mhtml_dir = Path(MHTML_DIR)
    files = sorted(mhtml_dir.glob("*.mhtml"))

    if not files:
        print(f"[错误] 在 {mhtml_dir.resolve()} 下没有找到 .mhtml 文件")
        sys.exit(1)

    print(f"找到 {len(files)} 个 mhtml 文件，开始解析...\n")

    results = []
    skipped = []

    for i, f in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {f.name}")
        try:
            work = parse_mhtml(str(f))
            if work:
                q_count = len(work['questions'])
                print(f"  [OK] 解析到 {q_count} 题 -> {work['title']}")
                results.append(work)
            else:
                print(f"  [SKIP] 不含作业题目")
                skipped.append(f.name)
        except Exception as e:
            print(f"  [FAIL] 解析失败：{e}")
            skipped.append(f.name)

    print(f"\n{'='*50}")
    print(f"共解析 {len(results)} 份作业，跳过 {len(skipped)} 个文件")
    if skipped:
        print(f"跳过的文件：{', '.join(skipped)}")

    if not results:
        print("没有解析到任何题目，退出")
        sys.exit(1)

    # 统计
    total_q  = sum(len(w['questions']) for w in results)
    total_ok = sum(
        sum(1 for q in w['questions'] if q.get('是否正确') is True)
        for w in results
    )
    total_score = sum(
        sum(q.get('得分') or 0 for q in w['questions'])
        for w in results
    )
    print(f"共 {total_q} 题，答对 {total_ok} 题，总得分 {total_score:.1f}")

    # 保存 JSON
    with open(OUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n[OK] JSON -> {OUT_JSON}")

    # 保存可读文本
    with open(OUT_TEXT, 'w', encoding='utf-8') as f:
        for work in results:
            f.write(format_work(work) + '\n\n')
    print(f"[OK] 文本 -> {OUT_TEXT}")

    # 保存 Word
    save_docx(results, OUT_DOCX, with_images=WITH_IMAGES,
              course_name=COURSE_NAME, class_name=CLASS_NAME, student_name=STUDENT_NAME)
    print(f"[OK] Word -> {OUT_DOCX}")


if __name__ == '__main__':
    main()
