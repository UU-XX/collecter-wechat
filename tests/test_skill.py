# -*- coding: utf-8 -*-
"""使用合成记录验证独立Skill，禁止访问真实账号和远程模型。"""

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("collecter_skill", ROOT / "skills/collecter/scripts/collecter.py")
skill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(skill)
sys.path.append(str(ROOT / "python/local-collector"))
from workflow_selftest import SyntheticSource
from wechat_source import Conversation
from collector_core import collect, parse_range


class SkillTests(unittest.TestCase):
    """验证导出授权、逐段完整读取、统计离线和固定错误。"""

    def setUp(self):
        """建立不属于任何真实账号的两天记录。"""
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.item = Conversation("synthetic-skill@chatroom", "合成Skill会话", "群聊")
        self.window = parse_range("2025-01-01", "2025-01-02")
        rows = [(1, 101, 1, self.window.start + 1, 1, 0, "member:\n片段哨兵甲" * 1200),
                (2, 102, 1, self.window.start + 86401, 2, 0, "second:\n片段哨兵乙")]
        self.source = SyntheticSource(self.base / "source", [self.item], {self.item.username: rows})
        self.directory, _ = collect(self.source, self.item, self.window, self.base / "exports")

    def tearDown(self):
        """只清理测试自己创建的临时目录。"""
        self.temp.cleanup()

    def test_all_chunks_are_read_without_model_or_source_access(self):
        """多日多段数据无遗漏，已有导出分析不打开微信或调用模型。"""
        before = (self.directory / "records.sqlite3").read_bytes()
        with patch.object(skill, "WechatSource", side_effect=AssertionError("不得读取微信")), patch("model_client.ModelClient.generate", side_effect=AssertionError("不得调用模型")):
            plan = skill.run(skill.parser().parse_args(["plan", "--input", str(self.directory)]))
            text = ""
            for day in plan["days"]:
                for index in range(1, day["chunks"] + 1):
                    result = skill.run(skill.parser().parse_args(["chunk", "--input", str(self.directory), "--day", day["date"], "--index", str(index)]))
                    text += result["text"]
            report = skill.run(skill.parser().parse_args(["report", "--input", str(self.directory), "--output", str(self.base / "reports")]))
        self.assertEqual(text.count("片段哨兵甲"), 1200)
        self.assertEqual(text.count("片段哨兵乙"), 1)
        self.assertEqual(report["total"], 2)
        self.assertFalse(report["model_called"])
        self.assertEqual((self.directory / "records.sqlite3").read_bytes(), before)

    def test_export_uses_core_and_memory_permission_defaults_off(self):
        """模拟读取器接收默认关闭的内存授权，并生成独立完整结果。"""
        with patch.object(skill, "WechatSource", return_value=self.source), patch.object(self.source, "prepare", create=True) as prepare, \
                patch.object(self.source, "conversations", return_value=[self.item], create=True):
            args = skill.parser().parse_args(["export", "--account", str(self.base / "source"), "--conversation", self.item.short_id,
                                             "--start", "2025-01-01", "--end", "2025-01-02", "--output", str(self.base / "skill-exports")])
            result = skill.run(args)
        self.assertFalse(prepare.call_args.kwargs["read_memory"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["exports"]["status"], "complete")
        self.assertNotEqual(Path(result["directory"]), self.directory)

    def test_bad_scope_stops_before_source_access(self):
        """错误日期先失败，不能为了检查输入提前访问账号。"""
        with patch.object(skill, "WechatSource", side_effect=AssertionError("不得读取微信")), self.assertRaises(ValueError):
            skill.run(skill.parser().parse_args(["export", "--account", "synthetic", "--conversation", "synthetic", "--start", "bad", "--end", "2025-01-01", "--output", str(self.base)]))

    def test_invalid_chunk_is_rejected_without_dropping_other_data(self):
        """越界片段不能静默返回空文本，调用者能区分输入错误。"""
        with self.assertRaises(skill.TaskError):
            skill.run(skill.parser().parse_args(["chunk", "--input", str(self.directory), "--day", "2025-01-01", "--index", "999"]))


if __name__ == "__main__":
    unittest.main()
