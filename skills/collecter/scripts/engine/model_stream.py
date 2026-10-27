# -*- coding: utf-8 -*-
"""解析模型公开的流式文本及思考摘要，未完成的输出不能作为最终总结。"""

import json
from collections import deque

from model_settings import FINISH_REASONS, ModelError

# 限制单个事件和实际保留的正文，心跳、思考与协议开销不累计限制整条流。
MAX_LINE = 4 * 1024 * 1024
MAX_EVENT = 8 * 1024 * 1024
MAX_TEXT = 16 * 1024 * 1024


class StreamResult:
    """只把已完整结束的正文交给调用方，不保留思考内容。"""

    def __init__(self, text):
        self.text = text


class DeltaFilter:
    """保留密钥可能跨片段的尾部，防止逐字输出绕过密钥遮蔽。"""

    def __init__(self, key, callback):
        self.key, self.callback = key, callback
        self.pending = deque()
        self.length = 0

    def take(self, count, display=True):
        """按原通道顺序释放前缀，跨通道拼接的密钥也不能绕过遮蔽。"""
        while count:
            kind, value = self.pending.popleft()
            amount = min(count, len(value))
            if display:
                self.callback(kind, value[:amount])
            if amount < len(value):
                self.pending.appendleft((kind, value[amount:]))
            self.length -= amount
            count -= amount

    def emit(self, kind, text):
        """各公开通道独立缓冲，只向界面转交已经遮蔽的文字。"""
        if not isinstance(text, str):
            raise ModelError("RESPONSE")
        if not text:
            return
        if not self.key:
            self.callback(kind, text)
            return
        self.pending.append((kind, text))
        self.length += len(text)
        while True:
            value = "".join(part for _, part in self.pending)
            position = value.find(self.key)
            if position < 0:
                break
            self.take(position)
            channel = self.pending[0][0]
            self.take(len(self.key), display=False)
            self.callback(channel, "[已隐藏密钥]")
        # 只保留仍可能成为密钥的后缀，普通文本首片段无需等待整个密钥长度。
        keep = min(len(self.key) - 1, self.length)
        while keep and not value.endswith(self.key[:keep]):
            keep -= 1
        self.take(self.length - keep)

    def finish(self):
        """成功结束才释放待显示尾部，失败时丢弃残留，避免误示完整输出。"""
        self.take(self.length)


def stream_events(response, protocol):
    """逐行读取SSE或换行JSON，限制行和总字节，兼容心跳及多行事件。"""
    event_size, data = 0, []
    while True:
        line = response.readline(MAX_LINE + 1)
        if len(line) > MAX_LINE:
            raise ModelError("RESPONSE_SIZE", {"stage": "stream_line", "limit_bytes": MAX_LINE})
        if not line:
            if data:
                yield "\n".join(data)
            return
        try:
            value = line.decode("utf-8").rstrip("\r\n")
        except UnicodeError:
            raise ModelError("RESPONSE", {"stage": "stream_encoding"}) from None
        if protocol == "ollama":
            if value.strip():
                yield value
        elif not value:
            if data:
                yield "\n".join(data)
                data, event_size = [], 0
        elif value.startswith("data:"):
            event_size += len(line)
            if event_size > MAX_EVENT:
                raise ModelError("RESPONSE_SIZE", {"stage": "stream_event", "limit_bytes": MAX_EVENT})
            data.append(value[5:].lstrip(" "))


