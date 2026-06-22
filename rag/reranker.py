"""
Reranker：用 LLM 当 cross-encoder 重排向量检索结果
=====================================================

【为什么要 reranker】
向量检索（dense）拿出来的 topK 其实是"语义大致相近"，但**不会精确读懂查询意图**。
例如：
  query: "用户登录失败时怎么提示"
  vector topK 可能命中：
    - login_button.tsx (UI 按钮)        ← 实际不相关
    - auth_service.py (认证服务)        ← 真相关
    - error_messages.json (中文错误文案) ← 真相关
    - signup_form.tsx (注册表单)        ← 不相关
    - reset_password.py (重置密码)      ← 半相关

vector 得分把这五个都排进 top5，但**精度差**。
解决方案：用一个更小、更准的模型对 (query, candidate) 做"is this relevant?"判断，
按它的判断重排。

工业最佳实践：
  - 真正的 cross-encoder（BAAI/bge-reranker-v2 等）—— 需要 GPU，麻烦
  - 用 LLM 当 reranker：给 prompt"评分 0-10"，让模型对每个 candidate 打分
    精度跟 cross-encoder 接近，零额外部署，**适合教学 / 小流量场景**

【本模块的设计】
  rerank(query, candidates, k=5, scorer=None)
    candidates: 向量检索拿出来的更多结果（比如 top20）
    scorer: 注入的 async 函数 (query, candidate_text) -> float
            默认 scorer 用 doubao 模型（最便宜）打 0-10 分
    返回 candidates 按分数降序的前 k 个

【设计说明】
"Q: 为什么不用 BAAI 的 reranker 模型？"
→ 它要 GPU、要装 sentence-transformers、首次加载要下载几百 MB 模型。
  对demo 项目来说成本太高。LLM-as-reranker 拿 doubao 一次调 0.001 元，
  20 个 candidate 一次性塞 prompt 几秒搞定，效果接近 cross-encoder。

"Q: 怎么避免 reranker 自己也错？"
→ 设最低 score 阈值（比如 < 3 直接丢）。极端情况下 reranker 全 0 → 回退
  到原 vector 排序，不让它把好结果反而压下去。

【局限】
  - LLM 调用是串行 / 批 prompt，慢于真正的 cross-encoder
  - 对 ~50+ candidates 不划算（应用 listwise rerank prompt 一次搞定）
  - 没装 doubao key 时降级为 no-op（按原顺序返回前 k）
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional

from .retriever import Hit


# scorer 协议: (query, candidate_text) -> float (越大越相关)
Scorer = Callable[[str, str], Awaitable[float]]


# 默认评分 prompt（一次评一条；批量场景见 _llm_default_scorer 注释）
_DEFAULT_PROMPT = (
    "你是一个相关性评分助手。给定查询和一段候选文本，"
    "你需要判断候选对回答查询的相关性，输出 0-10 的整数分（10=完美相关，0=完全不相关）。\n"
    "**只输出一个数字，不解释。**\n\n"
    "查询：{query}\n\n"
    "候选文本：\n{candidate}\n\n"
    "分数（0-10）："
)


async def rerank(
    query: str,
    candidates: list[Hit],
    k: int = 5,
    scorer: Optional[Scorer] = None,
    min_score: float = 0.0,
) -> list[Hit]:
    """
    重排 candidates 后取前 k。
      scorer 缺省用 _llm_default_scorer（调 doubao）。
      没 doubao key 时降级为 no-op：原顺序返回前 k。

    Args:
        query     : 用户原查询
        candidates: 向量检索的更大结果集（如 top20）
        k         : 重排后保留的数量
        scorer    : 自定义评分函数（测试 / 自定义模型时注入）
        min_score : 低于这个分数的结果丢弃（哪怕排进前 k 也丢）

    Returns:
        list[Hit]，按评分降序，长度 ≤ k
    """
    if not candidates:
        return []
    if scorer is None:
        scorer = _llm_default_scorer
        if scorer is None:  # type: ignore[unreachable]
            return candidates[:k]

    scored: list[tuple[float, Hit]] = []
    for hit in candidates:
        try:
            s = await scorer(query, hit.text)
        except Exception:
            # 单个 candidate 评分失败：给一个低但不会被 min_score 过滤掉的中性分数。
            # 让它保留在结果集里但排到尾巴，模型看到上下文还能用，
            # 不会因为 reranker 偶发挂掉直接丢掉好结果。
            s = max(min_score, 0.0)
        scored.append((s, hit))

    scored.sort(key=lambda x: x[0], reverse=True)
    out = [h for s, h in scored if s >= min_score][:k]
    if not out:
        # 全部低于 min_score → 降级到原 vector 排序前 k
        return candidates[:k]
    return out


# ─────────────────────────── 默认 LLM scorer ───────────────────────────

# 全局缓存的 doubao adapter，避免每条 candidate 都重建
_llm_adapter = None


async def _llm_default_scorer(query: str, candidate: str) -> float:
    """
    默认 LLM scorer：用 doubao 给 0-10 分。
    没装 doubao key / 调用失败 → 抛异常（rerank() 捕获后回退）。
    """
    global _llm_adapter
    if _llm_adapter is None:
        # 延迟 import，避免 RAG 模块对 orchestration 形成强依赖
        from orchestration.adapters import VolcEngineAdapter
        _llm_adapter = VolcEngineAdapter()

    # 候选太长会浪费 token，截掉
    if len(candidate) > 1500:
        candidate = candidate[:1500] + "..."

    prompt = _DEFAULT_PROMPT.format(query=query, candidate=candidate)
    text = await _llm_adapter.complete(
        messages=[{"role": "user", "content": prompt}],
        system="你是相关性评分助手。",
    )
    return _parse_score(text)


def _parse_score(text: str) -> float:
    """从模型回复里抽出 0-10 的浮点分数。失败返回 0。"""
    import re
    if not text:
        return 0.0
    m = re.search(r"(\d+(\.\d+)?)", text)
    if not m:
        return 0.0
    try:
        v = float(m.group(1))
    except ValueError:
        return 0.0
    # 夹紧到 [0, 10]
    return max(0.0, min(10.0, v))


__all__ = ["rerank", "Scorer"]
