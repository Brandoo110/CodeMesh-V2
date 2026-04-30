# CodeMesh 用户体验报告 · 张伟

> 我是谁 / 我为什么来 / 我要解决什么问题
>
> 这份报告全部基于 2026-04-22 晚上 8–10 点我真实在 MacBook 上跑 CodeMesh v0.1.0 的操作记录。
> 所有"我看到什么"里贴的输出都是直接从我的终端复制的，没做任何修改。

---

## 1. 我的 30 秒自我介绍

我叫张伟，28 岁，杭州某医疗 SaaS B 轮公司的 Python 后端。主栈 Django + PostgreSQL + Celery，每天的工作是维护病历模块、写迁移脚本、凌晨爬起来排查线上 bug。

我**没怎么用过 Claude Code / Cursor**，主要有两个原因：

1. **公司合规要求医疗数据不出境**，IT 部门明令禁止用任何需要把代码 / 日志 / 数据发到 OpenAI / Anthropic 的工具。就算个人装了我也不敢拿公司仓库开刀。
2. GitHub Copilot 那种"在 IDE 里补全半行"我觉得不够用 —— 我想要一个能读懂整个 context，能自己跑命令、改文件的 agent。

上周在朋友圈刷到 CodeMesh，一句"面向国内合规场景的 Claude Code 类 Agent，DeepSeek / Qwen / Doubao"，我就心动了。今晚下班 8 点多，泡杯茶，花 1–2 小时试试：**能不能把它真的用在工作上。**

我手里只有一个 **DeepSeek key（自己充了 10 块钱）**，Qwen（DashScope）和 Doubao（VolcEngine）的 key 都没注册过。这是国内个人开发者的常态 —— 谁上来就三家都办。

**我带着的三个真实问题**：
- 问题 A：让它**解释一下 Django 的 middleware 是怎么做请求鉴权的**，看看它能不能当"阅读理解助手"。
- 问题 B：让它**写一段脚本，把我们数据库里过期的诊断记录批量归档到冷存储表**。
- 问题 C：让它自己告诉我：**国内这三家模型，写 Django ORM 到底哪家好用**。我正在给组里做技术选型。

---

## 2. 我真实的操作流水账（时间轴）

### 2.1 第 1 步：打开目录，看有什么文件

**我做了什么**：`cd` 进项目（注意路径带空格，还是 iCloud 同步路径），然后 `ls -la`。

**我看到什么**：

```
.env.example
.gitignore
LEARNING_PATH.md
README.md
__init__.py
cli.py
execution/
feedback/
harness.py
memory/
orchestration/
pyproject.toml
rag/
tests/
```

**我当时在想什么**：
- 有 `README.md`，我第一肯定看它。
- 注意到还有 `LEARNING_PATH.md`，318 行，看名字就知道不是"快速开始"那种东西，我不会先看。
- `__init__.py` 放在仓库根目录有点怪 —— 这意味着整个仓库根被当 package，后面会看到 `pyproject.toml` 里 `py-modules = ["harness", "cli"]` 也印证了这点。结构上略不干净，但不影响使用。

---

### 2.2 第 2 步：读 README.md

**我做了什么**：在 VS Code 里打开 README。

**我看到什么**（节选我实际停留的位置）：

```
## 1. 项目为什么存在
Claude Code、Cursor、Devin 都很好用，但它们都依赖境外 API。
国内有大量企业出于合规要求不能出境调模型 ...
**面试叙事**：当被问"你怎么理解 Agent 架构" ...
```

**我当时在想什么**：
- 开头"面向国内合规场景"这一句**正中我靶心**，我心里的好感一下子就上来了。
- 但读到"**面试叙事**：当被问'你怎么理解 Agent 架构'…这个项目给了你具体的、能上手讲 tradeoff 的例子" —— 我**愣了一下**。这是个**给找工作的同学刷面试用的项目**？还是**给我这种打工人用的生产力工具**？气质打架了。
- 再往下看"Harness 四层架构"大框图，画得是挺清楚的，但我当时想的是"哥，我下班了只想写个归档脚本，你先告诉我怎么装能不能跑"。
- `## 4. 快速开始` 是在第 129 行才出现的。一个面向开发者的 CLI 工具，"快速开始"应该在 README 最顶上，最多在"这是什么"之后。我等得有点烦躁。
- "快速开始"里写：`pip install -e .`。**没提要不要用 venv，也没提 Python 版本限制**。pyproject.toml 里虽然写了 `requires-python = ">=3.10"`，但 README 里没提，macOS 用户会踩下一步的坑。