def read_model_stream(response, protocol, callback, key):
    """只识别协议声明的公开文本字段，不执行工具或显示签名、加密推理等字段。"""
    output, reasoning_chars, text_chars, text_bytes, finished, reason = [], 0, 0, 0, False, None
    stage, event_index, end_marker = "stream_json", 0, "eof"
    display = DeltaFilter(key, callback)

    def emit(kind, text):
        """正文用于最终报告，思考仅计数并传给既有回调，不保存原文。"""
        nonlocal reasoning_chars, text_chars, text_bytes
        if not isinstance(text, str):
            fail("RESPONSE")
        if kind == "text":
            text_chars += len(text)
            text_bytes += len(text.encode("utf-8"))
            if text_bytes > MAX_TEXT:
                raise ModelError("RESPONSE_SIZE", {"stage": "text_limit", "limit_bytes": MAX_TEXT, "text_chars": text_chars})
            output.append(text)
        else:
            reasoning_chars += len(text)
        display.emit(kind, text)

    def fail(code):
        """错误附带的诊断只包含受控计数和固定结束原因，不携带响应正文。"""
        safe_reason = reason if isinstance(reason, str) and reason in FINISH_REASONS else "missing" if reason is None else "other"
        raise ModelError(code, {"stage": stage, "event_index": event_index, "text_chars": text_chars, "reasoning_chars": reasoning_chars,
                                "finish_reason": safe_reason, "termination_received": finished, "end_marker": end_marker}) from None

    try:
        for raw in stream_events(response, protocol):
            if raw.strip() == "[DONE]":
                finished = True
                end_marker = "done_marker"
                break
            event_index += 1
            stage = "stream_json"
            event = json.loads(raw)
            stage = "stream_shape"
            if not isinstance(event, dict):
                fail("RESPONSE")
            kind = event.get("type", "")
            if event.get("error") or kind in ("error", "response.failed"):
                fail("HTTP")
            if protocol == "responses":
                if kind == "response.output_text.delta":
                    emit("text", event.get("delta", ""))
                elif kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
                    emit("reasoning", event.get("delta", ""))
                elif kind.startswith("response.refusal."):
                    fail("BLOCKED")
                elif kind == "response.incomplete":
                    reason = event.get("response", {}).get("incomplete_details", {}).get("reason")
                    finished, end_marker = True, "protocol_end"
                    fail("OUTPUT_LIMIT" if reason == "max_output_tokens" else "INCOMPLETE")
                elif kind == "response.completed":
                    finished = True
                    end_marker = "protocol_end"
                    break
            elif protocol == "anthropic":
                if kind == "content_block_start":
                    block = event.get("content_block", {})
                    if block.get("type") == "text":
                        emit("text", block.get("text", ""))
                    elif block.get("type") == "thinking":
                        emit("reasoning", block.get("thinking", ""))
                elif kind == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        emit("text", delta.get("text", ""))
                    elif delta.get("type") == "thinking_delta":
                        emit("reasoning", delta.get("thinking", ""))
                elif kind == "message_delta":
                    reason = event.get("delta", {}).get("stop_reason")
                elif kind == "message_stop":
                    finished = True
                    end_marker = "protocol_end"
                    break
            elif protocol == "gemini":
                if event.get("promptFeedback", {}).get("blockReason"):
                    fail("BLOCKED")
                candidate = (event.get("candidates") or [{}])[0]
                for part in candidate.get("content", {}).get("parts", []):
                    if "text" in part:
                        emit("reasoning" if part.get("thought") else "text", part["text"])
                reason = candidate.get("finishReason")
                if reason:
                    finished = True
                    end_marker = "finish_reason"
                    break
            elif protocol == "ollama":
                message = event.get("message", {})
                emit("reasoning", message.get("thinking", ""))
                emit("text", message.get("content", ""))
                if event.get("done"):
                    reason = event.get("done_reason")
                    finished = True
                    end_marker = "protocol_end"
                    break
            else:
                choice = (event.get("choices") or [{}])[0]
                delta = choice.get("delta", {})
                emit("reasoning", delta.get("reasoning_content") or delta.get("reasoning") or "")
                text = delta.get("content") or ""
                if isinstance(text, list):
                    text = "".join(part.get("text", "") for part in text if part.get("type") == "text")
                emit("text", text)
                reason = choice.get("finish_reason")
                if reason:
                    finished = True
                    end_marker = "finish_reason"
                    break
        stage = "stream_finish"
        if reason == "insufficient_system_resource":
            fail("UPSTREAM_RESOURCE")
        if reason in ("length", "max_tokens", "MAX_TOKENS"):
            fail("OUTPUT_LIMIT")
        if reason in ("content_filter", "refusal", "SAFETY", "RECITATION", "PROHIBITED_CONTENT"):
            fail("BLOCKED")
        if not finished or reason not in (None, "stop", "end_turn", "stop_sequence", "STOP"):
            fail("INCOMPLETE")
        text = "".join(output)
        if not text.strip():
            fail("THINKING_ONLY" if reasoning_chars else "NO_TEXT")
        display.finish()
        return StreamResult(text.replace(key, "[已隐藏密钥]").strip() if key else text.strip())
    except (ValueError, UnicodeError, TypeError, AttributeError, IndexError):
        fail("RESPONSE")
