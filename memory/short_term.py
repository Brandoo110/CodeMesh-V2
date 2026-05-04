"""
短期记忆：对话历史 + 滑动窗口（Harness 记忆层）
====================================================

【为什么需要短期记忆】
大模型本身是无状态的 —— 每次调用都要把全部上下文塞进 messages 里。
Agent 要实现「多轮对话」，就必须自己保存历史消息，下一轮再一起传进去。
这就是「短期记忆」的作用。

【为什么要滑动窗口】
每个模型都有上下文长度上限（比如 32k tokens）。如果无限堆历史，
迟早会超限 API 报错。解决方法有两种：
  1. 滑动窗口：丢弃最旧的消息（最简单，信息可能丢）
  2. 压缩/总结：把老对话交给模型总结成一段（信息保留更好，但成本高）

CodeMesh 这里先用「滑动窗口」：保留最近 N 条。System message 永远保留，
因为它包含角色设定、工具说明等关键指令。

【面试点】
"为什么不按 token 数切而按消息数切？"
→ 消息数切简单、无需 tokenizer 依赖；短任务场景下够用。
  生产系统建议按 token 数切（用 tiktoken 计算），更精确。
  这是「工程简化 vs 精确控制」的取舍，取决于使用场景。

"滑动窗口会丢上下文怎么办？"
→ 可以在丢弃前让模型总结成一条 summary 消息塞进去（叫 "memory compression"）。
  本项目为了教学简洁没做这一步，但可以作为扩展。
"""

from collections import deque
from typing import Awaitable, Callable, Optional


# 压缩 summarizer 的类型：拿一组消息，返回中文摘要字符串
Summarizer = Callable[[list[dict]], Awaitable[str]]


class ShortTermMemory:
    """
    短期记忆：消息队列 + 滑动窗口。

    内部用 deque（双端队列）存非 system 消息，设置 maxlen 后自动丢弃左侧最旧的。
    这是 Python 标准库里实现「固定大小队列」最优雅的方式。
    """

    def __init__(
        self,
        max_messages: int = 20,
        compress_threshold: Optional[int] = None,
        summarizer: Optional[Summarizer] = None,
    ):
        """
        Args:
            max_messages: 最多保留多少条非 system 消息。超过后最旧的会被自动丢弃。
            compress_threshold: 触发记忆压缩的阈值（消息数 >= 该值时 maybe_compress 会触发）。
                                None 表示关闭压缩。
            summarizer: async 函数，接收一组消息返回压缩摘要文本。
                        compress_threshold 不为 None 时必传。
        """
        self._system: dict | None = None
        # deque 的 maxlen 参数：满了再 append 会自动 popleft，O(1) 复杂度
        self._messages: deque[dict] = deque(maxlen=max_messages)
        # ── 记忆压缩相关 ──
        self._compress_threshold = compress_threshold
        self._summarizer = summarizer
        self._summary: str | None = None  # 累积的对话摘要

    def set_system(self, content: str) -> None:
        """设置/更新 system message。它不参与窗口淘汰，永远保留。"""
        self._system = {"role": "system", "content": content}

    def add(self, role: str, content: str) -> None:
        """
        追加一条消息。

        role 约定:
          - "user"      : 用户输入
          - "assistant" : 模型回复
          - "tool"      : 工具调用结果（部分 API 用 "function"）
        """
        self._messages.append({"role": role, "content": content})

    def get_messages(self) -> list[dict]:
        """
        返回当前完整消息列表（system 在最前）。
        上层调适配器时直接把这个结果传进去即可。

        若已有累积 summary，会以 system 消息形式紧跟在原 system 之后注入，
        让模型继续看到被压缩掉的早期对话信息。
        """
        result: list[dict] = []
        if self._system:
            result.append(self._system)
        if self._summary:
            result.append({
                "role": "system",
                "content": f"<previous conversation summary>: {self._summary}",
            })
        result.extend(self._messages)
        return result

    def clear(self) -> None:
        """清空历史（system 保留）。开始新任务时可以调。"""
        self._messages.clear()

    def __len__(self) -> int:
        """当前非 system 消息数量。"""
        return len(self._messages)

    # ─────────────── 记忆压缩 ───────────────

    @property
    def summary(self) -> str | None:
        """当前累积的对话摘要（用于调试 / 单测断言）。"""
        return self._summary

    async def maybe_compress(self) -> bool:
        """
        如果当前消息数 >= compress_threshold 且配置了 summarizer，
        把最旧一半消息交给 summarizer 压缩成 summary，并从 _messages 中移除。

        多次触发时新摘要会与旧摘要合并：保留旧 summary 作为前缀。

        Returns:
            True  : 触发了一次压缩
            False : 阈值未到 / 未配置 summarizer / 没有可压缩的消息
        """
        if self._compress_threshold is None or self._summarizer is None:
            return False
        if len(self._messages) < self._compress_threshold:
            return False
        # 至少要有 2 条才值得压缩
        if len(self._messages) < 2:
            return False

        half = len(self._messages) // 2
        # 注意：用 popleft 而不是切片，保持 O(1) 摊销 + 真正缩短队列
        to_compress = [self._messages.popleft() for _ in range(half)]

        new_summary = await self._summarizer(to_compress)
        if self._summary:
            self._summary = f"{self._summary}\n\n{new_summary}"
        else:
            self._summary = new_summary
        return True
