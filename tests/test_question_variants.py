import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "build_markdown_docs.py"
SPEC = importlib.util.spec_from_file_location("build_markdown_docs", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


def parse_page(text, group_by="chapter"):
    with tempfile.TemporaryDirectory() as raw_dir:
        Path(raw_dir, "course_page1.txt").write_text(text, encoding="utf-8")
        pages = MODULE.discover_pages(Path(raw_dir), 10, 100_000, 100_000)
        stats = {}
        questions, rejected, duplicates = MODULE.parse_corpus(
            pages, 20, 100, 10_000, stats=stats, group_by=group_by
        )
    return questions, rejected, duplicates, stats


class JudgeQuestionTests(unittest.TestCase):
    def test_judge_without_options_is_normalized(self):
        questions, rejected, _, _ = parse_page(
            "1. 钢瓶必须固定，以防止倾倒引发安全事故。\n"
            "标准答案：正确\n知识点：实验室安全\n题型：判断题\n主题：气瓶\n"
        )
        self.assertEqual([], rejected)
        self.assertEqual(1, len(questions))
        question = questions[0]
        self.assertEqual("A", question["标准答案"])
        self.assertEqual({"A": "正确", "B": "错误"}, question["选项"])
        self.assertEqual("判断题", question["题型"])
        self.assertTrue(question["_judge_normalized"])

    def test_judge_with_ab_options_maps_letter(self):
        questions, rejected, _, _ = parse_page(
            "1. 可以直接用空的饮料瓶盛装试剂或样品。\n"
            "A. 正确\nB. 错误\n"
            "标准答案：错误\n知识点：实验室安全\n"
        )
        self.assertEqual([], rejected)
        self.assertEqual("B", questions[0]["标准答案"])
        self.assertEqual("B", questions[0]["选项"]["B"] and "B")

    def test_judge_text_variants(self):
        for raw in ("正确", "对", "√", "T"):
            with self.subTest(raw=raw):
                questions, rejected, _, _ = parse_page(
                    f"1. 测试判断。\n标准答案：{raw}\n知识点：实验室安全\n"
                )
                self.assertEqual([], rejected)
                self.assertEqual("A", questions[0]["标准答案"])


class ExtendedOptionTests(unittest.TestCase):
    def test_five_option_multi_choice_keeps_full_answer(self):
        questions, rejected, _, _ = parse_page(
            "1. 关于压力表的使用，哪些说法是正确的？\n"
            "A. 压力表至少每年检定一次\n"
            "B. 压力表至少每半年检定一次\n"
            "C. 买来的新压力表未经检定可以直接使用\n"
            "D. 新安装的压力容器在投入使用前必须先检定压力表\n"
            "E. 不得使用未经检定的压力表\n"
            "标准答案：B,D,E\n知识点：实验室安全\n题型：多选题\n主题：压力容器\n"
        )
        self.assertEqual([], rejected)
        question = questions[0]
        self.assertEqual("BDE", question["标准答案"])
        self.assertEqual(5, len(question["选项"]))
        self.assertIn("E", question["选项"])
        self.assertEqual("多选题", question["题型"])

    def test_six_option_letter_f_is_not_truncated(self):
        questions, rejected, _, _ = parse_page(
            "1. 下列化学试剂哪些是易制毒品：\n"
            "A. 盐酸\nB. 硝酸\nC. 甲苯\nD. 高锰酸钾\nE. 硫酸\nF. 丙酮\n"
            "标准答案：A,C,D,E,F\n知识点：实验室安全\n题型：多选题\n"
        )
        self.assertEqual([], rejected)
        self.assertEqual("ACDEF", questions[0]["标准答案"])
        self.assertEqual(6, len(questions[0]["选项"]))

    def test_answer_letter_missing_from_options_is_quarantined(self):
        questions, rejected, _, _ = parse_page(
            "1. 测试题。\nA. 一\nB. 二\nC. 三\nD. 四\n"
            "标准答案：A,B,E\n知识点：实验室安全\n"
        )
        self.assertEqual([], questions)
        self.assertEqual(1, len(rejected))
        self.assertIn("答案字母不在已有选项中", "；".join(rejected[0]["原因"]))


class ThemeGroupingTests(unittest.TestCase):
    PAGE = (
        "1. 消防题。\nA. 一\nB. 二\n标准答案：A\n知识点：实验室安全\n主题：消防安全\n"
        "\n2. 化学题。\nA. 一\nB. 二\n标准答案：B\n知识点：实验室安全\n主题：化学安全\n"
    )

    def test_theme_field_parsed_and_grouped(self):
        questions, rejected, _, _ = parse_page(self.PAGE, group_by="theme")
        self.assertEqual([], rejected)
        self.assertEqual(["消防安全", "化学安全"], [q["主题"] for q in questions])
        md = MODULE.render_questions("测试课", questions, {}, group_by="theme")
        self.assertIn("## 主题：消防安全", md)
        self.assertIn("## 主题：化学安全", md)
        self.assertNotIn("## 补充材料", md)
        knowledge_md = MODULE.render_knowledge("测试课", questions, {}, group_by="theme")
        self.assertIn("## 主题：消防安全", knowledge_md)


class NoStandardAnswerStopTests(unittest.TestCase):
    def _run(self, page_text, extra_args=()):
        with tempfile.TemporaryDirectory() as raw_dir, tempfile.TemporaryDirectory() as out_dir:
            Path(raw_dir, "course_page1.txt").write_text(page_text, encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable, str(SCRIPT),
                    "--input", raw_dir, "--output-dir", out_dir, "--course", "无答案测试",
                    *extra_args,
                ],
                capture_output=True, encoding="utf-8", errors="replace", timeout=60,
            )
            outputs = sorted(p.name for p in Path(out_dir).iterdir())
            return result, outputs

    def test_no_answers_stops_with_exit_code_2(self):
        result, outputs = self._run("1. 没有答案的题。\nA. 选项一\nB. 选项二\n知识点：实验室安全\n")
        self.assertEqual(2, result.returncode)
        self.assertIn("题库未提供标准答案", result.stdout)
        self.assertEqual([], outputs)

    def test_isolate_mode_continues_when_explicitly_allowed(self):
        result, outputs = self._run(
            "1. 没有答案的题。\nA. 选项一\nB. 选项二\n知识点：实验室安全\n",
            extra_args=("--on-no-answers", "isolate"),
        )
        self.assertEqual(0, result.returncode)
        self.assertIn("质量报告", " ".join(outputs))

    def test_stats_count_raw_answer_blocks(self):
        _, _, _, stats = parse_page("1. 无答案。\nA. 一\nB. 二\n知识点：实验室安全\n")
        self.assertEqual(1, stats["blocks"])
        self.assertEqual(0, stats["raw_answer_blocks"])


if __name__ == "__main__":
    unittest.main()
