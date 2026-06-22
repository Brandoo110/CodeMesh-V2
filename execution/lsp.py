"""
轻量级 LSP（参考 HKUDS/OpenHarness services/lsp）
=================================================

【这玩意是什么】
LSP = Language Server Protocol。完整版需要起一个语言服务进程（pyright / pylsp 等），
但对 Coding Agent 的 80% 用例（找定义、找引用、列符号、hover 文档）来说，
**Python 的 `ast` 模块自带的能力就够了**。

【为什么不上 pyright/pylsp】
1. 多一个 daemon 进程，启动慢、依赖重
2. Agent 每次只查 1-2 次符号，启动开销不划算
3. ast 静态分析对"找 def class assign"足够精确，覆盖 95% 场景
4. 不依赖 stdin/stdout JSON-RPC 协议，调试简单

【设计说明】
"为什么不直接用 pyright？"
→ Coding Agent 是单次查询多次终止的进程，启动 pyright daemon 不值。
   ast.parse 三十毫秒能把整个仓库的 symbol table 拉出来，
   对 'go to definition / find references / hover docstring' 足够。
   要 type inference 才需要真 LSP。

【五个操作】
  document_symbol  : 列单个文件里的所有 def / class / 顶层 assign
  workspace_symbol : 跨文件按子串模糊搜索符号名
  go_to_definition : 对一个符号名（或 line:col 位置）找定义
  find_references  : 用正则在仓库里找所有引用行
  hover            : 取定义 + signature + docstring 摘要

【局限】
  - 仅支持 Python 文件（.py）
  - 不做 type inference / scope-aware resolution
  - go_to_definition 用名字精确匹配，多个同名函数都返回（让模型挑）
  - find_references 用 \b 词边界正则，会把字符串里出现的同名也算上
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# 跳过的目录（rg 在外部依靠 .gitignore，这里走 Python 必须自己跳）
_SKIP_PARTS = {".git", ".hg", ".svn", ".venv", "venv", "__pycache__", "node_modules"}


@dataclass(frozen=True)
class SymbolLocation:
    """一个符号的位置 + 元数据。"""
    name: str
    kind: str          # function / class / variable
    path: Path
    line: int          # 1-based
    character: int     # 1-based
    signature: str = ""
    docstring: str = ""


def iter_python_files(root: Path) -> list[Path]:
    """稳定顺序返回 root 下所有 .py。"""
    files: list[Path] = []
    for p in root.rglob("*.py"):
        if any(part in _SKIP_PARTS for part in p.parts):
            continue
        if p.is_file():
            files.append(p)
    files.sort()
    return files


def list_document_symbols(path: Path) -> list[SymbolLocation]:
    """列一个文件里的 def / class / 顶层 assign。"""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []
    symbols: list[SymbolLocation] = []
    _collect_symbols(tree, path, symbols, parent=None)
    return symbols


def workspace_symbol_search(root: Path, query: str) -> list[SymbolLocation]:
    """在整个 workspace 里按子串（不区分大小写）模糊搜符号名。"""
    needle = (query or "").lower().strip()
    if not needle:
        return []
    matches: list[SymbolLocation] = []
    for path in iter_python_files(root):
        for sym in list_document_symbols(path):
            if needle in sym.name.lower():
                matches.append(sym)
    return matches


def go_to_definition(
    *,
    root: Path,
    file_path: Path,
    symbol: Optional[str] = None,
    line: Optional[int] = None,
    character: Optional[int] = None,
) -> list[SymbolLocation]:
    """
    找定义。symbol 显式给则用 symbol，否则从 file_path:line:character 提取。
    返回所有同名定义（多个可能时让模型自己挑）。
    """
    target = symbol or extract_symbol_at_position(
        file_path, line=line, character=character
    )
    if not target:
        return []
    matches: list[SymbolLocation] = []
    for path in iter_python_files(root):
        for sym in list_document_symbols(path):
            if sym.name == target or sym.name.endswith("." + target):
                matches.append(sym)
    return matches


def find_references(
    *,
    root: Path,
    file_path: Path,
    symbol: Optional[str] = None,
    line: Optional[int] = None,
    character: Optional[int] = None,
) -> list[tuple[Path, int, str]]:
    """
    词边界正则在所有 .py 里找引用行。
    返回 [(path, line_no, line_text), ...]，limit 5000 条。
    """
    target = symbol or extract_symbol_at_position(
        file_path, line=line, character=character
    )
    if not target:
        return []
    pattern = re.compile(rf"\b{re.escape(target)}\b")
    out: list[tuple[Path, int, str]] = []
    for path in iter_python_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for lineno, line_text in enumerate(text.splitlines(), start=1):
            if pattern.search(line_text):
                out.append((path, lineno, line_text.strip()))
                if len(out) >= 5000:
                    return out
    return out


def hover(
    *,
    root: Path,
    file_path: Path,
    symbol: Optional[str] = None,
    line: Optional[int] = None,
    character: Optional[int] = None,
) -> Optional[SymbolLocation]:
    """命中第一个匹配的定义即返回（含 signature / docstring）。"""
    matches = go_to_definition(
        root=root, file_path=file_path,
        symbol=symbol, line=line, character=character,
    )
    return matches[0] if matches else None


def extract_symbol_at_position(
    file_path: Path,
    *,
    line: Optional[int],
    character: Optional[int],
) -> Optional[str]:
    """从 (line, character) 处抽取最近的 Python 标识符。"""
    if line is None:
        return None
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if line < 1 or line > len(lines):
        return None
    text = lines[line - 1]
    if not text:
        return None
    idx = max(0, min((character or 1) - 1, len(text) - 1))
    # 命中 idx 所在 identifier
    for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", text):
        if m.start() <= idx < m.end():
            return m.group(0)
    # 兜底：返回行内第一个 identifier
    for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", text):
        return m.group(0)
    return None


def _collect_symbols(
    node: ast.AST,
    path: Path,
    bucket: list[SymbolLocation],
    *,
    parent: Optional[str],
) -> None:
    """递归收集 def / async def / class / 顶层 assign。"""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = f"{parent}.{child.name}" if parent else child.name
            args = [arg.arg for arg in child.args.args]
            bucket.append(SymbolLocation(
                name=name, kind="function", path=path,
                line=child.lineno, character=child.col_offset + 1,
                signature=f"def {child.name}({', '.join(args)})",
                docstring=ast.get_docstring(child) or "",
            ))
            _collect_symbols(child, path, bucket, parent=name)
        elif isinstance(child, ast.ClassDef):
            name = f"{parent}.{child.name}" if parent else child.name
            bucket.append(SymbolLocation(
                name=name, kind="class", path=path,
                line=child.lineno, character=child.col_offset + 1,
                signature=f"class {child.name}",
                docstring=ast.get_docstring(child) or "",
            ))
            _collect_symbols(child, path, bucket, parent=name)
        elif isinstance(child, ast.Assign):
            # 只记录顶层 / 类内 assign，不进函数体（局部变量太碎）
            if parent is None or _is_class_parent_node(node):
                for target in child.targets:
                    if isinstance(target, ast.Name):
                        name = f"{parent}.{target.id}" if parent else target.id
                        bucket.append(SymbolLocation(
                            name=name, kind="variable", path=path,
                            line=target.lineno, character=target.col_offset + 1,
                            signature=f"{target.id} = ...",
                        ))
        else:
            _collect_symbols(child, path, bucket, parent=parent)


def _is_class_parent_node(node: ast.AST) -> bool:
    return isinstance(node, ast.ClassDef)


__all__ = [
    "SymbolLocation",
    "iter_python_files",
    "list_document_symbols",
    "workspace_symbol_search",
    "go_to_definition",
    "find_references",
    "hover",
    "extract_symbol_at_position",
]
