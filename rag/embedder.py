"""
Embedding：把文本变成向量
============================

【Embedding 是什么】
把一段文本映射成一个固定维度的数字向量（比如 1536 维的 float list）。
相似含义的文本在向量空间里距离近，这就是"语义搜索"的基础。

【选型：用谁家的 embedding】
国内合规场景下可选：
  - DashScope text-embedding-v3  （阿里，1024 维）
  - 智谱 embedding-3             （1024 维）
  - 豆包 doubao-embedding        （2560 维）

CodeMesh 选 DashScope，因为 DashScope key 已经配了，少一套密钥。

【为什么不用本地模型】
比如 bge-m3 / nomic-embed-text，跑本地不花钱。
但：1) 依赖 sentence-transformers 和 PyTorch，安装体积大
    2) 第一次加载慢，M1 Mac 没 GPU 更慢
选 API 方案是"工程性价比"的选择。生产可以切本地。

【批量】
embed API 支持一次传多段，成本远低于逐条调。
DashScope 单次限 25 条，这里我们分批处理。
"""

import os
from typing import List

from openai import AsyncOpenAI


# DashScope embedding 端点（OpenAI 兼容）
_EMB_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_EMB_MODEL = "text-embedding-v3"
_BATCH_SIZE = 25


_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=os.getenv("DASHSCOPE_API_KEY", ""),
            base_url=_EMB_BASE_URL,
        )
    return _client


async def embed_texts(texts: List[str]) -> List[List[float]]:
    """
    批量把文本转向量。返回顺序和输入一致。
    空字符串会被替换成 " "（DashScope 拒绝空输入）。
    """
    client = _get_client()
    # 预处理：空字符串替换
    inputs = [t if t.strip() else " " for t in texts]

    vectors: list[list[float]] = []
    for i in range(0, len(inputs), _BATCH_SIZE):
        batch = inputs[i : i + _BATCH_SIZE]
        resp = await client.embeddings.create(model=_EMB_MODEL, input=batch)
        # resp.data 已按输入顺序返回
        for d in resp.data:
            vectors.append(d.embedding)
    return vectors