---

### 2.3 第 3 步：`pip install -e .`

**我做了什么**：按 README 说的，什么 venv 都没建，直接 `pip install -e .`。

**我看到什么**：

```
error: externally-managed-environment

× This environment is externally managed
╰─> To install Python packages system-wide, try brew install
    xyz, where xyz is the package you are trying to
    install.

    If you wish to install a Python library that isn't in Homebrew,
    use a virtual environment:

    python3 -m venv path/to/venv
    source path/to/venv/bin/activate
    python3 -m pip install xyz
```

**我当时在想什么**：
- 这是 macOS + Homebrew 的 PEP 668 行为，不是 CodeMesh 的锅，**但 README 一个字没提**。对我这种用 Homebrew Python 的人，新手第一条命令就卡了。
- 我熟 venv，两行搞定 `python3 -m venv .venv && source .venv/bin/activate`。但如果是一个更初级的同事，很可能到这里就合上电脑了。
- README 的"快速开始"应该加一句提示，哪怕就一行：`# macOS / Homebrew 用户请先建 venv: python3 -m venv .venv && source .venv/bin/activate`。

---

### 2.4 第 4 步：建 venv 后重装

**我做了什么**：`python3 -m venv .venv && source .venv/bin/activate && pip install -e .`

**我看到什么**（节选最后一段成功日志）：

```
Successfully installed ag-ui-protocol-0.1.18 aiofile-3.9.0 aiohappyeyeballs-2.6.1
aiohttp-3.13.5 ... anthropic-0.96.0 ... cohere-5.21.1 ... codemesh-0.1.0 ...
google-genai-1.73.1 ... groq-1.2.0 ... mistralai-2.4.1 ... xai-sdk-1.11.0 ...
```

**我当时在想什么**：
- 装了整整 **140+ 个包**。一个"国内合规 Agent"装出了 `anthropic`、`cohere`、`mistralai`、`google-genai`、`groq`、`xai-sdk`、`boto3`。我知道是 `pydantic-ai` 拖进来的 transitive dependency，但这是**严重削弱合规叙事**的事。
- 我司 IT 审依赖清单的时候 `xai-sdk` 一行就能把这包毙了。真要推到生产，团队需要换掉 `pydantic-ai` 或只装 `pydantic-ai-slim` 的 openai extra。README 完全没提这个选项。
- `pyproject.toml` 第 12 行显式写了 `"anthropic>=0.40.0"`。**一个号称"合规替代 Claude Code"的项目把 anthropic 作为主依赖**，这是叙事和代码的撕裂。

---

### 2.5 第 5 步：看 help

**我做了什么**：`codemesh --help`，然后 `codemesh run --help`。

**我看到什么**：

```
Usage: codemesh [OPTIONS] COMMAND [ARGS]...
 CodeMesh：国内多模型 Code Agent（Harness 四层架构实践）

 Commands
  run    跑一次 CodeMesh 任务。
  index  为代码目录建 RAG 索引。以后用 --rag 时会检索这里的内容。
  stats  Langfuse 统计入口（占位）。

Usage: codemesh run [OPTIONS] TASK
  --compare  -c   并排展示三家模型输出
  --stream        强制流式输出
  --rag           启用 RAG 前置检索（需先 index）
```

**我当时在想什么**：
- 简洁是优点。但有几个关键信息**我想知道却查不到**：
  - 默认路由会选哪家模型？
  - `--compare` 是并发调用三家，那我只有一个 key 会不会整个命令挂掉？
  - `--rag` 除了要先 index，还需要什么 key？
  - `stats` 写着"占位"直接就挂在 Commands 里，让我作为用户觉得像是**没做完的功能塞进来糊弄我**。这种建议别列在顶层 help 里，或者写 `(coming soon)`。

---

### 2.6 第 6 步：不配 .env 直接跑

