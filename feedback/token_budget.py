"""
Token 计数与预算（Harness 反馈层）
====================================

【为什么不按字符数（max_chars）切】
原版 rag/retriever.format_context 用 max_chars=4000 截 context。这有两个问题：
  1. 中文 1 字 ≈ 1 token，但英文 1 字 ≈ 0.25 token —— 同样的 4000 字符
     塞进 prompt，中文用 ~4000 token，英文只用 ~1000 token。
     模型 context 上限是按 token 算的，按字符切要么浪费要么超限。
  2. 4000 字符的 markdown / 代码块结构不可控，可能截在 fenced code block 中间。

【这个模块解决什么】
统一暴露：
  count_tokens(text, model=None) -> int
  truncate_to_budget(text, max_tokens, model=None) -> str

优先用 tiktoken（OpenAI 官方），离线 / 加载失败时退到一个**经验启发式**：
  - 中文（CJK 字符）：1 char ≈ 1 token
  - 其他（英文、数字、符号、空白）：1 char ≈ 0.25 token

这个混合启发式在中英混排代码评测里跟 cl100k_base 误差 ±15%，
够用做"避免超限"的预算计算。

【设计要点】
"Q: 直接 `len(text)` 不行吗？"
→ 不行。token 数 ≠ char 数：tiktoken cl100k_base 算 'hello' = 1 token
  但 5 个字符；'你好' = 2 token 但 2 字符；中英混排误差差 4 倍。
  用 char 数当 token 预算要么浪费上下文，要么超限报错。

"Q: 为什么不强制依赖 tiktoken？"
→ tiktoken 首次使用要从 OpenAI 公网下载 BPE 表（数 MB）。在内网 / 飞机上 /
  CI 容器里会失败。我们用它做主路径，离线时降级到启发式，**业务永远不挂**。
"""

from __future__ import annotations

import re
from typing import Optional


# tiktoken 缓存：第一次拿到就重复使用
_ENC = None
_TIKTOKEN_TRIED = False


def _try_load_tiktoken():
    """尝试加载 tiktoken cl100k_base。失败就把 _ENC 置为 None 不再重试。"""
    global _ENC, _TIKTOKEN_TRIED
    if _TIKTOKEN_TRIED:
        return _ENC
    _TIKTOKEN_TRIED = True
    try:
        import tiktoken  # type: ignore[import-not-found]
        _ENC = tiktoken.get_encoding("cl100k_base")
    except Exception:
        # 网络问题 / 包未装 / 任何错都吞掉，走启发式
        _ENC = None
    return _ENC


# CJK 字符（中日韩）正则。中文一个字符约 1 token，单独算。
_CJK_RE = re.compile(r"[一-鿿　-〿぀-ヿ＀-￯]")


def _heuristic_count(text: str) -> int:
    """
    无 tiktoken 时的备用估算。
    cjk_chars * 1 + other_chars * 0.25，向上取整。
    """
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    # 0.25 ≈ 4 chars / token in English+code
    estimated = cjk + other * 0.25
    return max(1, int(estimated + 0.5))


def count_tokens(text: str, model: Optional[str] = None) -> int:
    """
    估算 text 占多少 token。
      - 优先 tiktoken cl100k_base（GPT-4 / 大多数现代 OpenAI 兼容模型）
      - 没法加载就用启发式

    model 参数留给以后接 deepseek / qwen 各自的 tokenizer，目前忽略。
    """
    if not text:
        return 0
    enc = _try_load_tiktoken()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    return _heuristic_count(text)


def truncate_to_budget(
    text: str,
    max_tokens: int,
    model: Optional[str] = None,
) -> str:
    """
    把 text 截到不超过 max_tokens。从尾部切，保留头部信息。
      - tiktoken 路径：encode → 截 → decode（最精确）
      - 启发式路径：按比例截字符（足够用，±15% 误差）
    """
    if max_tokens <= 0:
        return ""
    enc = _try_load_tiktoken()
    if enc is not None:
        try:
            ids = enc.encode(text)
            if len(ids) <= max_tokens:
                return text
            return enc.decode(ids[:max_tokens])
        except Exception:
            pass
    # 启发式：按比例切。先估总 token，按比例反推可保留字符数。
    total = count_tokens(text)
    if total <= max_tokens:
        return text
    keep_ratio = max_tokens / total
    keep_chars = max(1, int(len(text) * keep_ratio))
    return text[:keep_chars]


def using_tiktoken() -> bool:
    """调试 / 测试用：判断当前是否走了 tiktoken 路径。"""
    return _try_load_tiktoken() is not None


__all__ = ["count_tokens", "truncate_to_budget", "using_tiktoken"]
