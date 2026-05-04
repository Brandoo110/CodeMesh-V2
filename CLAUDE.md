# CLAUDE.md

> 给下次进 session 的 Claude 看的工作守则。每条都是真实生效的约束，不是装饰。

---

## 项目定位（最重要的事）

CodeMesh 是 **面试 / 学习项目**，不是上线产品。这条决定了所有 trade-off：

- **代码注释要"教学语气"**：模块顶部的"为什么这样设计 / 面试点 / tradeoff"是**卖点**，不是冗余。**不要重写、删减、换风格**——它们是面试官 review 的关键。
- **可以加新文件、加新功能**，但**不要重构现有文件的注释结构**。
- **README、LEARNING_PATH.md、DEVLOG.md** 都是叙事产物，写新的可以，**别动旧的**。

---

## 身份与提交

| 事 | 怎么做 |
|---|---|
| Git author | `Brandoo110 <Lijj0103syd@gmail.com>` |
| GPG 签名 | 关掉（commit.gpgsign = false） |
| 全局 `~/.gitconfig` | **不要碰**（通常是 `Claude <noreply@anthropic.com>`） |
| 怎么覆盖 | 每条 `git commit` 都用 `git -c user.name=Brandoo110 -c user.email=Lijj0103syd@gmail.com -c commit.gpgsign=false commit -m "..."` |
| 推送分支 | 只能推到 `claude/review-repo-history-0W2bx`，**main 是禁地** |
| commit 拆分 | 一个功能一个 commit；不要把无关改动塞同一个 commit |
| commit message | 英文标题（feat/fix/refactor/test/docs/chore），多行 body 解释 why。**末尾不要带任何"by Claude"标识** |
| 永远不要做 | `--amend`、`--force-push`、`reset --hard`、`push --force-with-lease` —— 除非用户明说 |

---

## Devlog 必更

每完成一个**带 commit 的实质改动**（不是 typo），就在 `DEVLOG.md` **顶部**插入新段：

```
## YYYY-MM-DD — 一句话标题

### 背景
为什么改？解决什么？

### 改动
1. 文件 X：做了 Y
2. 文件 Z：做了 W

### Commit 范围
git log --oneline <prev>..HEAD

### 面试故事
"我做了什么、tradeoff 是什么、参考了哪些开源项目"

### 还没做
列下一步可做的事
```

格式不强求，但**这四块得有**。

---

## 测试

| 事 | 约束 |
|---|---|
| 框架 | **不依赖 pytest**——纯 Python 写 `test_xxx.py`，每个文件末尾自带 `if __name__ == "__main__":` runner |
| 跑法 | `python -m tests.test_<name>` |
| 网络 | **任何测试不允许调真实 API**——用 fake adapter / TestModel / mock |
| Pydantic AI 测试 | 用 `pydantic_ai.models.test.TestModel` + `agent.override(model=tm)`（参考 `tests/test_router_planner.py`） |
| OpenAI client mock | 用 `_FakeAdapter` 注入预设 response 序列（参考 `tests/test_loop.py`） |
| 跑全套 | `for t in test_*.py: python -m tests.$t` |
| 加新功能 | **必须配单测**——评估里"测试薄"是已修补的硬伤，别又开倒车 |

---

## Tool Registry（execution/tools.py）

加新工具的标准做法：

```python
@registry.register(
    name="my_tool",
    description="一句话告诉模型这工具干啥",
    parameters={"type": "object", "properties": {...}, "required": [...]},
)
async def my_tool(...) -> str:        # 或 def，registry 都支持
    try:
        ...
        return "OK: ..."
    except Exception as e:
        return f"[ERROR] {type(e).__name__}: {e}"
```

**铁律**：
- 错误**永远以字符串返回**，不抛异常（模型看不到异常）
- 输入参数尽量用 string，模型生成稳定
- 任何文件类工具默认在 cwd 下，不动模型给的绝对路径除非显式

---

## RAG 的真相

代码检索**不靠向量 RAG**——用 `grep_text`（ripgrep）+ `glob_files`（rg --files）+ `lsp_code`（AST）+ `read_file` 让模型自己 agentic search。这是 Claude Code / OpenHarness 的事实标准。

`rag/` 模块**保留作"非代码场景的 RAG"**（文档库、知识库、用户文本）——别为了代码搜索去碰它。新建代码搜索类工具，请走 `execution/tools.py` + `execution/lsp.py` 这条线。

---

## 移动端 / 手机 session 注意

我（Claude）不能：
- 读取你电脑 / 手机 / iCloud 的任何文件
- 拿到桌面端 Claude Code 配置 / API key
- 调外部 API（DeepSeek / Qwen / Doubao / Gemini）—— 沙箱里 key 没配

我能做：
- 读、改、写 git 仓库的所有文件
- 跑 `python -m tests.*`、`pip install -e .`、`git commit/push`
- 用 ripgrep / git / 任何已装 CLI 工具
- WebFetch + WebSearch 看公开网页

外部 API 测试请你自己拉到本地跑。

---

## 风格

- 代码注释：模块顶部一段"教学叙事"，函数级一两行 docstring，行内**只在非显然处**写注释。模仿现有 `harness.py` / `execution/loop.py` 的密度。
- 错误信息：用模型读得懂的人话（"file not found"、"old_string appears 3 times"），不要直接吐 traceback。
- 命名：snake_case 函数 / 变量；CamelCase 类；ALL_CAPS 常量。
- Type hints：写，但**不强求**全文件覆盖；公共 API 加上即可。
- 异步：执行层 / RAG 层 / Adapter 层都是 async；记忆层混合（save/load 是 async，set_system 是 sync）。新写代码尽量沿用所在层的风格。

---

## OpenHarness 参考

`/tmp/openharness/`（如果你 clone 过）或 https://github.com/HKUDS/OpenHarness 是港大的轻量 Claude Code 克隆，**11.7k 行**。本项目已经从它借了：

- Tool Registry 模式
- ripgrep + Python fallback 双路径检索
- AST-based LSP（services/lsp/）
- HookEvent 标准事件名（PreToolUse / PostToolUse / SessionStart / ...）
- HookResult 含 blocked/reason 字段

**继续借鉴的方向**：plugins/、permissions/、mcp/、coordinator/。

**不要借的**：他们的工具数量（43+）—— 我们走"少而精"。