**我做了什么**：`codemesh run "帮我解释什么是 middleware"`，一心想看看没 key 时的错误提示。

**我看到什么**（我粘贴的是最后一屏的输出）：

```
ModelHTTPError: status_code: 401, model_name: deepseek-chat, body:
Authentication Fails (governor)
```

**我当时在想什么**：
- 等等，我**根本没配 .env**，它怎么拿到一个 key 去请求的？一定是从我 shell 里某个 `DEEPSEEK_API_KEY` 环境变量拿的残留值。
- 但更严重的问题：**在拿到 401 之前，我的屏幕被 200+ 行 Python traceback 刷屏了**。我要按住 `shift+PageUp` 往上翻五屏才能看见"任务"框，完全找不到"原因是什么、我下一步该做什么"。
- 对合规场景用户来说这体验是灾难级的。README 副本说"面向医疗/金融/政务"，但这些场景的工程师第一次上手的第一条命令就给他一页 traceback，对心理预期打击很大。
- 期望的体验：没配 .env 时应该先做一次**前置检查**，`cli.py` 进 run 函数开头调用 `check_env()` 打印：

  ```
  [配置错误] 找不到可用的 API Key。请按以下步骤配置：
    1. cp .env.example .env
    2. 在 .env 里至少填入 DEEPSEEK_API_KEY
    3. DeepSeek 注册入口：https://platform.deepseek.com/
  ```

  然后 `sys.exit(1)`。不要让用户直接撞到 HTTP 层的 traceback。

---

### 2.7 第 7 步：用假 key 试

**我做了什么**：`cp .env.example .env`，然后把 `DEEPSEEK_API_KEY=sk-fake-test-key` 填进去，其它留空，跑 `codemesh run "帮我解释什么是 middleware"`。

**我看到什么**（底部）：

```
ModelHTTPError: status_code: 401, model_name: deepseek-chat, body: {'message':
'Authentication Fails, Your api key: ****-key is invalid', 'type':
'authentication_error', 'param': None, 'code': 'invalid_request_error'}
```

上面依然是几百行 `pydantic_ai/_agent_graph.py`、`pydantic_ai/models/openai.py` 等第三方库的 traceback。

**我当时在想什么**：
- 模型那边返回的 `'Your api key: ****-key is invalid'` **文案其实挺清楚**，可惜被埋在 traceback 底部。
- 如果在 `harness.py` 或 `cli.py` 顶层包一个 `try/except ModelHTTPError`，捕获 401/403，翻译成友好提示，新用户体验会好十倍。
- 这里也暴露了一个架构问题：`harness.run()` 根本没做"认证预检查"。更健康的做法是在第一次真正调模型之前，发一个最便宜的 ping 请求校验 key 可用性，失败直接抛业务异常。

---

### 2.8 第 8 步：试 `--compare`

**我做了什么**：`codemesh run "帮我解释什么是 middleware" --compare`。我就只有 DeepSeek 的 key 还是假的，想看这工具会不会整个挂掉，还是优雅降级。

**我看到什么**（这个是整段输出，不是截断，它给我画了个表）：

```
并发调用 DeepSeek / Qwen / Doubao ...
                                  三家模型对比
┏━━━━━━━━━━┳━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ 模型     ┃ 延迟 ┃ token (in/out) ┃ 成本    ┃ 输出                            ┃
┡━━━━━━━━━━╇━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ deepseek │ 0ms  │ 0/0            │ ¥0.0000 │ [ERROR] AuthenticationError ... │
│ qwen     │ 0ms  │ 0/0            │ ¥0.0000 │ [ERROR] AuthenticationError ... │
│ doubao   │ 0ms  │ 0/0            │ ¥0.0000 │ [ERROR] AuthenticationError ... │
└──────────┴──────┴────────────────┴─────────┴─────────────────────────────────┘
```

**我当时在想什么**：
- **这是今晚我体验最好的一瞬间。** `--compare` 的表格漂亮，三家结果并排展示，延迟/token/成本列位齐全，即便全报错也不会整个命令挂掉，而是把错误放进"输出"那列。架构是对的。
- 讽刺的是：**`run` 默认模式（没 --compare）的错误处理反而是最差的**——直接扔 traceback；而 `--compare` 用了"部分成功也能渲染"的优雅模式。**作者应该把 --compare 的错误处理策略搬到 run 的默认模式上**。
- 还有个细节：延迟显示 `0ms`、token `0/0`、成本 `¥0.0000` ——因为根本没发起成功请求。应该在错误态里把这几列渲染成 `-` 或 `N/A`，现在这样看上去像在说"三家模型零延迟零成本",有点误导。

