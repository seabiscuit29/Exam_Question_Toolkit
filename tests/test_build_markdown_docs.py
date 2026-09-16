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


if __name__ == "__main__":
    unittest.main()

