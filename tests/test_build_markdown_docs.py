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


class OptionIntegrityTests(unittest.TestCase):
    """Regression tests for option bodies that wrap across lines.

    The body group was `.*?` under `(?ms)`, and `.` never matches a newline, so
    only the first line of a wrapped option survived. The end anchor was also
    `$`, which under re.MULTILINE matches at every line end and cut the body
    short as well. Both are covered here.
    """

    WRAPPED_PAGE = """1. 下列关于保险利益的说法正确的是（ ）。
A. 保险利益是
保险合同的有效要件
B. 保险利益只存在于财产保险
C. 保险利益可以转让
D. 保险利益无关紧要
标准答案：A
知识点：1.1 保险利益
答案解析：保险利益是保险合同的有效要件。
"""

    def _parse(self, page_text):
        with tempfile.TemporaryDirectory() as raw_dir:
            Path(raw_dir, "course_page1.txt").write_text(page_text, encoding="utf-8")
            pages = MODULE.discover_pages(Path(raw_dir), 10, 100_000, 100_000)
            return MODULE.parse_corpus(pages, 10, 100, 10_000)

    def test_wrapped_option_body_is_preserved_in_full(self):
        questions, rejected, _ = self._parse(self.WRAPPED_PAGE)
        self.assertEqual([], rejected)
        self.assertEqual(1, len(questions))
        options = questions[0]["选项"]
        self.assertEqual(4, len(options))
        # The continuation line must survive, not just the first line.
        self.assertIn("保险合同的有效要件", options["A"])
        self.assertEqual("保险利益是 保险合同的有效要件", options["A"])

    def test_wrapped_option_does_not_consume_following_labels(self):
        questions, _, _ = self._parse(self.WRAPPED_PAGE)
        options = questions[0]["选项"]
        self.assertNotIn("标准答案", options["D"])
        self.assertEqual("A", questions[0]["标准答案"])
        self.assertEqual("1.1 保险利益", questions[0]["知识点"])

    def test_inline_options_are_still_split(self):
        page = """1. 下列（ ）属于保险合同的解释原则。
A. 文义解释 B. 意图解释 C. 有利于被保险人解释 D. 以上都是
标准答案：D
知识点：1.1 解释原则
答案解析：以上都是解释原则。
"""
        questions, rejected, _ = self._parse(page)
        self.assertEqual([], rejected)
        options = questions[0]["选项"]
        self.assertEqual(4, len(options))
        self.assertEqual("文义解释", options["A"])
        self.assertEqual("以上都是", options["D"])


class RestatedAnswerTests(unittest.TestCase):
    """A restated answer must not leak into the 解析 field as a stray fragment."""

    def _analysis(self, page_text):
        with tempfile.TemporaryDirectory() as raw_dir:
            Path(raw_dir, "course_page1.txt").write_text(page_text, encoding="utf-8")
            pages = MODULE.discover_pages(Path(raw_dir), 10, 100_000, 100_000)
            questions, rejected, _ = MODULE.parse_corpus(pages, 10, 100, 10_000)
            self.assertEqual([], rejected)
            return questions[0]["解析"]

    def test_inline_restated_answer_is_stripped_and_prose_kept(self):
        page = """1. 甲为其妻投保，以下说法正确的是（ ）。
A. 甲
B. 乙
C. 丙
标准答案：C
知识点：1.1 保险利益
答案解析：答案：C. 保险金额需征得同意 根据《保险法》第三十四条规定，合同无效。
"""
        analysis = self._analysis(page)
        self.assertFalse(analysis.startswith("答案"))
        self.assertTrue(analysis.endswith("合同无效。"))
        self.assertIn("根据《保险法》第三十四条规定", analysis)

    def test_own_line_restated_answer_is_excluded(self):
        page = """1. 以下说法正确的是（ ）。
A. 甲
B. 乙
C. 丙
D. 丁
标准答案：D
知识点：1.1 服务创新
答案解析：营销应从交易性推销转变为关系营销。
答案：D．我国健康保险保费增长迅速
"""
        analysis = self._analysis(page)
        self.assertNotIn("答案", analysis)
        self.assertIn("关系营销", analysis)

    def test_prose_mentioning_answer_is_not_stripped(self):
        prose = "本题的答案解析表明，正确答案的选择需要结合题干。"
        page = f"""1. 以下说法正确的是（ ）。
A. 甲
B. 乙
标准答案：A
知识点：1.1 说明
答案解析：{prose}
"""
        self.assertEqual(prose, self._analysis(page))