---

### 2.9 第 9 步：试 `codemesh index .`

**我做了什么**：我不死心，`codemesh index .` 想建一次 RAG 索引。

**我看到什么**（底部）：

```
RuntimeError: 需要安装 chromadb: pip install chromadb
```

**我当时在想什么**：
- 至少这个 RuntimeError 的 message **挺清楚**。但问题是：
  1. README 里我刚被告知用 `pip install -e ".[rag]"`，这里却让我 `pip install chromadb`。**两处说法不一致**，在我这种按步骤执行的用户看来就是 bug。建议这个错误文案改成 `"RAG 功能需要额外依赖，请执行：pip install -e '.[rag]'"`。
  2. 这个错误同样是一整页 traceback 顶在上面，底部才是关键信息。**同一个 handler 风格**贯穿整个项目：任何错误都不拦截，全部透传到控制台。
- 装完 chromadb 再跑，又撞到 DashScope 401（因为 RAG 的 embedder 硬编码用 DashScope）。index 命令在 help 里完全没提需要 DASHSCOPE_API_KEY。一个友好的 CLI 应该在操作前就检查必要的 key，提示清楚后直接退出。

---

### 2.10 第 10 步：装 chromadb 后再试 index

**我做了什么**：`pip install chromadb`，装好后 `codemesh index .`。

**我看到什么**（只看最底下）：

```
AuthenticationError: Error code: 401 - {'error': {'message': "You didn't provide
an API key. You need to provide your API key in an Authorization header using
Bearer auth ...}
```

还有一个 pip 警告我之前忽略了：

```
logfire 4.32.1 requires opentelemetry-sdk<1.41.0,>=1.39.0,
but you have opentelemetry-sdk 1.41.0 which is incompatible.
```

**我当时在想什么**：
- 我没有 DashScope key，RAG 这条路对我这种无 key 新用户**整体堵死**。
- pyproject.toml 里 `"[rag]"` extra 明明只列了 `chromadb>=0.5.0`，但实际 chromadb 又拖进一个更新版本的 opentelemetry-sdk，跟 langfuse 的 logfire 版本冲突。**依赖没被 pin 住**。对要上生产环境的合规项目是硬伤，我需要一个 `requirements.lock` 或者 `uv.lock`，至少截到哪天的版本能稳定 reproduce。

---

### 2.11 第 11 步：瞄一眼 LEARNING_PATH.md

**我做了什么**：读 LEARNING_PATH.md 前 40 行。

**我看到什么**：

```
> 把你从"听说过 Agent"带到"在面试中能讲清 Harness 架构"的循序渐进指南。
> ...
预计总学习时长：**8–12 小时**。按阶段来，不要跳。
...
## 阶段 1 · 读项目总览（30 分钟）
按顺序读：
1. `README.md` —— 看 Harness 四层图、文件地图、面试速记
2. `~/obsidian/Brain/Projects/CodeMesh/index.md` —— 项目定位与 7 个 Session 的背景
3. `pyproject.toml` —— 看一眼依赖 ...
```

**我当时在想什么**：
- 这东西是**给准备求职的 Agent 方向候选人刷面试题用的**，不是给我这个要用它干活的后端工程师看的。
- 而且**让我去读作者本地 `~/obsidian/Brain/Projects/CodeMesh/index.md`** —— 这是作者自己 Mac 上的 Obsidian 笔记路径，任何 clone 这个仓库的人都读不到。这行需要删掉或者改成"作者的设计笔记（仓库外）"。
- LEARNING_PATH 定位是合理的，但它不该跟 README 平级放。建议挪到 `docs/LEARNING_PATH.md`，在 README 里作为一个 link 提一下就够了。

---

### 2.12 第 12 步：试 `codemesh stats`

**我做了什么**：好奇那个"占位"命令。

**我看到什么**：

