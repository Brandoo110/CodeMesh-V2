"""
RAG 模块：代码库检索增强
==========================

让 Agent 在回答前能「语义搜索」整个 codebase，找到相关代码片段，
避免每次都盲猜或读全部文件。

子模块：
  - embedder    : 把文本转成向量（调 DashScope embedding）
  - indexer     : 扫描代码库 → 切 chunk → 算向量 → 存 ChromaDB
  - retriever   : 用户 query → 向量化 → topK 检索返回片段
  - ast_chunker : Python 文件的 AST 级 chunker（每个 def/class 一个 chunk）

【定位说明（v2 之后）】
代码搜索的事实标准是 grep + glob + lsp + read（execution/tools.py 那套），
不是向量 RAG。本模块保留作"非代码场景"的检索（文档、知识库、用户文本）。
仍可对代码库使用，但建议优先用 agentic search。
"""
from .embedder import embed_texts
from .indexer import build_index, DEFAULT_DB_DIR
from .retriever import retrieve
from .ast_chunker import chunk_python_file, chunk_file_with_fallback, CodeChunk
from .reranker import rerank, Scorer

__all__ = [
    "embed_texts",
    "build_index",
    "retrieve",
    "DEFAULT_DB_DIR",
    "chunk_python_file",
    "chunk_file_with_fallback",
    "CodeChunk",
    "rerank",
    "Scorer",
]