class ChapterResolutionTests(unittest.TestCase):
    """A leading chapter number is authoritative and overrides citation markers."""

    def test_book_title_citation_inside_a_chapter_is_not_supplementary(self):
        # "《" used to force every such label into 补充材料, mis-filing whole chapters.
        self.assertEqual((11, (11, 4, 2, 0)), MODULE.extract_chapter("11.4.2 《保险法》关于诉讼时效的规定", 13))
        self.assertEqual((10, (10, 3, 2, 0)), MODULE.extract_chapter("10.3.2 《保险法》对死亡保险合同的特殊规制", 13))
        self.assertEqual(
            (5, (5, 2, 1, 3)),
            MODULE.extract_chapter("5.2.1.3 自杀除外条款-《保险法》自杀除外条款的具体适用", 13),
        )

    def test_numbered_prefix_still_wins_over_supplement_keyword(self):
        self.assertEqual((1, (1, 1, 1, 1)), MODULE.extract_chapter("1.1.1.1 人身风险与可保人身风险", 13))

    def test_chinese_numeral_chapter_names_resolve_via_map(self):
        names = {"第八章": 8, "第十四章": 14}
        # Without the map these collapse into 补充材料, losing the whole chapter.
        self.assertEqual((8, (8, 0, 0, 0)), MODULE.extract_chapter("第八章", 20, None, names))
        self.assertEqual((14, (14, 0, 0, 0)), MODULE.extract_chapter("第十四章", 20, None, names))

    def test_unmapped_chinese_numeral_falls_back_to_supplement(self):
        self.assertEqual(
            (MODULE.SUPPLEMENT_CHAPTER, (0, 0, 0, 0)),
            MODULE.extract_chapter("第八章", 20, None, None),
        )

    def test_declared_name_map_routes_unnumbered_categories(self):
        # main() builds this reverse map from --chapters, so an un-numbered label
        # such as 综合知识点 gets its own section instead of collapsing.
        names = {"综合知识点": 12}
        self.assertEqual((12, (12, 0, 0, 0)), MODULE.extract_chapter("综合知识点", 13, None, names))
        self.assertEqual(
            (MODULE.SUPPLEMENT_CHAPTER, (0, 0, 0, 0)),
            MODULE.extract_chapter("补充材料", 20, None, names),
        )

    def test_bare_leading_chapter_number_is_accepted(self):
        # A full-width space or a missing separator used to hide the chapter.
        self.assertEqual((7, (7, 0, 0, 0)), MODULE.extract_chapter("7\u3000人身保险产品策略", 12))
        self.assertEqual((6, (6, 0, 0, 0)), MODULE.extract_chapter("6保险公司目标市场营销战略", 12))

    def test_out_of_range_numbers_stay_supplementary(self):
        for label in ("99.1 超范围", "15 章节"):
            with self.subTest(label=label):
                self.assertEqual(
                    (MODULE.SUPPLEMENT_CHAPTER, (0, 0, 0, 0)),
                    MODULE.extract_chapter(label, 13),
                )

    def test_empty_label_is_supplementary(self):
        self.assertEqual((MODULE.SUPPLEMENT_CHAPTER, (0, 0, 0, 0)), MODULE.extract_chapter("", 13))


if __name__ == "__main__":
    unittest.main()