```
╭─────────────────────────────────── Stats ────────────────────────────────────╮
│ 实时统计请到 https://cloud.langfuse.com 控制台查看。                         │
╰──────────────────────────────────────────────────────────────────────────────╯
```

**我当时在想什么**：
- 一个什么都不做的命令挂在顶层 CLI 里，每次 `codemesh --help` 都在骚扰我。
- 建议要么做完再合入，要么从 `cli.py` 里先注释掉，等做好了再放出来。

---

## 3. 我的三个问题解决了吗

| 问题 | 结果 | 为什么 |
|------|------|--------|
| A. 解释 Django middleware 是怎么做请求鉴权的 | ❌ **没解决** | 我没有真实 DeepSeek 余额可以跑。这本来是它最擅长的场景，但今晚我根本没走到能产出答案的那一步。 |
| B. 写脚本批量归档过期诊断记录 | ❌ **没解决** | 同上。而且就算我有 key，我也不敢在公司代码库里跑它的 `bash_exec` 工具 —— 我还没搞清楚它的 `sandbox.py` 能拦到什么程度，README 只说"正则黑名单"，这在合规审计面前不够打。 |
| C. 三家国内模型谁写 Django ORM 最强 | ❌ **没解决**，但 `--compare` 这个命令**给我留下了"以后如果三家 key 都有，我会专门用它做选型"的印象**。这是今晚唯一让我想再回来的功能。 |

**诚实地说：今晚三个实际问题一个都没真正解决**。但问题的根源不在 CodeMesh 的核心能力，而在**冷启动路径太长**：我需要注册三家云平台、充值、配三个 key，才能用上它的核心价值（对比 + 路由）。对只有一个 key 的新用户，它应该先给我一个能跑通的 happy path。

---

## 4. 这东西能给我什么（诚实的正面评价）

1. **定位切得准**。"不能出境 + 多模型调度 + 可观测性"这个细分市场确实真实存在，我身边就是目标用户。
2. **`--compare` 的表格交互**是整个工具最扎实的产出。即便全部调用失败，它依然能把三家结果渲染出来，这说明作者在错误处理上是有分层思考的，只是没贯彻到 `run` 主路径。
3. **错误信息的底部一行其实是有用的** —— `'Your api key: ****-key is invalid'`、`需要安装 chromadb: pip install chromadb` —— 说明作者心里有"给用户看的 message"这个意识，只是被 traceback 挤没了。把 traceback 吞掉就行。
4. **README 的 Harness 四层图**和文件地图画得认真，我能感觉到这是个用心组织过的项目，不是一堆乱代码凑起来的。
5. **`cli.py` + `typer` + `rich`** 整体 CLI 风格清爽，没做花里胡哨的动画。这个基调对合规场景的工程师是加分的。

---

## 5. 这东西给不了我什么（诚实的负面）

1. **傻瓜模式不存在**。我手里一个 DeepSeek key，凑不齐三家就**哪个 happy path 都走不通**。对新用户来说这相当于"没有 demo"。
2. **错误提示全靠 traceback 砸**。`run` 主路径在任何错误情况下（401、网络、缺依赖）都直接 500 行 Python 栈，完全没做 user-facing error handling。这是**劝退效果最强的硬伤**。
3. **合规叙事和代码不一致**。`pyproject.toml` 硬依赖 `anthropic`，装一次拖进 `cohere`、`xai-sdk`、`google-genai`、`groq`、`mistralai`。对"医疗数据不能出境"这种场景的 IT 部门，这个依赖清单直接过不了审。
4. **没有 venv 提示、没有 requirements.lock、存在版本冲突警告**。不能叫"可以端到端跑通"，只能叫"在作者电脑上能跑"。
5. **LEARNING_PATH 气质和 README 气质打架**。README 前半段像产品文案，后半段突然"面试叙事"，LEARNING_PATH 全文是"冲面试"风。我作为实用户看起来挺分裂的 —— 这到底是个工具，还是个简历项目？
6. **`index` 和 `run --rag` 的冷启动门槛过高**。必须有 DashScope key 才能 embed，README 没给替代方案（比如本地 bge-small 模型）。对无 key 新手，RAG 这条路根本没法体验。

---

