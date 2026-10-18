#!/usr/bin/env python3
"""Parse captured question pages and build Markdown-only deliverables.

Question-type support (v2):
- 判断题 (true/false) without options: answer text 正确/错误 (对/错、T/F、√/×) is
  normalized to A.正确 / B.错误 automatically.
- 选择题 options may use A through H (not only A–D); answers are validated against
  the complete answer string — an answer letter that is missing from the options is
  quarantined instead of being silently truncated.
- Optional page fields: 题型 (判断题/单选题/多选题/…), 主题 (theme used for grouping).
- Hard stop: if the captured bank provides NO standard answer at all, the task stops
  (exit code 2) and asks the user to decide the next action. Self-answered or inferred
  answers must never be substituted.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import tempfile
import unicodedata
from collections import Counter, OrderedDict, defaultdict
from pathlib import Path
from typing import Any


SUPPLEMENT_CHAPTER = 99
JUNK_QUESTIONS = {"0.02", "0.03", "0.04", "0.05", "0.08", "0.2"}
PAGE_PATTERN = re.compile(r"_page(\d+)\.txt$", re.IGNORECASE)
OPTION_LETTERS = "ABCDEFGH"
JUDGE_TRUE = {"正确", "对", "√", "T", "TRUE", "是", "Y", "YES"}
JUDGE_FALSE = {"错误", "错", "×", "X", "F", "FALSE", "否", "N", "NO"}
JUDGE_TRUE_UPPER = {value.upper() for value in JUDGE_TRUE}
JUDGE_FALSE_UPPER = {value.upper() for value in JUDGE_FALSE}
# Option bodies are matched with `[\s\S]` rather than `.` so a body that wraps
# across lines cannot be cut short, and the value terminator is \Z rather than $.
# `答案` joins the label set so a restated answer that follows the last option
# cannot be absorbed into that option's text.
OPTION_PATTERN = re.compile(
    r"(?m)^\s*([A-H])[.．、]\s*([\s\S]*?)"
    r"(?=^\s*[A-H][.．、]\s*|^\s*(?:标准答案|答案解析|知识点|所选答案|答案|题型|主题)[：:]|\Z)"
)
FIELD_STOP = r"^\s*(?:知识点|标准答案|答案解析|所选答案|答案|题型|主题)[：:]"


class InputLimitError(ValueError):
    """Raised when a corpus crosses an explicitly configured safety bound."""


class MissingAnswerError(RuntimeError):
    """Raised when the captured bank provides no standard answer at all."""


def normalize_text(content: str) -> str:
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    content = content.replace("\\n", "\n").strip().strip('"')
    content = re.sub(r"\n上页.*?下页(?:\n|$)", "\n", content, flags=re.DOTALL)
    content = re.sub(r"\(纠错\)\s*\(分析\)|\(纠错\)|\(分析\)", "", content)
    content = re.sub(r"[ \t]+", " ", content)
    content = re.sub(r"\n{3,}", "\n\n", content)
    # Normalize common inline option layouts into one option per line. Only spaces
    # and tabs trigger the split so a line that legitimately starts with "E. coli"
    # is not turned into option E.
    content = re.sub(r"[ \t]+(?=[A-H][.．、]\s+)", "\n", content)
    content = re.sub(r"(?<!\n)(?=(?:标准答案|答案解析|知识点|题型|主题)[：:])", "\n", content)
    return content.strip()


def normalize_knowledge(value: str) -> str:
    return (
        str(value or "")
        .strip()
        .replace("\u3000", " ")
        .replace("—", "-")
        .replace("．", ".")
    )


def normalize_answer(raw: str) -> tuple[str, str]:
    """Return (normalized_answer, mode). mode: letters | judge | missing."""
    text = str(raw or "").strip()
    if not text:
        return "", "missing"
    letters = re.findall(r"[A-H]", text.upper())
    if letters and re.fullmatch(r"[A-Ha-h\s,，、;；/]+", text):
        return "".join(dict.fromkeys(letters)), "letters"
    key = re.sub(r"[\s。.,，、;；]", "", text).upper()
    if key in JUDGE_TRUE_UPPER:
        return "A", "judge"
    if key in JUDGE_FALSE_UPPER:
        return "B", "judge"
    if letters:
        # Mixed text: keep only explicit letters (still validated against options).
        return "".join(dict.fromkeys(letters)), "letters"
    return "", "missing"


def judge_letter_for_option(text: str) -> str | None:
    key = re.sub(r"[\s。.,，、;；]", "", str(text or "")).upper()
    if key in JUDGE_TRUE_UPPER:
        return "A"
    if key in JUDGE_FALSE_UPPER:
        return "B"
    return None


def split_blocks(content: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?m)(?=^\s*\d+[\.．、]\s*)", content) if part.strip()]


CN_NUMERALS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def chinese_numeral(text: str) -> int | None:
    """Convert a Chinese numeral (一…九十九) to an int, or None if not valid."""
    if not text or any(char not in CN_NUMERALS and char != "十" for char in text):
        return None
    if "十" not in text:
        return CN_NUMERALS.get(text)
    tens, _, ones = text.partition("十")
    if text.count("十") != 1:
        return None
    high = CN_NUMERALS[tens] if tens else 1
    low = CN_NUMERALS[ones] if ones else 0
    return high * 10 + low


def extract_chapter(knowledge: str, max_chapter: int) -> tuple[int, tuple[int, int, int, int]]:
    """Derive (chapter, sub-parts) from a knowledge-point label."""
    value = normalize_knowledge(knowledge)
    if not value:
        return SUPPLEMENT_CHAPTER, (0, 0, 0, 0)
    match = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?", value)
    if match:
        parts = tuple(int(match.group(index) or 0) for index in range(1, 5))
        if 1 <= parts[0] <= max_chapter:
            return parts[0], parts
        return SUPPLEMENT_CHAPTER, (0, 0, 0, 0)
    chinese = re.match(r"^第([一二三四五六七八九十]+)章", value)
    if chinese:
        chapter = chinese_numeral(chinese.group(1))
        if chapter and 1 <= chapter <= max_chapter:
            return chapter, (chapter, 0, 0, 0)
    return SUPPLEMENT_CHAPTER, (0, 0, 0, 0)


def _field(block: str, label: str) -> str:
    match = re.search(rf"(?ms)^\s*{label}[：:]\s*(.*?)(?={FIELD_STOP}|\Z)", block)
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


LEADING_RESTATED_ANSWER = re.compile(
    r"^\s*答案[：:]\s*([A-Ha-h](?:\s*[、,，]?\s*[A-Ha-h])*)\s*[.．、)）]?\s*"
)


def strip_restated_answer(analysis: str) -> str:
    match = LEADING_RESTATED_ANSWER.match(analysis)
    return analysis[match.end():] if match else analysis


def infer_question_type(qtype: str, options: dict[str, str], answer: str) -> str:
    if qtype:
        return qtype
    if set(options) == {"A", "B"} and {judge_letter_for_option(v) for v in options.values()} == {"A", "B"}:
        return "判断题"
    if len(answer) > 1:
        return "多选题"
    if len(options) >= 2 and len(answer) == 1:
        return "单选题"
    return ""


def parse_block(
    block: str, max_chapter: int, max_field_chars: int
) -> tuple[dict[str, Any] | None, list[str]]:
    reasons: list[str] = []
    if "答题范围" in block or block[:20].lstrip().startswith("重新答题"):
        return None, ["页面噪声"]
    number_match = re.match(r"^\s*(\d+)[\.．、]\s*", block)
    if not number_match:
        return None, ["无法识别题号"]

    qtype = normalize_knowledge(_field(block, "题型"))
    theme = normalize_knowledge(_field(block, "主题"))

    options = {key: re.sub(r"\s+", " ", value).strip() for key, value in OPTION_PATTERN.findall(block)}
    options = {key: options[key] for key in sorted(options)}

    first_option = re.search(r"(?m)^\s*A[.．、]\s*", block)
    cut_candidates = [match.start() for match in [first_option] if match]
    for marker in ("知识点", "标准答案", "所选答案", "答案解析", "题型", "主题"):
        found = re.search(rf"(?m)^\s*{marker}[：:]", block)
        if found:
            cut_candidates.append(found.start())
    question_region = block[: min(cut_candidates)] if cut_candidates else block
    question = re.sub(r"^\s*\d+[.．、]\s*", "", question_region)
    question = re.sub(r"\s+", " ", question).strip()

    answer_raw = _field(block, "标准答案")
    answer, answer_mode = normalize_answer(answer_raw)
    judge_normalized = False
    if answer_mode == "judge":
        target_letter = answer
        matched = [key for key, value in options.items() if judge_letter_for_option(value) == target_letter]
        if matched:
            answer = matched[0]
        elif len(options) < 2:
            options = {"A": "正确", "B": "错误"}
            answer = target_letter
            judge_normalized = True
    knowledge = normalize_knowledge(_field(block, "知识点"))
    analysis = strip_restated_answer(_field(block, "答案解析"))
    qtype = infer_question_type(qtype, options, answer)

    if not question or question in JUNK_QUESTIONS:
        reasons.append("题干为空或属于已知噪声")
    if len(options) < 2:
        reasons.append("有效选项少于两个")
    if not answer:
        reasons.append("缺少标准答案")
    elif any(letter not in options for letter in answer):
        reasons.append("答案字母不在已有选项中")
    fields = {"题目": question, "知识点": knowledge, "解析": analysis, "题型": qtype, **{f"选项{key}": value for key, value in options.items()}}
    oversized = [name for name, value in fields.items() if len(value) > max_field_chars]
    if oversized:
        reasons.append("字段超出长度限制：" + "、".join(oversized))
    if reasons:
        return None, reasons

    chapter, subchapter = extract_chapter(knowledge, max_chapter)
    return {
        "题目": question,
        "选项": options,
        "标准答案": answer,
        "知识点": knowledge,
        "解析": analysis,
        "题型": qtype,
        "主题": theme,
        "_chapter": chapter,
        "_sub": subchapter,
        "_source_number": int(number_match.group(1)),
        "_judge_normalized": judge_normalized,
        "_answer_mode": answer_mode,
    }, []


def discover_pages(input_dir: Path, max_files: int, max_file_bytes: int, max_total_bytes: int) -> list[tuple[int, Path]]:
    if input_dir.is_symlink() or not input_dir.is_dir():
        raise InputLimitError("输入必须是非符号链接目录")
    pages: list[tuple[int, Path]] = []
    seen_numbers: set[int] = set()
    total_bytes = 0
    for path in input_dir.iterdir():
        match = PAGE_PATTERN.search(path.name)
        if not match:
            continue
        if path.is_symlink() or not path.is_file():
            raise InputLimitError(f"拒绝符号链接或非普通文件：{path.name}")
        page_number = int(match.group(1))
        if page_number in seen_numbers:
            raise InputLimitError(f"检测到重复页码：{page_number}")
        seen_numbers.add(page_number)
        size = path.stat().st_size
        if size > max_file_bytes:
            raise InputLimitError(f"单页超过大小限制：{path.name}")
        total_bytes += size
        if total_bytes > max_total_bytes:
            raise InputLimitError("原始页总大小超过限制")
        pages.append((page_number, path))
        if len(pages) > max_files:
            raise InputLimitError("原始页文件数超过限制")
    if not pages:
        raise InputLimitError("没有找到 *_page数字.txt 文件")
    return sorted(pages)


def parse_corpus(
    pages: list[tuple[int, Path]],
    max_chapter: int,
    max_questions: int,
    max_field_chars: int,
    stats: dict[str, Any] | None = None,
    group_by: str = "chapter",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    questions: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicates = 0
    stats = stats if stats is not None else {}
    stats.setdefault("pages", len(pages))
    stats.setdefault("blocks", 0)
    stats.setdefault("raw_answer_blocks", 0)
    stats.setdefault("judge_normalized", 0)
    theme_order: dict[str, int] = {}
    for page_number, path in pages:
        content = normalize_text(path.read_text(encoding="utf-8"))
        for block_number, block in enumerate(split_blocks(content), 1):
            stats["blocks"] += 1
            if _field(block, "标准答案"):
                stats["raw_answer_blocks"] += 1
            question, reasons = parse_block(block, max_chapter, max_field_chars)
            if not question:
                if reasons != ["页面噪声"]:
                    rejected.append({"页": page_number, "块": block_number, "原因": reasons, "片段": block[:240]})
                continue
            theme = question.get("主题") or ""
            if theme and theme not in theme_order:
                theme_order[theme] = len(theme_order)
            question["_theme_order"] = theme_order.get(theme, 10**6)
            question["_source_page"] = page_number
            questions.append(question)
            if len(questions) > max_questions:
                raise InputLimitError("有效题目数超过限制")
    unique: list[dict[str, Any]] = []
    for question in questions:
        key = hashlib.sha256(re.sub(r"\s+", "", question["题目"]).encode("utf-8")).hexdigest()
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        unique.append(question)
    questions = unique

    order = list(range(1, max_chapter + 1)) + [SUPPLEMENT_CHAPTER]
    rank = {chapter: index for index, chapter in enumerate(order)}
    if group_by == "theme":
        questions.sort(key=lambda item: (item.get("_theme_order", 10**6), item["_source_page"], item["_source_number"]))
    else:
        questions.sort(
            key=lambda item: (
                rank.get(item["_chapter"], len(order)),
                item["_sub"],
                item["_source_page"],
                item["_source_number"],
            )
        )
    for number, question in enumerate(questions, 1):
        question["_number"] = number
    return questions, rejected, duplicates


def markdown(value: Any) -> str:
    text = html.escape(str(value or ""), quote=False)
    return re.sub(r"([\\`*_{}\[\]])", r"\\\1", text)


def chapter_label(chapter: int, chapter_names: dict[str, str]) -> str:
    if chapter == SUPPLEMENT_CHAPTER:
        return "补充材料"
    return chapter_names.get(str(chapter), f"第{chapter}章")


def theme_label(question: dict[str, Any]) -> str:
    return question.get("主题") or "未分类主题"


def render_questions(
    course: str,
    questions: list[dict[str, Any]],
    chapter_names: dict[str, str],
    group_by: str = "chapter",
) -> str:
    lines = [f"# {markdown(course)}题库", "", f"> 共 {len(questions)} 道通过完整性校验的题目。", ""]
    current = None
    for question in questions:
        if group_by == "theme":
            group = theme_label(question)
            if group != current:
                current = group
                lines.extend([f"## 主题：{markdown(group)}", ""])
        else:
            chapter = question["_chapter"]
            if chapter != current:
                current = chapter
                lines.extend([f"## {markdown(chapter_label(chapter, chapter_names))}", ""])
        lines.extend([f"### 题 {question['_number']}", "", markdown(question["题目"]), ""])
        for letter, value in question["选项"].items():
            lines.append(f"- {letter}. {markdown(value)}")
        lines.extend(["", f"**正确答案：** {markdown(question['标准答案'])}", ""])
        qtype = question.get("题型") or ""
        if qtype:
            note = "（A=正确，B=错误）" if qtype == "判断题" else ""
            lines.extend([f"**题型：** {markdown(qtype)}{note}", ""])
        if question["知识点"]:
            lines.extend([f"**知识点：** {markdown(question['知识点'])}", ""])
        if question["解析"]:
            lines.extend([f"**解析：** {markdown(question['解析'])}", ""])
        lines.extend([f"**来源：** 第 {question['_source_page']} 页，原题号 {question['_source_number']}", "", "---", ""])
    return "\n".join(lines).rstrip() + "\n"


def render_knowledge(
    course: str,
    questions: list[dict[str, Any]],
    chapter_names: dict[str, str],
    group_by: str = "chapter",
) -> str:
    lines = [f"# {markdown(course)}知识体系", "", "> 本文仅由题库中的知识点和解析归纳，所有条目保留题号来源。", ""]
    if group_by == "theme":
        by_theme: OrderedDict[str, OrderedDict[str, dict[str, Any]]] = OrderedDict()
        for question in questions:
            theme = theme_label(question)
            by_theme.setdefault(theme, OrderedDict())
            knowledge = question["知识点"].strip()
            if not knowledge:
                continue
            point = by_theme[theme].setdefault(knowledge, {"sources": [], "analyses": [], "seen": set()})
            point["sources"].append(question["_number"])
            analysis = question["解析"].strip()
            if analysis and analysis not in point["seen"]:
                point["seen"].add(analysis)
                point["analyses"].append(analysis)
        for theme, points in by_theme.items():
            lines.extend([f"## 主题：{markdown(theme)}", ""])
            if not points:
                lines.extend(["- 待补充：本主题题目未提供知识点。", ""])
                continue
            for knowledge, point in points.items():
                sources = "、".join(f"题 {number}" for number in point["sources"])
                lines.extend([f"### {markdown(knowledge)}", "", f"**来源：** {sources}", ""])
                if point["analyses"]:
                    lines.append("**题库解析要点：**")
                    lines.append("")
                    lines.extend(f"- {markdown(value)}" for value in point["analyses"])
                else:
                    lines.append("- 待补充：题库未提供解析。")
                lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    grouped: dict[int, OrderedDict[str, dict[str, Any]]] = defaultdict(OrderedDict)
    for question in questions:
        knowledge = question["知识点"].strip()
        if not knowledge:
            continue
        point = grouped[question["_chapter"]].setdefault(knowledge, {"sources": [], "analyses": [], "seen": set()})
        point["sources"].append(question["_number"])
        analysis = question["解析"].strip()
        if analysis and analysis not in point["seen"]:
            point["seen"].add(analysis)
            point["analyses"].append(analysis)
    for chapter in sorted(grouped, key=lambda value: (value == SUPPLEMENT_CHAPTER, value)):
        lines.extend([f"## {markdown(chapter_label(chapter, chapter_names))}", ""])
        for knowledge, point in grouped[chapter].items():
            sources = "、".join(f"题 {number}" for number in point["sources"])
            lines.extend([f"### {markdown(knowledge)}", "", f"**来源：** {sources}", ""])
            if point["analyses"]:
                lines.append("**题库解析要点：**")
                lines.append("")
                lines.extend(f"- {markdown(value)}" for value in point["analyses"])
            else:
                lines.append("- 待补充：题库未提供解析。")
            lines.append("")
    if not grouped:
        lines.extend(["## 待整理", "", "题库中没有可识别的知识点。", ""])
    return "\n".join(lines).rstrip() + "\n"


def render_quality(
    course: str,
    pages: list[tuple[int, Path]],
    questions: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    duplicates: int,
    chapter_names: dict[str, str],
    group_by: str = "chapter",
    stats: dict[str, Any] | None = None,
    on_no_answers: str = "stop",
) -> str:
    chapter_counts = Counter(question["_chapter"] for question in questions)
    type_counts = Counter((question.get("题型") or "未标注") for question in questions)
    option_counts = Counter(len(question["选项"]) for question in questions)
    missing_knowledge = sum(not question["知识点"] for question in questions)
    missing_analysis = sum(not question["解析"] for question in questions)
    judge_normalized = sum(1 for question in questions if question.get("_judge_normalized"))
    missing_answer_isolated = sum(1 for item in rejected if any("缺少标准答案" in reason for reason in item["原因"]))
    stats = stats or {}
    lines = [
        f"# {markdown(course)}质量报告",
        "",
        "## 汇总",
        "",
        f"- 原始页文件：{len(pages)}",
        f"- 有效题目：{len(questions)}",
        f"- 重复题目：{duplicates}",
        f"- 隔离块：{len(rejected)}",
        f"- 缺少知识点：{missing_knowledge}",
        f"- 缺少解析：{missing_analysis}",
        f"- 原始题目块：{stats.get('blocks', '未统计')}",
        f"- 含“标准答案”字段的块：{stats.get('raw_answer_blocks', '未统计')}",
        "",
        "## 章节分布",
        "",
    ]
    for chapter in sorted(chapter_counts, key=lambda value: (value == SUPPLEMENT_CHAPTER, value)):
        lines.append(f"- {markdown(chapter_label(chapter, chapter_names))}：{chapter_counts[chapter]} 题")
    lines.extend(["", "## 题型分布", ""])
    if type_counts:
        for qtype, count in type_counts.most_common():
            lines.append(f"- {markdown(qtype)}：{count} 题")
    else:
        lines.append("没有可统计的题型。")
    lines.extend(["", "## 选项数分布", ""])
    if option_counts:
        for count in sorted(option_counts):
            lines.append(f"- {count} 个选项：{option_counts[count]} 题")
    else:
        lines.append("没有可统计的选项。")
    lines.extend(
        [
            "",
            "## 答案处理统计",
            "",
            "- 答案字母范围：A–H；答案按完整字符串解析，**任何字母不在选项中即隔离，不做截断**。",
            f"- 判断题自动归一化（A=正确，B=错误）：{judge_normalized} 题",
            f"- 因缺少标准答案被隔离：{missing_answer_isolated} 题",
            f"- 无标准答案时任务策略：{on_no_answers}（stop=停止任务并通知用户，isolate=仅隔离继续）",
            "",
        ]
    )
    if group_by == "theme" or any(question.get("主题") for question in questions):
        theme_counts = Counter(theme_label(question) for question in questions)
        lines.extend(["## 主题分布", ""])
        for theme, count in theme_counts.items():
            lines.append(f"- {markdown(theme)}：{count} 题")
        lines.append("")
    lines.extend(["## 隔离详情", ""])
    if not rejected:
        lines.append("没有因完整性错误被隔离的题目块。")
    else:
        for item in rejected:
            reasons = "；".join(item["原因"])
            excerpt = re.sub(r"\s+", " ", item["片段"]).strip()
            lines.extend(
                [
                    f"### 第 {item['页']} 页 / 块 {item['块']}",
                    "",
                    f"- 原因：{markdown(reasons)}",
                    f"- 片段：{markdown(excerpt)}",
                    "",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def safe_stem(course: str) -> str:
    value = unicodedata.normalize("NFKC", course).strip()
    value = re.sub(r"[\x00-\x1f/\\:]+", "_", value)
    value = re.sub(r"\s+", "_", value)
    while ".." in value:
        value = value.replace("..", "_")
    value = value.strip("._")[:80]
    return value or "课程"


def prepare_outputs(output_dir: Path, filenames: list[str], overwrite: bool) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise ValueError("输出目录必须是非符号链接目录")
    root = output_dir.resolve()
    paths: list[Path] = []
    for filename in filenames:
        path = output_dir / filename
        resolved = path.resolve(strict=False)
        if resolved.parent != root:
            raise ValueError("输出路径超出指定目录")
        if path.is_symlink():
            raise ValueError(f"拒绝写入符号链接：{filename}")
        if path.exists() and not overwrite:
            raise FileExistsError(f"输出已存在；如确认覆盖请传 --overwrite：{filename}")
        paths.append(path)
    return paths


def atomic_write(path: Path, content: str) -> None:
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def parse_chapters(raw: str, max_field_chars: int) -> dict[str, str]:
    value = json.loads(raw)
    if not isinstance(value, dict) or any(not isinstance(key, str) or not isinstance(name, str) for key, name in value.items()):
        raise ValueError("--chapters 必须是字符串到字符串的 JSON 对象")
    if any(len(name) > max_field_chars for name in value.values()):
        raise ValueError("章节名超过字段长度限制")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 Markdown 题库、知识体系和质量报告")
    parser.add_argument("--input", required=True, type=Path, help="包含 *_page数字.txt 的目录")
    parser.add_argument("--output-dir", required=True, type=Path, help="固定输出三个 Markdown 文件的目录")
    parser.add_argument("--course", required=True, help="课程名称")
    parser.add_argument("--max-chapter", type=int, default=10)
    parser.add_argument("--chapters", default="{}", help="章节名映射 JSON")
    parser.add_argument("--group-by", choices=["chapter", "theme"], default="chapter",
                        help="分组方式：chapter=按章节/知识点（默认），theme=按页面中的“主题：”字段")
    parser.add_argument("--on-no-answers", choices=["stop", "isolate"], default="stop",
                        help="当题库没有任何标准答案时：stop=停止任务并通知用户（默认），isolate=仅隔离并继续")
    parser.add_argument("--max-files", type=int, default=500)
    parser.add_argument("--max-file-bytes", type=int, default=2 * 1024 * 1024)
    parser.add_argument("--max-total-bytes", type=int, default=200 * 1024 * 1024)
    parser.add_argument("--max-questions", type=int, default=10_000)
    parser.add_argument("--max-field-chars", type=int, default=20_000)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    if not 1 <= args.max_chapter <= 98:
        parser.error("--max-chapter 必须在 1 到 98 之间")
    for name in ("max_files", "max_file_bytes", "max_total_bytes", "max_questions", "max_field_chars"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} 必须大于 0")
    if len(args.course) > args.max_field_chars:
        parser.error("课程名超过字段长度限制")

    chapters = parse_chapters(args.chapters, args.max_field_chars)
    pages = discover_pages(args.input, args.max_files, args.max_file_bytes, args.max_total_bytes)
    stats: dict[str, Any] = {}
    questions, rejected, duplicates = parse_corpus(
        pages, args.max_chapter, args.max_questions, args.max_field_chars, stats=stats, group_by=args.group_by
    )

    # Hard stop: the bank provides no standard answer at all -> stop and report, never self-answer.
    if stats.get("blocks", 0) > 0 and stats.get("raw_answer_blocks", 0) == 0 and args.on_no_answers == "stop":
        message = {
            "stopped": True,
            "reason": "题库未提供标准答案",
            "detail": "解析到的“标准答案”字段数量为 0。任务已停止，不会自行作答或推断答案。请通知用户并确认下一步动作（提供答案来源 / 仅整理题干 / 终止任务）。",
            "pages": len(pages),
            "blocks": stats.get("blocks", 0),
            "raw_answer_blocks": 0,
        }
        print(json.dumps(message, ensure_ascii=False))
        raise SystemExit(2)

    stem = safe_stem(args.course)
    paths = prepare_outputs(
        args.output_dir,
        [f"{stem}_题库.md", f"{stem}_知识体系.md", f"{stem}_质量报告.md"],
        args.overwrite,
    )
    contents = [
        render_questions(args.course, questions, chapters, group_by=args.group_by),
        render_knowledge(args.course, questions, chapters, group_by=args.group_by),
        render_quality(
            args.course, pages, questions, rejected, duplicates, chapters,
            group_by=args.group_by, stats=stats, on_no_answers=args.on_no_answers,
        ),
    ]
    for path, content in zip(paths, contents):
        atomic_write(path, content)
    print(
        json.dumps(
            {
                "pages": len(pages),
                "questions": len(questions),
                "duplicates": duplicates,
                "rejected": len(rejected),
                "group_by": args.group_by,
                "types": dict(Counter((question.get("题型") or "未标注") for question in questions)),
                "option_counts": dict(sorted(Counter(len(question["选项"]) for question in questions).items())),
                "judge_normalized": sum(1 for question in questions if question.get("_judge_normalized")),
                "outputs": [str(path) for path in paths],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
