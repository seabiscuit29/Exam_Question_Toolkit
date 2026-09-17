import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "build_markdown_docs.py"
SPEC = importlib.util.spec_from_file_location("build_markdown_docs", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


VALID_PAGE = """1. 风险的基本含义是什么？
A. 损失的不确定性
B. 确定的收益
C. 固定成本
D. 已发生损失
标准答案：A
知识点：1.1 风险的定义
答案解析：风险体现为损失发生及其程度的不确定性。

2. 下列哪项属于风险管理措施？
A. 风险识别
B. 忽略风险
标准答案：A
知识点：1.2 风险管理
答案解析：风险识别是风险管理的基础步骤。
"""

INVALID_PAGE = """3. 这道题缺少答案
A. 选项一
B. 选项二
知识点：1.3 完整性
答案解析：应被隔离。
"""


class BuildMarkdownTests(unittest.TestCase):
    def test_valid_questions_render_with_traceable_knowledge(self):
        with tempfile.TemporaryDirectory() as raw_dir, tempfile.TemporaryDirectory() as out_dir:
            page = Path(raw_dir) / "course_page1.txt"
            page.write_text(VALID_PAGE, encoding="utf-8")
            pages = MODULE.discover_pages(Path(raw_dir), 10, 100_000, 100_000)
            questions, rejected, duplicates = MODULE.parse_corpus(pages, 10, 100, 10_000)
            self.assertEqual(2, len(questions))
            self.assertEqual([], rejected)
            self.assertEqual(0, duplicates)
            question_md = MODULE.render_questions("测试课", questions, {"1": "风险基础"})
            knowledge_md = MODULE.render_knowledge("测试课", questions, {"1": "风险基础"})
            self.assertIn("# 测试课题库", question_md)
            self.assertIn("**正确答案：** A", question_md)
            self.assertIn("来源：** 题 1", knowledge_md)

            paths = MODULE.prepare_outputs(Path(out_dir), ["测试课_题库.md"], False)
            MODULE.atomic_write(paths[0], question_md)
            self.assertEqual(question_md, paths[0].read_text(encoding="utf-8"))

    def test_incomplete_question_is_quarantined(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            Path(raw_dir, "course_page1.txt").write_text(INVALID_PAGE, encoding="utf-8")
            pages = MODULE.discover_pages(Path(raw_dir), 10, 100_000, 100_000)
            questions, rejected, _ = MODULE.parse_corpus(pages, 10, 100, 10_000)
            self.assertEqual([], questions)
            self.assertEqual(1, len(rejected))
            self.assertIn("缺少标准答案", rejected[0]["原因"])

    def test_oversized_page_is_rejected_before_read(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            Path(raw_dir, "course_page1.txt").write_text("x" * 101, encoding="utf-8")
            with self.assertRaises(MODULE.InputLimitError):
                MODULE.discover_pages(Path(raw_dir), 10, 100, 1_000)

    def test_existing_output_requires_explicit_overwrite(self):
        with tempfile.TemporaryDirectory() as out_dir:
            existing = Path(out_dir) / "题库.md"
            existing.write_text("keep", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                MODULE.prepare_outputs(Path(out_dir), ["题库.md"], False)
            self.assertEqual("keep", existing.read_text(encoding="utf-8"))

    def test_safe_stem_removes_path_components(self):
        self.assertNotIn("..", MODULE.safe_stem("../../课程/测试"))
        self.assertNotIn("/", MODULE.safe_stem("../../课程/测试"))


class ChapterResolutionRegressionTests(unittest.TestCase):
    """A leading chapter number is authoritative, and Chinese numerals resolve."""

    def test_book_title_citation_inside_a_chapter_is_not_supplementary(self):
        # A 书名号 used to force every such label into 补充材料, mis-filing whole
        # chapters: "11.4.2 《保险法》…" belongs to chapter 11, not the supplement.
        self.assertEqual((11, (11, 4, 2, 0)),
                         MODULE.extract_chapter("11.4.2 《保险法》关于诉讼时效的规定", 13))
        self.assertEqual((10, (10, 3, 2, 0)),
                         MODULE.extract_chapter("10.3.2 《保险法》对死亡保险合同的特殊规制", 13))
        self.assertEqual((5, (5, 2, 1, 3)),
                         MODULE.extract_chapter("5.2.1.3 自杀除外条款-《保险法》自杀除外条款的具体适用", 13))

    def test_numbered_prefix_unchanged_for_plain_labels(self):
        self.assertEqual((1, (1, 1, 1, 1)),
                         MODULE.extract_chapter("1.1.1.1 人身风险与可保人身风险-人身风险", 13))

    def test_un_numbered_labels_still_go_to_supplement(self):
        # Genuinely un-numbered material must keep its old behaviour.
        for label in ("补充资料《保险专业代理机构监管规定》", "综合知识点", "补充材料"):
            with self.subTest(label=label):
                self.assertEqual((MODULE.SUPPLEMENT_CHAPTER, (0, 0, 0, 0)),
                                 MODULE.extract_chapter(label, 13))

    def test_chinese_numeral_chapter_names_resolve(self):
        # Without numeral support these collapse into 补充材料, losing a chapter.
        for label, expected in (("第八章", 8), ("第十二章", 12), ("第十四章", 14), ("第一章", 1)):
            with self.subTest(label=label):
                self.assertEqual((expected, (expected, 0, 0, 0)),
                                 MODULE.extract_chapter(label, 20))

    def test_chinese_numeral_conversion(self):
        self.assertEqual(1, MODULE.chinese_numeral("一"))
        self.assertEqual(10, MODULE.chinese_numeral("十"))
        self.assertEqual(11, MODULE.chinese_numeral("十一"))
        self.assertEqual(14, MODULE.chinese_numeral("十四"))
        self.assertEqual(20, MODULE.chinese_numeral("二十"))
        self.assertEqual(24, MODULE.chinese_numeral("二十四"))
        self.assertIsNone(MODULE.chinese_numeral(""))

    def test_chinese_numeral_beyond_max_chapter_is_supplementary(self):
        self.assertEqual((MODULE.SUPPLEMENT_CHAPTER, (0, 0, 0, 0)),
                         MODULE.extract_chapter("第十四章", 10))

    def test_out_of_range_numbers_stay_supplementary(self):
        self.assertEqual((MODULE.SUPPLEMENT_CHAPTER, (0, 0, 0, 0)),
                         MODULE.extract_chapter("99.1 超范围", 13))
        self.assertEqual((MODULE.SUPPLEMENT_CHAPTER, (0, 0, 0, 0)),
                         MODULE.extract_chapter("", 13))


class RestatedAnswerRegressionTests(unittest.TestCase):
    """A restated answer must not leak into the 解析 field."""

    def _parse_one(self, page_text):
        with tempfile.TemporaryDirectory() as raw_dir:
            Path(raw_dir, "course_page1.txt").write_text(page_text, encoding="utf-8")
            pages = MODULE.discover_pages(Path(raw_dir), 10, 100_000, 100_000)
            questions, rejected, _ = MODULE.parse_corpus(pages, 20, 100, 10_000)
            self.assertEqual([], rejected)
            self.assertEqual(1, len(questions))
            return questions[0]

    def test_inline_restated_answer_is_stripped_and_prose_kept(self):
        question = self._parse_one(
            "1. 甲为其妻投保（ ）\nA. 甲\nB. 乙\nC. 丙\n"
            "标准答案：C\n知识点：1.1 保险利益\n"
            "答案解析：答案：C. 保险金额需征得同意 根据《保险法》第三十四条规定，合同无效。\n"
        )
        self.assertFalse(question["解析"].startswith("答案"))
        self.assertIn("根据《保险法》第三十四条规定，合同无效。", question["解析"])
        self.assertEqual("C", question["标准答案"])

    def test_own_line_restated_answer_is_excluded(self):
        question = self._parse_one(
            "1. 以下说法正确的是（ ）\nA. 甲\nB. 乙\nC. 丙\nD. 丁\n"
            "标准答案：D\n知识点：1.1 服务创新\n"
            "答案解析：营销应从交易性推销转变为关系营销。\n"
            "答案：D．我国健康保险保费增长迅速\n"
        )
        self.assertNotIn("答案", question["解析"])
        self.assertIn("关系营销", question["解析"])

    def test_prose_mentioning_answer_is_not_stripped(self):
        prose = "本题的答案解析表明，正确答案的选择需要结合题干。"
        question = self._parse_one(
            f"1. 以下说法正确的是（ ）\nA. 甲\nB. 乙\n标准答案：A\n"
            f"知识点：1.1 说明\n答案解析：{prose}\n"
        )
        self.assertEqual(prose, question["解析"])

    def test_strip_restated_answer_unit(self):
        self.assertEqual("正文。", MODULE.strip_restated_answer("答案：C. 正文。"))
        self.assertEqual("正文。", MODULE.strip_restated_answer("答案：AB．正文。"))
        self.assertEqual("正文。", MODULE.strip_restated_answer("正文。"))


class OptionBodyRegressionTests(unittest.TestCase):
    """Option bodies that wrap across lines must survive intact."""

    def test_wrapped_option_body_is_preserved(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            Path(raw_dir, "course_page1.txt").write_text(
                "1. 下列关于保险利益的说法正确的是（ ）。\n"
                "A. 保险利益是\n保险合同的有效要件\n"
                "B. 第二项\nC. 第三项\nD. 第四项\n"
                "标准答案：A\n知识点：1.1 保险利益\n答案解析：解析正文。\n",
                encoding="utf-8",
            )
            pages = MODULE.discover_pages(Path(raw_dir), 10, 100_000, 100_000)
            questions, rejected, _ = MODULE.parse_corpus(pages, 20, 100, 10_000)
            self.assertEqual([], rejected)
            options = questions[0]["选项"]
            self.assertEqual(4, len(options))
            self.assertIn("保险合同的有效要件", options["A"])
            self.assertNotIn("标准答案", options["D"])


if __name__ == "__main__":
    unittest.main()