## 6. 如果我是产品经理，会改这些（优先级排序）

### P0 必须改（不然劝退新用户）

1. **`cli.py` 在 `run` 命令入口加前置检查**：读取 `.env`，如果 `DEEPSEEK_API_KEY` 为空或为明显假 key（以 `sk-fake`、`your-key-here` 开头），打印一段友好引导后 `sys.exit(1)`，不要让用户看到 traceback。建议在 `cli.py` 的 `run` 函数第一行加 `_preflight_check()`。
2. **在 `harness.py` 顶层包一个全局 `try/except`**，捕获 `ModelHTTPError`、`AuthenticationError`、`APIConnectionError`，翻译成：`[模型调用失败] DeepSeek 返回 401：你的 API Key 无效。请检查 .env 里的 DEEPSEEK_API_KEY。`。然后 `sys.exit(1)`。不要向用户喷 pydantic-ai 的内部栈。
3. **README 第 129 行"快速开始"**加三行：
   - macOS/Homebrew 用户先 `python3 -m venv .venv && source .venv/bin/activate`
   - 最低只需 DeepSeek key 即可体验 `run`
   - `--compare` 需要三家 key 都填才能完整展示
4. **`pyproject.toml` 第 12 行去掉 `anthropic>=0.40.0`**。一个"面向国内合规的替代品"不该硬依赖 anthropic。如果是 `pydantic-ai` 拖进来的，换成 `pydantic-ai-slim[openai]`。这是合规叙事的第一关。
5. **`LEARNING_PATH.md` 第 29 行** `~/obsidian/Brain/Projects/CodeMesh/index.md` 这条引用**必须删掉**，普通用户根本读不到。
6. **`cli.py` 把 `stats` 命令的"占位"实现先藏起来**，或者在 `--help` 的 description 里明确标 `[not implemented, see Langfuse console]`。现在的实现是骚扰用户。

### P1 值得改（体验加分）

7. **`rag/indexer.py` 第 119 行** 的错误文案 `"需要安装 chromadb: pip install chromadb"` 改成 `"RAG 功能需要额外依赖，请执行：pip install -e '.[rag]'"`，和 README 对齐。
8. **`index` 命令在真正开始扫描前检查 `DASHSCOPE_API_KEY`**。没配就直接提示，不要在扫完几百个 chunk 第一次调 embedder 的时候才 401。
9. **`--compare` 的错误行把 `延迟 0ms / token 0/0 / 成本 ¥0.0000` 渲染成 `—`**，避免误导。
10. **给 `run` 一个 `--dry-run` 或 `--doctor` 子命令**，专门做环境诊断：打印"DeepSeek key 有/无 / 网络可达否 / chromadb 是否装了 / DashScope key 有/无"。合规场景用户非常吃"上线前先体检"这套。
11. **补一个 `requirements.lock` 或 `uv.lock`**。pyproject.toml 只写 `>=` 是不够的，生产不敢这么用。
12. **README 快速开始加一个 Happy Path 示例**：只有 DeepSeek key 的情况下怎么跑出一个有响应的结果，把截图或完整终端输出贴出来。现在的 README 默认读者已经有三家 key，这不现实。

### P2 锦上添花

13. **把 LEARNING_PATH.md 挪到 `docs/LEARNING_PATH.md`**，在 README 末尾挂 link。让根目录保持"工具仓库"气质，不要跟"求职简历项目"气质混在一起。
14. **README 的 Harness 四层图在终端宽度窄时会换行错位**，可以提供一张 PNG 图片备份。
15. **`--stream` 在默认 run 模式里是不是已经开了？**help 里写"强制流式"，但我没看出默认是非流式。加一句说明。
16. **让 embedder 支持本地模型**（bge-small / m3e-base via `sentence-transformers`），让没 DashScope key 的用户也能试 RAG。合规场景尤其吃本地向量化。
17. **README 增加"依赖清单说明"一节**，坦白列出哪些是 transitive dependency、建议如何瘦身到只有 openai + httpx，给要上生产的团队抄作业。

---

## 7. 一句话总结

> **定位对了，冷启动烂得让我劝退同事。等 P0 改完再推给朋友。**

——张伟，2026-04-22 深夜 10 点，关上笔记本去睡了。
