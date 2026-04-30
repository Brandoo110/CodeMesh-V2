"""
检索器：query → 相关代码片段
==============================

【流程】
  用户查询 → embedding → ChromaDB topK 查询 → 返回带路径/行号的片段

【检索结果用途】
由 harness 在任务开始前调用，把 topK 结果拼成一段 context 塞进 system prompt:

    <CODEBASE CONTEXT>
    === src/auth.py:12-48 ===
    def login(...): ...
    === tests/test_auth.py:5-30 ===
    def test_login_ok(...): ...
    </CODEBASE CONTEXT>

模型看到后就不用瞎猜文件在哪。

【面试点】
"Q: Hybrid search 做不做？"
→ 纯向量检索对精确匹配（函数名、错误信息）不如关键词 BM25。
  生产做法是 vector + BM25 融合，Reciprocal Rank Fusion 加权打分。
  CodeMesh MVP 没做，是故意的工程简化，可以作为扩展方向。

"Q: topK 选多少？"
→ 太少漏信息，太多 context 膨胀。实践：5–10 起步，按任务复杂度调。
  真正讲究的做法是根据 token budget 动态截断（比如 context 最多占 2k token）。
"""

from pathlib import Path
from typing import NamedTuple

from .embedder import embed_texts
from .indexer import DEFAULT_DB_DIR


class Hit(NamedTuple):
    """一条检索结果。"""
    path: str
    start_line: int
    end_line: int
    text: str
    score: float   # 距离（越小越相似）


async def retrieve(
    query: str,
    top_k: int = 5,
    db_dir: Path = DEFAULT_DB_DIR,
    collection_name: str = "codebase",
) -> list[Hit]:
    """查询 topK 相关代码片段。没有索引则返回空列表。"""
    try:
        import chromadb
    except ImportError:
        return []

    if not db_dir.exists():
        return []

    client = chromadb.PersistentClient(path=str(db_dir))
    try:
        coll = client.get_collection(collection_name)
    except Exception:
        return []

    # 把查询也做 embedding
    q_vec = (await embed_texts([query]))[0]

    raw = coll.query(
        query_embeddings=[q_vec],
        n_results=top_k,
        include=["documents", "metadatas", "distances"],
    )

    hits: list[Hit] = []
    # ChromaDB 返回的结构是 {ids: [[...]], documents: [[...]], ...}
    # 外层是 batch（我们只查了 1 条），内层才是 topK
    docs = raw.get("documents", [[]])[0]
    metas = raw.get("metadatas", [[]])[0]
    dists = raw.get("distances", [[]])[0]

    for doc, meta, dist in zip(docs, metas, dists):
        hits.append(Hit(
            path=str(meta.get("path", "")),
            start_line=int(meta.get("start_line", 0)),
            end_line=int(meta.get("end_line", 0)),
            text=doc,
            score=float(dist),
        ))
    return hits


def format_context(hits: list[Hit], max_chars: int = 4000) -> str:
    """
    把检索结果拼成给模型看的字符串。
    max_chars 防止 context 爆炸（一个中文字 = 1 char，~4000 char ≈ 2k token）。
    """
    if not hits:
        return ""
    buf: list[str] = ["<CODEBASE CONTEXT>"]
    remaining = max_chars
    for h in hits:
        header = f"=== {h.path}:{h.start_line}-{h.end_line} ==="
        piece = f"{header}\n{h.text}\n"
        if len(piece) > remaining:
            # 最后一条允许部分拼上，其余停止
            buf.append(piece[:remaining])
            break
        buf.append(piece)
        remaining -= len(piece)
    buf.append("</CODEBASE CONTEXT>")
    return "\n".join(buf)
