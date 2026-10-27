# -*- coding: utf-8 -*-
"""验证接口结束原因不会被笼统归类，全部使用合成响应和导出。"""

import io
import json
import unittest
from unittest.mock import patch

from analysis_report import generate_report
from model_client import ModelClient
from model_settings import ModelError
from model_stream import read_model_stream
import test_model_summary as existing_tests


def stream(reason=None, terminal=True):
    """构造仅含合成思考的流，不包含任何真实模型或聊天资料。"""
    events = [{"choices": [{"delta": {"reasoning_content": "合成思考"}}]}]
    if terminal:
        events.append({"choices": [{"delta": {}, "finish_reason": reason}]})
    return io.BytesIO("".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events).encode("utf-8"))


class ModelFinishTests(unittest.TestCase):
    """资源不足、中断、无终止信号以及未知枚举必须可以区分。"""

    setUp = existing_tests.ModelSummaryTests.setUp
    tearDown = existing_tests.ModelSummaryTests.tearDown

    def test_upstream_resource_reason_is_preserved(self):
        """上游推理资源不足不能误报为本机响应大小或输出预算超限。"""
        with self.assertRaises(ModelError) as caught:
            read_model_stream(stream("insufficient_system_resource"), "chat", lambda kind, text: None, "")
        error = caught.exception
        self.assertEqual(error.code, "UPSTREAM_RESOURCE")
        self.assertEqual(error.details["finish_reason"], "insufficient_system_resource")
        self.assertIs(error.details["termination_received"], True)
        self.assertEqual(error.details["end_marker"], "finish_reason")
        self.assertEqual(error.details["text_chars"], 0)

    def test_aborted_and_missing_termination_are_distinct(self):
        """明确中断与直接EOF保留不同诊断，两者均不能当成完整输出。"""
        for reason, terminal, expected, received in (("aborted", True, "aborted", True), (None, False, "missing", False)):
            with self.subTest(reason=reason), self.assertRaises(ModelError) as caught:
                read_model_stream(stream(reason, terminal), "chat", lambda kind, text: None, "")
            self.assertEqual(caught.exception.code, "INCOMPLETE")
            self.assertEqual(caught.exception.details["finish_reason"], expected)
            self.assertIs(caught.exception.details["termination_received"], received)

    def test_unknown_finish_reason_remains_private(self):
        """未知结束字符串不原样写入诊断，但保留已收到终止信号这一事实。"""
        with self.assertRaises(ModelError) as caught:
            read_model_stream(stream("PRIVATE_UNTRUSTED_REASON"), "chat", lambda kind, text: None, "")
        self.assertEqual(caught.exception.details["finish_reason"], "other")
        self.assertIs(caught.exception.details["termination_received"], True)
        self.assertNotIn("PRIVATE_UNTRUSTED_REASON", json.dumps(caught.exception.details))

    def test_nonstream_preserves_upstream_reasons_without_retry(self):
        """完整JSON同样区分上游资源不足与中断，不自动再次发送请求。"""
        for reason, code in (("insufficient_system_resource", "UPSTREAM_RESOURCE"), ("aborted", "INCOMPLETE")):
            client = ModelClient(self.config)
            response = {"choices": [{"message": {"content": "", "reasoning_content": "合成思考"}, "finish_reason": reason}]}
            with self.subTest(reason=reason), patch.object(client, "request", return_value=response) as request, self.assertRaises(ModelError) as caught:
                client.generate("合成问题")
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.details["finish_reason"], reason)
            request.assert_called_once()

    def test_report_displays_completed_requests_and_missing_marker(self):
        """两段完成而当日汇总失败时，页面明确显示请求进度和未收到终止信号。"""
        error = ModelError("INCOMPLETE", {"stage": "stream_finish", "finish_reason": "missing", "termination_received": False, "end_marker": "eof"})
        with patch("model_summary.CHUNK_CHARS", 80), patch("model_summary.ModelClient") as client:
            client.return_value.generate.side_effect = ["第一段完整摘要。", "第二段完整摘要。", error]
            root, data = generate_report(self.directory, self.base / "reports", model_config=self.config)
        self.assertEqual(data["content_summary"]["progress"]["completed_requests"], 2)
        self.assertEqual(data["content_summary"]["days"], [])
        page = (root / "分析报告.html").read_text(encoding="utf-8")
        self.assertIn("已完成请求：2", page)
        self.assertIn("实际结束原因", page)
        self.assertIn("未收到", page)


if __name__ == "__main__":
    unittest.main()
