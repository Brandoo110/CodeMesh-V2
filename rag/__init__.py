"""
RAG 模块：代码库检索增强
==========================

让 Agent 在回答前能「语义搜索」整个 codebase，找到相关代码片段，
避免每次都盲猜或读全部文件。

子模块：
  - embedder  : 把文本转成向量（调 DashScope embedding）
  - indexer   : 扫描代码库 → 切 chunk → 算向量 → 存 ChromaDB
  - retriever : 用户 query → 向量化 → topK 检索返回片段
"""
from .embedder import embed_texts
from .indexer import build_index, DEFAULT_DB_DIR
from .retriever import retrieve

__all__ = ["embed_texts", "build_index", "retrieve", "DEFAULT_DB_DIR"]
