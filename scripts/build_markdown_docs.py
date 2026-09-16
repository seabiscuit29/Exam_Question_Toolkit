#!/usr/bin/env python3
"""Parse captured question pages and build Markdown-only deliverables."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import tempfile
import unicodedata
from collections import Counter, OrderedDict, defaultdict
from pathlib import Path
from typing import Any


SUPPLEMENT_CHAPTER = 99
JUNK_QUESTIONS = {"0.02", "0.03", "0.04", "0.05", "0.08", "0.2"}
PAGE_PATTERN = re.compile(r"_page(\d+)\.txt$", re.IGNORECASE)
# Option bodies may wrap across lines, so `[\s\S]` is used instead of `.`
# (`.` does not match newlines even under re.S).
#
# The end anchor MUST be \Z, not $: under re.MULTILINE, $ also matches at every
# line end, so a lazy body would stop as soon as the first option line ended and
# any wrapped continuation would be silently dropped.
OPTION_PATTERN = re.compile(
    r"(?m)^\s*([A-D])[\.．、]\s*([\s\S]*?)"
    r"(?=^\s*[A-D][\.．、]\s*|^\s*(?:标准答案|答案解析|知识点|所选答案|答案)[：:]|\Z)"
)


class InputLimitError(ValueError):
    """Raised when a corpus crosses an explicitly configured safety bound."""


def normalize_text(content: str) -> str:
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    content = content.replace("\\n", "\n").strip().strip('"')
    content = re.sub(r"\n上页.*?下页(?:\n|$)", "\n", content, flags=re.DOTALL)
    content = re.sub(r"\(纠错\)\s*\(分析\)|\(纠错\)|\(分析\)", "", content)
    content = re.sub(r"[ \t]+", " ", content)
    content = re.sub(r"\n{3,}", "\n\n", content)
    # Normalize common inline option layouts into one option per line.
    content = re.sub(r"\s+(?=[A-D][\.．、]\s+)", "\n", content)
    content = re.sub(r"(?<!\n)(?=(?:标准答案|答案解析|知识点)[：:])", "\n", content)
    return content.strip()


def normalize_knowledge(value: str) -> str:
    return (
        str(value or "")
        .strip()
        .replace("\u3000", " ")
        .replace("—", "-")
        .replace("．", ".")
    )


def split_blocks(content: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?m)(?=^\s*\d+[\.．、]\s*)", content) if part.strip()]


def extract_chapter(
    knowledge: str,
    max_chapter: int,
    extra_chapters: dict[str, int] | None = None,
    chapter_names: dict[str, int] | None = None,
) -> tuple[int, tuple[int, int, int, int]]:
    """Derive (chapter, sub-parts) from a knowledge-point label.

    Resolution order:
      1. explicit label mapping (un-numbered categories such as 综合知识点)
      2. a leading number, which is authoritative: "11.4.2 《保险法》…" is a
         citation inside chapter 11, NOT supplementary material, so a 书名号 or
         a 补充 elsewhere in the label must not override it
      3. a bare leading number ("7　人身保险产品策略", "6保险公司目标…")
      4. a declared chapter name supplied by the caller, which supports sources
         that label chapters with Chinese numerals ("第八章", "第十四章")
    """
    value = normalize_knowledge(knowledge)
    if not value:
        return SUPPLEMENT_CHAPTER, (0, 0, 0, 0)

    if extra_chapters and value in extra_chapters:
        chapter = extra_chapters[value]
        if 1 <= chapter <= max_chapter:
            return chapter, (chapter, 0, 0, 0)

    match = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?", value)
    if match:
        parts = tuple(int(match.group(index) or 0) for index in range(1, 5))
        if 1 <= parts[0] <= max_chapter:
            return parts[0], parts
        return SUPPLEMENT_CHAPTER, (0, 0, 0, 0)

    bare = re.match(r"^(\d{1,2})(?=[\s\u3000]|[^\d.．、])", value)
    if bare:
        chapter = int(bare.group(1))
        if 1 <= chapter <= max_chapter:
            return chapter, (chapter, 0, 0, 0)
        return SUPPLEMENT_CHAPTER, (0, 0, 0, 0)

    if chapter_names and value in chapter_names:
        chapter = chapter_names[value]
        if 1 <= chapter <= max_chapter:
            return chapter, (chapter, 0, 0, 0)

    return SUPPLEMENT_CHAPTER, (0, 0, 0, 0)


def _field(block: str, label: str) -> str:
    # "答案" is treated as a label so a restated answer inside the analysis
    # ("解析：… 答案：C. …") cannot bleed into the 解析 field as a stray fragment.
    # \Z (not $) terminates the value, because under re.MULTILINE $ also matches
    # at every line end and would cut a wrapped value short.
    match = re.search(
        rf"(?ms)^\s*{label}[：:]\s*(.*?)(?=^\s*(?:知识点|标准答案|答案解析|所选答案|答案)[：:]|\Z)",
        block,
    )
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


LEADING_RESTATED_ANSWER = re.compile(
    r"^\s*答案[：:]\s*([A-Da-d](?:\s*[、,，]?\s*[A-Da-d])*)\s*[.．、)）]?\s*"
)


def strip_restated_answer(analysis: str) -> str:
    """Drop a leading restated answer ("答案：C. …") from an analysis field.

    Some sources inline the correct answer at the start of the analysis, glued
    to the prose without a line break (so the line-oriented label scanning in
    _field cannot see it). That restatement duplicates the 标准答案 field, so it
    is removed; the explanatory prose that follows is preserved verbatim.
    """
    match = LEADING_RESTATED_ANSWER.match(analysis)
    return analysis[match.end():] if match else analysis


def parse_block(
    block: str,
    max_chapter: int,
    max_field_chars: int,
    extra_chapters: dict[str, int] | None = None,
    chapter_names: dict[str, int] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    reasons: list[str] = []
    if "答题范围" in block or block[:20].lstrip().startswith("重新答题"):
        return None, ["页面噪声"]
    number_match = re.match(r"^\s*(\d+)[\.．、]\s*", block)
    if not number_match:
        return None, ["无法识别题号"]

    options = {key: re.sub(r"\s+", " ", value).strip() for key, value in OPTION_PATTERN.findall(block)}
    first_option = re.search(r"(?m)^\s*A[\.．、]\s*", block)
    cut_candidates = [match.start() for match in [first_option] if match]
    for marker in ("知识点", "标准答案", "所选答案", "答案解析"):
        found = re.search(rf"(?m)^\s*{marker}[：:]", block)
        if found:
            cut_candidates.append(found.start())
    question_region = block[: min(cut_candidates)] if cut_candidates else block
    question = re.sub(r"^\s*\d+[\.．、]\s*", "", question_region)
    question = re.sub(r"\s+", " ", question).strip()

    answer_match = re.search(r"标准答案[：:]\s*([A-D]+)", block, re.IGNORECASE)
    answer = "".join(dict.fromkeys((answer_match.group(1).upper() if answer_match else "")))
    knowledge = normalize_knowledge(_field(block, "知识点"))
    analysis = strip_restated_answer(_field(block, "答案解析"))

    if not question or question in JUNK_QUESTIONS:
        reasons.append("题干为空或属于已知噪声")
    if len(options) < 2:
        reasons.append("有效选项少于两个")
    if not answer:
        reasons.append("缺少标准答案")
    elif any(letter not in options for letter in answer):
        reasons.append("答案字母不在已有选项中")

    fields = {"题目": question, "知识点": knowledge, "解析": analysis, **{f"选项{key}": value for key, value in options.items()}}
    oversized = [name for name, value in fields.items() if len(value) > max_field_chars]
    if oversized:
        reasons.append("字段超出长度限制：" + "、".join(oversized))
    if reasons:
        return None, reasons

    chapter, subchapter = extract_chapter(knowledge, max_chapter, extra_chapters, chapter_names)
    return {
        "题目": question,
        "选项": options,
        "标准答案": answer,
        "知识点": knowledge,
        "解析": analysis,
        "_chapter": chapter,
        "_sub": subchapter,
        "_source_number": int(number_match.group(1)),
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
    extra_chapters: dict[str, int] | None = None,
    chapter_names: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    questions: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicates = 0
    for page_number, path in pages:
        content = normalize_text(path.read_text(encoding="utf-8"))
        for block_number, block in enumerate(split_blocks(content), 1):
            question, reasons = parse_block(
                block, max_chapter, max_field_chars, extra_chapters, chapter_names
            )
            if not question:
                if reasons != ["页面噪声"]:
                    rejected.append({"页": page_number, "块": block_number, "原因": reasons, "片段": block[:240]})
                continue
            key = hashlib.sha256(re.sub(r"\s+", "", question["题目"]).encode("utf-8")).hexdigest()
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            question["_source_page"] = page_number
            questions.append(question)
            if len(questions) > max_questions:
                raise InputLimitError("有效题目数超过限制")
    order = list(range(1, max_chapter + 1)) + [SUPPLEMENT_CHAPTER]
    rank = {chapter: index for index, chapter in enumerate(order)}
    questions.sort(key=lambda item: (rank.get(item["_chapter"], len(order)), item["_sub"], item["_source_page"], item["_source_number"]))
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


def render_questions(course: str, questions: list[dict[str, Any]], chapter_names: dict[str, str]) -> str:
    lines = [f"# {markdown(course)}题库", "", f"> 共 {len(questions)} 道通过完整性校验的题目。", ""]
    current = None
    for question in questions:
        chapter = question["_chapter"]
        if chapter != current:
            current = chapter
            lines.extend([f"## {markdown(chapter_label(chapter, chapter_names))}", ""])
        lines.extend([f"### 题 {question['_number']}", "", markdown(question["题目"]), ""])
        for letter, value in question["选项"].items():
            lines.append(f"- {letter}. {markdown(value)}")
        lines.extend(["", f"**正确答案：** {markdown(question['标准答案'])}", ""])
        if question["知识点"]:
            lines.extend([f"**知识点：** {markdown(question['知识点'])}", ""])
        if question["解析"]:
            lines.extend([f"**解析：** {markdown(question['解析'])}", ""])
        lines.extend([f"**来源：** 第 {question['_source_page']} 页，原题号 {question['_source_number']}", "", "---", ""])
    return "\n".join(lines).rstrip() + "\n"


def render_knowledge(course: str, questions: list[dict[str, Any]], chapter_names: dict[str, str]) -> str:
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

    lines = [f"# {markdown(course)}知识体系", "", "> 本文仅由题库中的知识点和解析归纳，所有条目保留题号来源。", ""]
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
) -> str:
    chapter_counts = Counter(question["_chapter"] for question in questions)
    missing_knowledge = sum(not question["知识点"] for question in questions)
    missing_analysis = sum(not question["解析"] for question in questions)
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
        "",
        "## 章节分布",
        "",
    ]
    for chapter in sorted(chapter_counts, key=lambda value: (value == SUPPLEMENT_CHAPTER, value)):
        lines.append(f"- {markdown(chapter_label(chapter, chapter_names))}：{chapter_counts[chapter]} 题")
    lines.extend(["", "## 隔离详情", ""])
    if not rejected:
        lines.append("没有因完整性错误被隔离的题目块。")
    else:
        for item in rejected:
            reasons = "；".join(item["原因"])
            excerpt = re.sub(r"\s+", " ", item["片段"]).strip()
            lines.extend([
                f"### 第 {item['页']} 页 / 块 {item['块']}",
                "",
                f"- 原因：{markdown(reasons)}",
                f"- 片段：{markdown(excerpt)}",
                "",
            ])
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

    # Reverse map of the declared chapter names. This serves two purposes:
    #   * sources that label chapters with Chinese numerals ("第八章") resolve to
    #     their number instead of collapsing into 补充材料;
    #   * un-numbered source labels get a dedicated section when the caller maps
    #     them explicitly, e.g. --chapters '{"12":"综合知识点"}'.
    chapter_names: dict[str, int] = {}
    for key, label in chapters.items():
        index = int(key) if str(key).isdigit() else 0
        if index >= 1 and label not in chapter_names:
            chapter_names[label] = index

    pages = discover_pages(args.input, args.max_files, args.max_file_bytes, args.max_total_bytes)
    questions, rejected, duplicates = parse_corpus(
        pages,
        args.max_chapter,
        args.max_questions,
        args.max_field_chars,
        None,
        chapter_names,
    )
    stem = safe_stem(args.course)
    paths = prepare_outputs(
        args.output_dir,
        [f"{stem}_题库.md", f"{stem}_知识体系.md", f"{stem}_质量报告.md"],
        args.overwrite,
    )
    contents = [
        render_questions(args.course, questions, chapters),
        render_knowledge(args.course, questions, chapters),
        render_quality(args.course, pages, questions, rejected, duplicates, chapters),
    ]
    for path, content in zip(paths, contents):
        atomic_write(path, content)
    print(json.dumps({"pages": len(pages), "questions": len(questions), "duplicates": duplicates, "rejected": len(rejected), "outputs": [str(path) for path in paths]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
