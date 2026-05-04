"""
代码库索引器
==============

【流程】
  扫描路径 → 筛代码文件 → 切 chunk → 调 embedding → 存 ChromaDB

【chunk 策略】
代码文件不能整个塞进 embedding（模型限 8k token；塞太大则精度下降）。
常见切分策略：
  1. 按行数切（最简单）：每 N 行一个 chunk，有 overlap 保连贯
  2. 按 AST 切（最精确）：每个函数/类一个 chunk，tree-sitter 解析
  3. 按 markdown 标题切：适合文档

CodeMesh MVP 用（1），每 40 行一个 chunk，重叠 10 行。
生产可以升级到 AST 切分 —— 这是跟面试官展示"我知道怎么做更好"的空间。

【ChromaDB 是什么】
开源的嵌入式向量数据库，pip 装完就用。
  - persistent=True → 本地文件存储（~/.codemesh/rag/）
  - 内置余弦相似度、过滤、metadata 查询

替代品：Qdrant / Milvus / 自建 FAISS。
选 Chroma 是"最小启动成本"，量级不大时足够。

【为什么做 index 用 CLI 命令而不是自动】
索引要调 embedding API，花钱 + 花时间。用户应该显式触发（知情同意）。
codemesh index .  → 明确、可控。
"""

import asyncio
from pathlib import Path
from typing import Iterable

from .embedder import embed_texts
from .ast_chunker import chunk_file_with_fallback


# 索引存储目录
DEFAULT_DB_DIR = Path.home() / ".codemesh" / "rag"
# 代码文件后缀（排除二进制、资源文件）
CODE_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
    ".cpp", ".c", ".h", ".hpp", ".md", ".sql", ".sh", ".yaml", ".yml", ".toml",
}
# 忽略的目录
IGNORED_DIRS = {
    "node_modules", ".git", ".venv", "venv", "__pycache__",
    "dist", "build", "target", ".next", ".cache",
}

# chunk 参数
CHUNK_LINES = 40
CHUNK_OVERLAP = 10


def _iter_code_files(root: Path) -> Iterable[Path]:
    """深度遍历 root，产出所有代码文件路径。"""
    for p in root.rglob("*"):
        # 跳过被忽略的目录下的所有文件
        if any(part in IGNORED_DIRS for part in p.parts):
            continue
        if p.is_file() and p.suffix in CODE_EXTS:
            # 跳过超大文件（> 500KB，通常是生成的）
            try:
                if p.stat().st_size > 500_000:
                    continue
            except OSError:
                continue
            yield p


def _chunk_file(path: Path) -> list[tuple[str, int, int]]:
    """
    把单个文件切成 chunk。返回 [(text, start_line, end_line), ...]。
    start/end 是 1-based，方便给模型展示。

    .py 文件：走 AST chunker（每个 def/class 自成 chunk）
    其他文件：按行滑窗
    """
    code_chunks = chunk_file_with_fallback(
        path, fallback_chunks=CHUNK_LINES, fallback_overlap=CHUNK_OVERLAP,
    )
    return [(c.text, c.start_line, c.end_line) for c in code_chunks]


async def build_index(
    root: Path,
    db_dir: Path = DEFAULT_DB_DIR,
    collection_name: str = "codebase",
    on_progress=None,
) -> int:
    """
    对 root 下的代码建索引。返回索引的 chunk 数量。

    Args:
        root           : 要索引的目录
        db_dir         : ChromaDB 存储路径
        collection_name: 集合名（一个项目一个 collection）
        on_progress    : 可选回调 (processed_files, total_files, chunks) -> None
    """
    # chromadb 是可选依赖，延迟 import
    try:
        import chromadb
    except ImportError as e:
        raise RuntimeError(
            "RAG 功能需要额外依赖，请执行：pip install -e '.[rag]'"
        ) from e

    db_dir.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(db_dir))

    # 每次索引先删旧 collection，避免脏数据
    try:
        client.delete_collection(collection_name)
    except Exception:
        pass
    coll = client.create_collection(name=collection_name)

    files = list(_iter_code_files(Path(root)))
    total_files = len(files)
    all_ids: list[str] = []
    all_docs: list[str] = []
    all_metas: list[dict] = []

    for idx, f in enumerate(files):
        chunks = _chunk_file(f)
        for ci, (text, s, e) in enumerate(chunks):
            all_ids.append(f"{f.relative_to(root)}::{ci}")
            all_docs.append(text)
            all_metas.append({
                "path": str(f.relative_to(root)),
                "start_line": s,
                "end_line": e,
            })
        if on_progress:
            on_progress(idx + 1, total_files, len(all_docs))

    if not all_docs:
        return 0

    # 批量调 embedding
    vectors = await embed_texts(all_docs)

    # ChromaDB 的 add 是同步 API，直接调
    coll.add(
        ids=all_ids,
        documents=all_docs,
        embeddings=vectors,
        metadatas=all_metas,
    )
    return len(all_docs)
