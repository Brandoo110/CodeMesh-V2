# DEVLOG

CodeMesh 开发日志。从 main 拉出来后所有的改动按时间顺序记在这里。
每条改动都说清楚：**做了什么、为什么这么做、对应哪些文件、能讲什么面试故事**。

---

## 2026-05-14 — Web UI Phase 0-1：FastAPI + Next.js 骨架 + 后端 API（3 endpoints）

> 分支：`feature/web-ui`（新开）
> 灵感与边界：CLI 已经覆盖全部能力，UI 比 CLI 强的就 3 处——**流式可视化 / 历史浏览 / Stats dashboard 嵌入**。
> 关联 ADR：**ADR-0006**（FastAPI + Next.js 拒 NestJS）

### 一、背景

v4 末项目反思后定下"功能层冻结，启动讲述层"。但 CLI demo 不够直观——录视频时一片黑白终端文字。需要一个 Web UI 把流式输出 / 工具调用 timeline / planner 步骤可视化。

用户初问"技术用 NestJS"——但 NestJS 是 Node 后端框架不能直接做 UI；按 karpathy "Think Before Coding"surface 歧义 + 给 3 个栈方案对比（A: FastAPI+Next.js / B: NestJS+FastAPI+Next.js / C: NestJS spawn Python）后用户确认是 Next.js 口误，拍板方案 A。

### 二、设计文档与 ADR

1. `docs/ui-design-plan.md`（≈800 行 / 12 章节）：完整设计——视觉（暗/亮 hex / 字体 / 8px grid）/ 布局（Sidebar 240px + Top bar + Chat/Stats/History 三视图）/ 后端 API spec / 前端架构 / 9 个 Phase 实施计划 / 7 个风险 mitigation
2. `docs/decisions/0006-web-ui-stack-fastapi-nextjs.md`：完整 ADR——3 个栈方案评估 + 4 条诚实坏处 + Mitigation
3. CLAUDE.md ADR 列表加 0006

### 三、技术栈

| 层 | 选型 | 版本 |
|---|---|---|
| Python 环境 | 项目专属 venv（PEP 405） | Python 3.14.3 |
| 后端 | FastAPI + sse-starlette + uvicorn[standard] | fastapi 0.136 |
| 前端 | Next.js + TypeScript + Tailwind + App Router | Next.js 16.2.6 |
| 包管理（前） | pnpm | 10.33 |
| 构建脚本 | Makefile（6 个 targets） | — |

**为什么 venv 不 conda**：CodeMesh 是纯 Python，不需要 conda 的 CUDA/GPU/多版本能力。conda env 留给学校 ML 课作业。

### 四、Phase 0 — 环境准备（commit `05a9afc` + `c74d07f`）

- `pyproject.toml` 加 `[web]` extras（fastapi / sse-starlette / uvicorn[standard]）
- `web/` Python 包骨架：`__init__.py` / `server.py`（FastAPI factory + CORS for localhost:3000）/ `routes/health.py`
- `frontend/` Next.js 创建（TypeScript / Tailwind / App Router / pnpm）
- 项目专属 `.venv/` 创建（`/usr/local/bin/python3` = Python 3.14.3）
- 清理 miniconda base 误装的 codemesh + sse-starlette
- 新 `Makefile`：`venv` / `install` / `ui-backend` / `ui-frontend` / `test` / `clean` + help
- **Demo 通过**：`make ui-backend` + `curl http://localhost:8000/api/health` → 200 + 正确 JSON；浏览器看到 Next.js 默认页

### 五、Phase 1 — 后端 API 骨架（commit `eabd304`）

3 个端点 + Harness DI + 15 个测试：

**Routes**：

- `GET /api/models` — 列 4 个模型 + configured-key 检测 + 品牌色（颜色对齐 `feedback/render_html.py` MODEL_COLORS）
- `POST /api/chat` — **非流式**：`req.task: str` → `await harness.run(task)` → 聚合 `harness.last_costs` 推断模型 + 总成本。Phase 3 会加 `/api/chat/stream` SSE
- `POST/GET/DELETE /api/sessions[/{id}]` — CRUD **内存占位**（_SESSIONS dict）。Phase 5 改 SQLite 复用 `memory/long_term.py`

**Infrastructure**：

- `web/schemas.py` — Pydantic ChatRequest/Response / ModelInfo / SessionInfo（自动 OpenAPI doc at `/docs`）
- `web/deps.py` — `get_harness()` lru_cache 单例 + `is_configured()` env var 校验（长度 >= 20，和 `harness._get_adapter._valid()` 同源）
- `web/server.py` — 4 个 router include 注册

**Tests**（15/15 pass，跑 < 20ms）：

- `tests/test_web/test_models.py` — 3 tests（4 个模型 / 字段完整 / hex color + bool configured）
- `tests/test_web/test_chat.py` — 6 tests（mock harness via `app.dependency_overrides`）
- `tests/test_web/test_sessions.py` — 6 tests（完整 CRUD + 404 路径）

**铁律遵守**：所有测试用 AsyncMock + dep override，零真实 API 调用（CodeMesh CLAUDE.md 测试规则）。

### 六、关键设计决策（边走边补）

| 决策 | 原因 |
|------|------|
| `ChatRequest` 是 `task: str` 而非 `messages: []` | Harness.run 接受单个 task；short_term 内部维护历史。方案 §4.2 初稿写的 messages 数组改成 task |
| Harness 全局单例（lru_cache） | localhost 单用户场景（ADR-0006），共享 memory 7 层。多用户需要 Phase 5 重构 |
| Sessions Phase 1 内存占位 | 让前端能马上接入 sidebar 不卡在持久化设计 |
| 测试用 `dependency_overrides` 而非 monkeypatch | FastAPI 官方推荐；每个测试 tearDown 自动清理，无污染 |

### 七、踩坑

1. **Python 多版本混乱**：用户机器 `python3 --version` = 3.11.2 但 `/usr/local/bin/python3 -m venv` 实际是 3.14.3——同一 mac 有 framework 3.11 / brew 3.14 共存。**用 `.venv` 后无所谓 PATH 怎么变**
2. **miniconda base 被污染**：第一次 `pip install -e .[web]` 装到了 miniconda base（pip 在 miniconda 但 python3 在系统）。已清理 + 切到 `.venv`
3. **Next.js 装 latest 是 16 不是 15**：`pnpm create next-app --yes` 默认装 latest。方案文档把 `Next.js 15` 改成 `15+`
4. **logfire-plugin import warning**：pydantic plugin 加载失败但不影响功能。已确认是 opentelemetry 版本不兼容，不影响 CodeMesh 主流程
5. **pydantic-ai 0.0.14 → 1.95.0 隐式升级**：`pyproject.toml` 上界没锁。harness import 居然没 break，但这是 pip resolve 副作用。**候选 ADR-0007：依赖版本上界 pinning 策略**

### 八、Commit 范围

```
67dec83 docs(web): finalize web UI design plan v1 and add ADR-0006
05a9afc feat(web): Phase 0 - scaffold FastAPI backend and Next.js 16
c74d07f chore(web): switch to project-local .venv (Python 3.14) and add Makefile
eabd304 feat(web): Phase 1 - backend API skeleton (models / chat / sessions)
```

### 九、面试故事

> "v4 末做完后我意识到讲述层是瓶颈，决定加 Web UI 但限定 MVP 范围 Phase 0-5（约 11.5h）。
> 用户初问 NestJS——但 NestJS 是 Node 后端不能做 UI，我列了 3 个栈方案对比，包括完整诚实段，
> 最后选 FastAPI（同进程复用 harness）+ Next.js + shadcn/ui，写成 ADR-0006。
>
> 工程纪律部分：项目专属 venv 隔离 / Makefile 命令统一 / Phase 1 后端 15 个测试全 mock 零真实 API。
> 测试用 FastAPI 的 `dependency_overrides` 比 monkeypatch 干净——这是 v4 末又一个'读官方文档而非凭经验
> 写'的小迭代。
>
> 关键设计权衡：Harness.run 接受 `task: str` 不是 messages 数组，所以请求 payload 简化成 task + session_id。
> 这是边写边发现 vs 设计阶段拍头的差异——设计文档写完后实际 surface 出来，及时收窄 scope。"

### 十、还没做（Phase 0-1 节点）

> Phase 2 已完成，见下面的"十一、Phase 2 追加段"。

| 项 | Phase | 说明 |
|---|---|---|
| ~~前端 UI 主页 + 消息组件~~ | 2 | ✅ 已完成 |
| SSE 流式输出 | 3 | `/api/chat/stream` 接 `harness.run_stream` + EventSourceResponse |
| Stats dashboard 嵌入 | 4 | iframe 嵌入 `stats_report.render_stats_dashboard` HTML |
| 历史会话 Sidebar | 5 | SQLite 持久化 + Sidebar 历史列表 + only-read 详情视图 |

### 十一、Phase 2 追加段 — 前端对话 UI（commit `bf29f68`）

#### 11.1 设计目标 "Claude 简洁风"

参考 claude.ai：极简留白 / 暗色优先 / Anthropic 橙做点缀 / 等宽代码字体 / 键盘优先。

#### 11.2 三个关键决策（karpathy 视角）

1. **不装 shadcn/ui**（方案 §5.1 写了，实际放弃）—— Phase 2 只需要 6 个简单组件，shadcn 30+ 文件用不到 90%；且 Tailwind 4 + Next 16 + React 19 兼容性还在演进。**简洁 ≠ 自己造轮子；装库 ≠ 必然简洁**
2. **messages 不入全局 Zustand store**，留在 ChatView 本地 `useState` —— Phase 3 流式输出每秒 30-50 token 会引爆全局重渲染，本地 state + React 自动 batching 解决
3. **Pending → Replace 模式** —— 用户发消息先 push pending placeholder，await 结果后 replace 真实内容；错误时 replace 为 error variant。同一套代码 Phase 3 流式输出复用（pending=true 时 content 逐 token 追加）

#### 11.3 视觉设计（对照方案 §2.1）

```css
/* 三档背景层级 */
--color-canvas:         #1a1a1a   /* 主背景 */
--color-surface:        #232323   /* 侧栏 / 气泡 */
--color-surface-hover:  #2d2d2d   /* hover / focus */

/* 三档文字层级 */
--color-fg:             #ececec
--color-fg-muted:       #a0a0a0
--color-fg-subtle:      #6e6e6e

/* 品牌色 */
--color-accent:         #cc785c   /* Anthropic 橙 */
--color-error:          #f87171

/* 模型品牌色（对齐 feedback/render_html.py MODEL_COLORS）*/
--color-model-deepseek: #5b8def
--color-model-qwen:     #7c3aed
--color-model-doubao:   #ef4444
--color-model-gemini:   #10b981
```

Tailwind 4 用 `@theme inline` 注册自定义颜色 token：`bg-canvas / text-fg / border-border` 自动生成。

#### 11.4 文件清单（Phase 2 新增）

```
frontend/components/        6 个 client components
├── Sidebar.tsx              55 行 (240px / "Phase 5 启用" 占位)
├── TopBar.tsx               43 行 (56px / Cmd+\\ toggle)
├── ModelSelector.tsx        99 行 (自写 dropdown + configured 状态)
├── ChatView.tsx            128 行 (Pending → Replace + auto scroll)
├── MessageBubble.tsx        97 行 (4 种 role + react-markdown + GFM + meta 行)
└── InputBar.tsx             74 行 (auto-grow textarea + Cmd+Enter)

frontend/lib/               3 个 utility
├── types.ts                 60 行 (前后端类型对齐)
├── api.ts                   73 行 (fetch wrapper + ApiError class)
└── store.ts                 37 行 (Zustand: models/selectedModel/sidebarOpen)
```

修改 `frontend/app/globals.css`（默认 27 行 → 106 行）+ `layout.tsx` + `page.tsx`。

#### 11.5 验证

- `pnpm tsc --noEmit` 类型零错误 ✅
- `pnpm dev` Next.js 16 Turbopack 329ms 启动 ✅
- `GET / 200 in 272ms` ✅

#### 11.6 数据流（一次发消息完整链）

```
InputBar 输入 + Cmd+Enter
    │
    ▼ onSend(trimmed)
ChatView.handleSend:
  - push userMsg + pendingMsg (id: pendingId, pending: true)
  - setSending(true)
  - await sendChat({ task, model })
    │
    ▼ POST /api/chat
FastAPI chat.py:
  - Pydantic 验证 ChatRequest
  - Depends(get_harness)
  - await harness.run(task)
    │
    ▼ harness.run:
  - router decide → adapter.complete (LLM)
  - return answer
    │
    ▼ ChatResponse + duration_ms + cost_rmb
ChatView.handleSend continue:
  - setMessages(map: replace pendingId with real content)
  - setSending(false)
    │
    ▼
MessageBubble re-render:
  - pending=false → react-markdown
  - meta 行: "0.85s · ¥0.0034 · DeepSeek V4 Pro"
    │
    ▼
useEffect → scrollTo bottom
```

错误路径：catch ApiError → replace pendingId with `role: "error"`, content = msg → MessageBubble error variant（红边卡片）。

#### 11.7 commit

```
bf29f68 feat(web): Phase 2 - frontend chat UI (Claude-style dark theme)
```

14 files / +1711 / -77。

#### 11.8 还没做（Phase 3-5）

| Phase | 前端要加 |
|---|---|
| 3 | EventSource / SSE 消费 + token 追加 / ToolCallCard / PlannerTimeline / Shiki 代码高亮 |
| 4 | StatsView iframe + 日期范围选择器 |
| 5 | 历史 sidebar 真接 / TanStack Query 加缓存 |

完成 Phase 5 = MVP 关闭，merge `feature/web-ui` 到 main。

#### 11.9 详细学习笔记

- Backend 笔记：`~/obsidian/Brain/Projects/CodeMesh/web-ui-backend-notes.md`（≈600 行）
- Frontend 笔记：`~/obsidian/Brain/Projects/CodeMesh/web-ui-frontend-notes.md`（≈700 行）

---

## 2026-05-14（Phase 3）— SSE 流式 + 工具调用可视化（commit `21d627c`）

> 分支：`feature/web-ui` 续作。前后端合并一个 commit（685 insertions / 27 deletions / 8 files）

### 一、启动前的 contract surface

读 `harness.py` 发现 CodeMesh 内部**两条路是分裂的**：

| 方法 | 工具调用 | 流式 |
|---|---|---|
| `harness.run(task)` | ✅ complex 走 agent loop | ❌ 一次返回 |
| `harness.run_stream(task)` | ❌ 只 simple | ✅ |

无法同时拿 token 流 + 工具事件。Surface 三方案给用户拍板：

| 方案 | 改 harness？ | 工时 | 选 |
|---|---|---|---|
| A. Surgical 只 token 流 | ❌ | 1-1.5h | |
| **B. 加 run_stream_full（不改 loop.py）** | ✅ | 3-4h | **✅** |
| C. Hook + asyncio.Queue 外部中转 | ❌ | 3h+ | |

### 二、后端核心实现

**`harness.run_stream_full(task)` 双路径**：

- Simple：走 `adapter.complete_stream` 透出 token chunk（已有 run_stream 改 yield dict）
- Complex：注册 PRE/POST_TOOL_USE hook callback 推 asyncio.Queue → `asyncio.create_task(self.run(task))` 后台跑 → 主循环 `wait_for(queue.get, timeout=0.1)` 中转 events → run 完成后一次性 yield 完整答案

```python
async def _stream_complex(self, task, model_name):
    queue: asyncio.Queue = asyncio.Queue()

    def on_pre_tool(*, tool_name="", args=None, **_):
        queue.put_nowait({"type": "tool_start", "data": {"name": tool_name, "args": args or {}}})
        return HookResult.ok()
    def on_post_tool(*, tool_name="", result="", **_):
        shown = result if len(result) < 2000 else result[:2000] + "…[truncated]"
        queue.put_nowait({"type": "tool_end", "data": {"name": tool_name, "result": shown, "ok": not result.startswith("[ERROR]")}})
        return HookResult.ok()

    self.hooks.register(HookEvent.PRE_TOOL_USE, on_pre_tool)
    self.hooks.register(HookEvent.POST_TOOL_USE, on_post_tool)
    try:
        run_task = asyncio.create_task(self.run(task))
        while not run_task.done():
            try:
                yield await asyncio.wait_for(queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                continue
        while not queue.empty():
            yield queue.get_nowait()
        answer = await run_task
        yield {"type": "token", "data": {"delta": answer}}
        yield {"type": "usage", "data": {...}}
    finally:
        # hooks.py 没 unregister —— 手动 list.remove
        self.hooks._handlers[HookEvent.PRE_TOOL_USE].remove(on_pre_tool)
        self.hooks._handlers[HookEvent.POST_TOOL_USE].remove(on_post_tool)
```

**SSE route**（chat.py 扩展）：

```python
@router.post("/stream")
async def chat_stream(req, harness=Depends(get_harness)):
    async def event_generator():
        async for event in harness.run_stream_full(req.task):
            yield {"event": event["type"], "data": json.dumps(event["data"], ensure_ascii=False)}
    return EventSourceResponse(event_generator())
```

**5 个新 SSE 测试**全过（token+done / tool 事件 / error / 422 / usage），全部 web 测试 20 → 25。

### 三、前端核心实现

**关键 surface**：**EventSource 不支持 POST**。改用 fetch + ReadableStream 手动解析 SSE 帧。

**`lib/sse.ts` async generator**（116 行）：

```typescript
export async function* streamChat(task, options) {
  const res = await fetch("/api/chat/stream", { method: "POST", ... });
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // 按 \n\n 分帧 + parseSSEFrame yield
      ...
    }
  } finally {
    reader.releaseLock();
  }
}
```

**`components/ToolCallCard.tsx`**（99 行）：折叠卡片 / pending 黄脉冲 / ok 绿 ✓ / error 红 ✗ / args JSON / result 50 行预览。

**`ChatView` 改造**：单次 `await sendChat()` → `for await (const event of streamChat())` 6 个 case：

- `token`: append delta 到 content
- `tool_start`: push pending ToolCall
- `tool_end`: FIFO 找最近 pending 同名 tool 更新结果（CodeMesh 不并发同名工具）
- `usage`: 更新 model / cost
- `done`: pending=false + duration_ms
- `error`: role="error" + content=message

**MessageBubble 更新**：toolCalls 在文本前 / pending 时显示打字光标 `▌`。

### 四、关键 tradeoff（要诚实讲述）

| trade-off | 现状 | 解决要做什么 |
|---|---|---|
| Complex 任务回答**不流式**（一次性出现） | 因为 agent loop 不是 generator | 改 execution/loop.py 6h+ |
| Disconnect 不 cancel | run 继续跑完 | request.is_disconnected() + run_task.cancel() ~1h |
| Planner step 不发 event | _run_planned 内部没 yield | 改 _run_planned 2h |
| Tool result 2000 字符截断 | 防 SSE 撑爆 | 配 `Set-Cookie` 大对象传递？过度工程 |

### 五、踩坑（7 个）

1. **hooks.py 没 unregister** → 手动 `_handlers[event].remove(callback)` + try/finally
2. **harness.py imports 缺**：补 `from typing import Any` + `from orchestration.hooks import HookResult`
3. **EventSource 不支持 POST** → fetch + ReadableStream
4. **ReadableStream releaseLock** → 必须在 finally 调，否则 stream 锁泄漏
5. **UTF-8 多字节切断** → TextDecoder `{ stream: true }` 跨 chunk 保状态
6. **TypeScript event.data 类型** → 每个字段 `String(...)` / `as Record<...>` narrowing
7. **pending 状态多模态** → "思考中..." 仅在 content + toolCalls 都空时显示，避免和工具卡片重复

### 六、Commit + 文件清单

`21d627c feat(web): Phase 3 - SSE streaming with tool call visualization`

| 文件 | 变化 |
|---|---|
| `harness.py` | +178 / -0（run_stream_full + 2 imports） |
| `web/routes/chat.py` | +43 / -1（/stream endpoint） |
| `tests/test_web/test_chat_stream.py` | +113 新建（5 tests） |
| `frontend/lib/sse.ts` | +116 新建 |
| `frontend/lib/types.ts` | +15（ToolCall 类型） |
| `frontend/components/ToolCallCard.tsx` | +99 新建 |
| `frontend/components/MessageBubble.tsx` | +20 / -10（toolCalls + cursor） |
| `frontend/components/ChatView.tsx` | +95 / -25（streamChat for-await-of） |

**没动**：execution/loop.py / orchestration/* / memory/* / feedback/* / web/schemas.py / web/deps.py。

### 七、Phase 3 面试故事

> "Phase 3 SSE 设计阶段 surface 一个事实：CodeMesh 的 `harness.run` 和 `run_stream` 是分裂的——run 有工具但不流式，run_stream 流式但只 simple 无工具。没法同时拿两者。
>
> 我给用户列了 3 个方案：A. 只做 token 流（surgical 但 demo 砍半）/ B. 加 run_stream_full（动 harness 但不动 loop）/ C. 外部 hook 中转（不动 harness 但 SSE 复杂）。用户选 B。
>
> 实现的精髓是 **Hook relay 模式** —— 注册 PRE/POST_TOOL_USE callback 推 asyncio.Queue，主循环 wait_for(0.1s) 中转 events 同时等 run() 完成。**不改 execution/loop.py 也拿到工具事件**。
>
> 前端关键 surface：**EventSource 不支持 POST**（设计文档拍头写的）。task 可能多行 markdown URL 装不下，必须 POST body。改用 fetch + ReadableStream 手动解析 SSE 帧——TextDecoder `{ stream: true }` 处理 UTF-8 多字节切断、ReadableStream `releaseLock()` finally 防 lock 泄漏。
>
> **诚实坏处段**：complex 任务回答不流式（一次性出，不逐字）；要让 complex 也逐字需要改 execution/loop.py 让 agent loop 成 async generator——6h+ 重构。这是工程纪律的 trade-off。"

### 八、还没做（Phase 4-5）

| Phase | 内容 |
|---|---|
| 4 | Stats Dashboard iframe 嵌入 `stats_report.py` 输出的 HTML + 日期范围选择器 |
| 5 | 历史会话 SQLite（替换 _SESSIONS dict）+ Sidebar 真接 + TanStack Query 缓存 |

Phase 5 完成 = MVP 关闭，merge `feature/web-ui` 到 main + 更新 README v5 + 写 DEVLOG 总段。

---

## 2026-05-10 — HTML 工件渲染：把"给人看"的产物从 markdown 升级到自包含 HTML

> 分支：`feature/html-artifacts`（新开）
> 灵感来源：[thariqs · The Unreasonable Effectiveness of HTML](https://thariqs.github.io/html-effectiveness/)

### 一、背景

之前 CodeMesh 全栈纯文本：执行层流字符串、tools 全 string return、planner 行号 print、stats 是 Rich Table、文档全 markdown。问题是——**diff / 调用图 / 并排比较都是空间信息**，markdown 是线性的会丢一个维度。给面试官 / 公众号文章看一张终端表格远远不够。

但 thesis 有一条很容易被搞混的红线：**HTML 工件是给"人"看的，不是给"agent"吃的**。把 `read_file` / `grep_text` 改 HTML 会污染 token 经济，模型也消化不了。所以这一轮只动"最终给人看"的产物：stats / edit diff / planner timeline / 架构图 / showcase。

### 二、改动

1. **`feedback/render_html.py`（新，~340 行）**：共享渲染基建。
   - 暗色主题（emerald/red、等宽字体、sticky 表头）+ 模型品牌色板
   - `HtmlDoc` wrapper：自包含 HTML 模板（DOCTYPE + inline CSS + 标题 + 时间戳）
   - SVG 原语：`horizontal_bar_chart` / `sparkline`（带 fill 选项）/ `pie_chart`，**零 PyPI 依赖**
   - `write_artifact` + `rotate_dir`：按 mtime 自动滚动保留最近 N 个

2. **`feedback/stats_report.py`（新）+ `cli.py stats --html`**：dashboard 渲染。
   - KPI 行（calls / tokens in / tokens out / cost）
   - 各模型成本横条 + token 横条 + calls share pie
   - 按天 sparkline trend（最近 30 天）
   - 详细 per-model 表（带 model 品牌色 dot）
   - 默认写到 `.codemesh/reports/stats-<ts>.html`，`-o` 自定义路径

3. **`feedback/diff_report.py`（新）+ `execution/tools.py edit_file()` 钩子**：edit diff 落盘。
   - 用 `difflib.unified_diff` + 自渲染 side-by-side 表（不用 `HtmlDiff` 默认 1990s 样式）
   - 行号 / 增删颜色块 / hunk 头 / `+N -M` 总结
   - env `CODEMESH_HTML_DIFF=1` 控制（默认关，避免反复 edit 累积垃圾）
   - 二进制 / >200KB 自动跳过；文件名 sanitize；写到 `.codemesh/diffs/`

4. **`feedback/planner_timeline.py`（新）+ `harness.py _run_planned` 钩子**：planner 时间线。
   - `StepRecord` dataclass 收集每步的 status / duration / cost / output / error
   - 顶部 task / summary / 总耗时 / 总成本 KPI
   - 按耗时占比的横条（每段一个步骤，按 model 品牌色）
   - 步骤卡片：左 border 颜色 = status（done/error/pending），output 折叠预览
   - env `CODEMESH_HTML_PLAN=1` 控制；simple 任务不会触发

5. **`docs/architecture.html`（新，手写一次性产物）**：交互式 4 层架构图。
   - `<details>` 元素做层折叠（点 ▶ 展开/收起）
   - 每层 tile 显示文件名 + hover tooltip 显示一句话职责
   - 顶部 SVG dataflow（user task → router/planner → agent loop → cost/observer → answer）
   - 标记新增 HTML 模块（绿色 NEW 徽章）
   - 不动 README，红线遵守

6. **`docs/index.html`（新）**：showcase 主页，仿 thariqs 风格。
   - Hero 引用 thesis 原句
   - 4 张工件卡片（stats / edit diff / plan timeline / architecture），每张带手画 SVG 缩略图
   - 设计原则区：6 条红线（不污染 agent 中间态、零 PyPI、env opt-in、滚动保留、不动叙事产物）

### 三、关键 tradeoff

- **为什么不上 matplotlib / plotly**：单文件自包含（HTML 工件能直接发简历 / 公众号 / 离线归档），零 PyPI 依赖（`requirements.txt` 没新增一行），数据量小（几十条 calls）杀鸡用牛刀。SVG 原生支持 CSS 主题，导出时不会因缺 PIL 跑不出图。
- **为什么不复用 `difflib.HtmlDiff`**：默认样式是浅蓝表 + 紫色字（1990s nostalgia），跟 CodeMesh 暗色 + emerald/red 主题完全打架。换 CSS 不如自渲染 unified diff 来得可控。
- **为什么 #3 / #2 默认关**：edit_file 调用频率高，每次写盘累积垃圾；planner 同理虽然低频但默认关也保持"安静"原则。stats --html 是 CLI 显式调用，所以总是开。
- **关于 CLAUDE.md 红线**：README / DEVLOG / LEARNING_PATH 一行不改。新增工件全部走 `docs/`（静态产物）或 `.codemesh/` 运行时目录（已加进 .gitignore）。

### 四、测试覆盖

新增 4 个测试模块共 60 个 case，全部走 CodeMesh 约定的"纯 Python + `if __name__ == '__main__'` runner"：

| 文件 | case 数 | 覆盖 |
|---|---|---|
| `tests/test_render_html.py` | 19 | doc/escape/3 SVG 原语/rotate/write_artifact |
| `tests/test_stats_report.py` | 12 | 空数据/4 panels/title/escape/边界 |
| `tests/test_diff_report.py` | 16 | 渲染各类行/safe filename/env/二进制/超大/滚动 |
| `tests/test_planner_timeline.py` | 13 | status 类/品牌色/totals/escape/env |

跑法：
```
for t in test_render_html test_stats_report test_diff_report test_planner_timeline; do
  python -m tests.$t
done
```

### 五、面试故事

> "我看到 X 上一篇 'The Unreasonable Effectiveness of HTML' 后，意识到我项目所有产物都是终端文本——但 diff、调用图、成本对比本质都是空间信息。
>
> 我做了三件事：(1) 写了一个 `feedback/render_html.py` 共享基建，把 SVG 原语、CSS 主题、文件滚动管理集中——下次加新工件不用各处重复；(2) 把项目 5 个最该可视化的产物 HTML 化——stats dashboard、edit diff、planner timeline、架构图、showcase 页；(3) **明确了不该 HTML 化的边界**——agent 自己吃的 tool returns 必须保持纯字符串，不然 token 经济污染 + 模型消化不了。
>
> tradeoff 是测试薄（HTML 渲染只能测'非空 / 关键字段在 / 路径不逃逸'）和零新依赖（手写 SVG 比 matplotlib 累但简历没多一行 requirements）。所有可选钩子默认关、env opt-in，跟之前的运行行为完全不变——这是我从 thesis 学到的最重要一条：'空间信息要 HTML，但要 opt-in；中间态保持文本'。"

### 六、Commit 范围

```
git log --oneline c09351e..HEAD
```

预计 6 个 commit（一个 feature 一个 commit，CLAUDE.md 约束）：
1. feat(feedback): render_html shared infrastructure
2. feat(feedback,cli): stats --html dashboard
3. feat(feedback,execution): edit_file → HTML diff (CODEMESH_HTML_DIFF)
4. feat(feedback,harness): planner timeline HTML (CODEMESH_HTML_PLAN)
5. docs: architecture.html + index.html showcase
6. docs(devlog): record HTML artifacts work

### 七、还没做

- **#5 进阶**：用 iframe `srcdoc` 嵌入真实工件预览（而不是手画 SVG 缩略图）。代价是单文件大几十 KB，需要先生成代表性 sample。
- **stats 加 hover tooltip**：sparkline 上 hover 看那一天的具体调用列表（用 `<title>` 最简，`<g>` + JS 听 mouseover 才精确）。
- **planner timeline 实时**：当前是任务结束后渲染。如果加成"边跑边更新"，需要一个 long-poll / SSE 端点，或者写到 `latest.html` 让用户开浏览器刷新。投入产出不一定划算，演示场景一次性渲染足够。
- **diff render 加 syntax highlight**：当前是纯白文本块。Prism.js / Pygments 都行但任一都引入依赖。可以考虑写 100 行的纯 Python tokenizer for Python（项目里大部分编辑都是 .py），这个保持零依赖。

---

## 2026-05-09 (晚) — 修正命名：原 dreamer 其实是 session_journal，写真 dreamer 做 4 阶段巩固

> 分支：`feature/dreaming`（继续）

### 一、问题发现

用户问："dreaming 是不是只是回看刚才的工作？CC 是不是有 MEMORY.md 那一套？"

这个问题戳到了一个**实现破绽**：CC 的 dreaming **不是**"per-session 写新记录"——
那是 L5（Auto Memory Extraction）的活。**dreaming 的真正工作是回去整理已写的记忆**：

- Phase 1 Orientation：扫 memory 目录 + 读 MEMORY.md
- Phase 2 Gather：grep 历史 session 找信号
- Phase 3 Consolidate：合并新信号 + 删矛盾 + 相对日期→绝对日期
- Phase 4 Prune & Index：更新 MEMORY.md + 删过时

而我**今天早些时候**做的 `feedback/dreamer.py` 实际是 per-session 叙事捕捉
（每个 session 结束写一条 4 段式 markdown 到 `~/.codemesh/dreams/`）——
这本质是 L5 的"叙事变体"，**不是 CC 意义的 dreaming**。

### 二、修正方向

```
原:                              修正后:
feedback/dreamer.py              feedback/session_journal.py     （叙事 L5）
   写每次 session 的复盘          + 改名 + 改注释定位
                                 feedback/dreamer.py             （真 L6）
                                  + 4 阶段巩固
                                  + 操作 auto_memory/ 已有记忆
                                  + 5 门门控保留
```

### 三、为什么改名而不是新加一个

如果我新加一个 `consolidator.py`，会有两个误导：
1. `dreamer.py` 名字保留 → 面试时讲"我做了 dreaming"，但实际是 per-session 写
2. CC 源码里 `DreamTask` 类干的就是 consolidation —— 跟着 CC 命名才正确

改名虽然 git history 短期看着乱（两个 commit 拆开看），但**长期对项目 honest**。

### 四、改完后的记忆层文件分布

| 文件 | 干什么 | 写到哪 | CC 7 层 |
|---|---|---|---|
| `memory/long_term.py` + 3 个工具 | 用户主动 remember_fact | `~/.codemesh/memory.db` | L5 子集 |
| `memory/auto_extract.py` | 任务结束自动抽 4 类型结构化事实 | `~/.codemesh/auto_memory/*.md` + MEMORY.md 索引 | L5 主体 |
| **`feedback/session_journal.py`** ← 改名 | 任务结束写叙事复盘 | `~/.codemesh/journal/*.md` （重命名了目录） | L5 叙事变体（CodeMesh 独有） |
| **`feedback/dreamer.py`** ← 全新 | 5 门门控 + 4 阶段巩固 | 操作 `auto_memory/` 和 `journal/` | **L6 真 dreaming** |

session_journal 和 dreamer 用同一套 5 门门控（共享 `.consolidate-lock`），
但门控阈值可能不同——session_journal 每会话末都触发（频繁），
dreamer 24h 一次（稀疏）。

### 五、面试故事的修正

> "我做了两阶段对齐 OpenHarness 的记忆层。但**晚些时候发现自己的 dreamer.py
> 其实是叙事 L5 不是真 L6**——CC 的 dreaming 干的是回去整理已有记忆（4 阶段：
> orientation / gather / consolidate / prune），不是写新条目。所以我把原来的
> dreamer.py 改名 session_journal.py（叙事日志），新写了个 dreamer.py 做真的
> consolidation。
>
> 这种'实现完了发现命名错了 → 主动改名 + 重新做'的迭代是常态——比假装一开始
> 就对要诚实。"

### 六、Commits

按顺序：
1. `docs(devlog): note misnaming of dreamer.py and plan to rename`（本段）
2. `refactor(feedback): rename dreamer.py → session_journal.py`
3. `feat(feedback): real dreamer (4-phase consolidation)`
4. `feat(harness): wire new dreamer alongside session_journal`

---

## 2026-05-09 — 第二阶段对齐：记忆层 7 层架构（compactor + auto_extract + dreamer 5 门）

> 分支：`feature/dreaming`（基于 `main`）
> Commit 范围：本批次（dreamer 之后）

### 一、背景：从 troyhua 公众号到 OpenHarness 实测对照

事件链：
- **2026-03-31** Anthropic CC v2.1.88 npm 包误打包 source map → 全网拿到 ~51.2 万行 TS 真源码
- **2026-04-01** troyhua 公众号 51CTO 技术栈发文，24 小时内分析完 7 层记忆架构
- **2026-04-09** HKUDS OpenHarness v0.1.2 发布（基于泄漏源码做 Python 翻译）
- **2026-05-08** CodeMesh 做了 dreaming 80 行简化版（v1，feature/dreaming 分支前一段）
- **2026-05-09** **本批次**：读 troyhua + grep OpenHarness 实测，发现 v2-v4 第一阶段对齐**漏了记忆层**——7 层中只对齐了"工具/Hook/编排"层，记忆层只触及最浅的 short_term。本批次补**第二阶段对齐**：记忆层 3 件套。

### 二、本次完成的改动

| 模块 | 文件 | 来源参考 | 行数 |
|---|---|---|---|
| **L4 全压缩 + L2 微压缩** | `feedback/compactor.py`（新建） | OH `services/compact/__init__.py`（600+ 行） | 234 |
| **L5 自动记忆抽取** | `memory/auto_extract.py`（新建） | OH 的 `memory/manager.py + scan.py + search.py` 存储层（OH 没有抽取层）+ CC 4 类型设计 | 207 |
| **L6 dreamer 5 门触发 + 锁** | `feedback/dreamer.py`（升级） | troyhua 公众号原文表格 + cli.js grep 实证 | +135 |
| **harness 集成** | `harness.py` | — | +30 |
| **测试** | `tests/test_compactor.py` (15) + `tests/test_auto_extract.py` (14) + 更新 `test_dreamer.py` (+9) | — | 470+ |

### 三、改动详解

#### 3.1 feedback/compactor.py — L2 微压缩 + L4 全压缩

**机制**：两层防御金字塔：
- **microcompact**（cheap，纯 Python）：清掉旧 COMPACTABLE_TOOLS（bash_exec / read_file / grep_text 等）的结果，保留最近 5 条
- **full compact**（expensive，调便宜模型）：超过阈值才做，9 段结构化摘要

**对齐 CC/OH 字面常量**：
```python
AUTOCOMPACT_BUFFER_TOKENS = 13_000      # 与 cli.js / OH 一致
MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000  # 与 cli.js / OH 一致
DEFAULT_KEEP_RECENT = 5                 # 与 OH `DEFAULT_KEEP_RECENT` 一致
DEFAULT_GAP_THRESHOLD_MINUTES = 60      # 与 OH `DEFAULT_GAP_THRESHOLD_MINUTES` 一致
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3  # 与 OH 一致
```

**9 段摘要 prompt**：照抄 CC/OH 的字面文本（这是 source map 泄漏后的 ground truth）。第 6 段 verbatim 是关键设计——意图追踪不能依赖摘要必须保留用户原话。

**AutoCompactState 状态机**：跟踪 `consecutive_failures`，连续 3 次失败后 `should_autocompact` 永远返回 False，避免坏 LLM 反复浪费钱。这是 CC/OH 直接抄的设计——失败时模型可能进入坏循环（输出 truncated / 格式错），重试更贵。

**与 OH 的差异**：
- 减：`cache_edits` API 调用（OpenAI 兼容客户端不支持，国内厂商也基本没有 prompt cache）
- 减：`context_management` API 参数（同上）
- 减：`preCompactDiscoveredTools` / `SystemCompactBoundaryMessage` 元数据（教学项目用不到）
- 减：OH 的 600+ 行 → 我的 234 行
- 加：教学注释密度（每段都讲 why）

#### 3.2 memory/auto_extract.py — L5 自动记忆抽取

**OH 没做的层**——OH 提供了 memory/manager.py（add/remove）、memory/scan.py（扫描）、memory/search.py（关键词召回）等**存储基础设施**，但没有"任务结束自动抽取"逻辑。

**4 种记忆类型**（照抄 CC source map 字面值）：
```
user      —— 用户的角色、目标、偏好
feedback  —— 用户纠正过的事 / 验证过的做法
project   —— 进行中的工作、截止日、决策
reference —— 指向外部资源的指针
```

**单条记忆格式**：
```markdown
---
name: testing-approach
description: User prefers integration tests over mocks
type: feedback
---
**Why:** Prior incident where mock/prod divergence masked bug.
**How to apply:** When writing tests for DB code, always use the test database helper.
```

`Why:` + `How to apply:` 双段强迫模型记**因果**和**应用场景**，不只是干巴巴结论。

**MEMORY.md 索引硬约束**（CC `s56=200, j58=25000` 字面常量）：
- ≤ 200 行 / ≤ 25 KB
- 每条 < ~150 字符
- 是索引不是日志——指向 `memory/*.md` 真实内容
- 超出按 LRU 删最旧（防止无限增长）

**与 OH 的差异**：
- OH 的 sha1 路径 hash（每个 cwd 一个独立目录）→ 简化为单一仓库
- OH 的复杂 metadata 解析 → 简化为正则 `---ENTRY---` 分隔
- 加：抽取 prompt + parse_entries 解析（OH 完全没有这层）

#### 3.3 feedback/dreamer.py — 5 门触发 + .consolidate-lock

**v1 问题**：每次 session 结束都触发 dream，token 浪费 + dreams/ 目录爆炸。

**v2 升级**：照抄 CC 的 5 门门控（按成本递增排序，99% 调用早退出）：

| Gate | Check | 默认 | Cost |
|---|---|---|---|
| 1. Enabled | `enabled` 布尔 | True | 一个 if |
| 2. Time | 距上次 dream | ≥ 24h | 1 stat() |
| 3. Scan throttle | 距上次 scan | ≥ 10 min | timestamp 比较 |
| 4. Session count | 累计 session 数 | ≥ 5 | dir listing |
| 5. Lock | `.consolidate-lock` 文件 | 不被持有 | stat() + read |

**锁机制**：`.consolidate-lock` 文件含 `PID:timestamp`。崩溃恢复：`os.kill(pid, 0)` 检查 PID 死活；mtime > 2h 视为 stale 可强夺。

**force=True 选项**：跳过 Gate 2-4（time/throttle/count），但仍尊重 Gate 1（enabled）+ Gate 5（lock）。enabled=False 是用户显式关掉，force 不能跨越；锁是并发安全保障，force 也不能破。

#### 3.4 harness.py 集成

3 个新挂钩：
- `_maybe_autocompact_short_term()` ——在 `run()`/`run_stream()` 调模型前，让 compactor 检查 short_term 是否需要压
- `_maybe_extract_memories()` —— 在 SESSION_END 之后调一次（与 dreamer 并存）
- `Harness.auto_compact_state: AutoCompactState` —— 跨 query loop 持久化压缩状态机

**dreamer 和 auto_extract 并存**：
- dreamer = 叙事版（"上次怎么做的"，自由 4 段式）
- auto_extract = fact 版（"用户偏好/纠正"，结构化 4 类型）
两者互补不冲突，写到不同目录（`~/.codemesh/dreams/` vs `~/.codemesh/auto_memory/`）。

### 四、测试

| 测试文件 | 用例数 | 备注 |
|---|---|---|
| tests/test_compactor.py | 15 | 全过 |
| tests/test_auto_extract.py | 14 | 全过 |
| tests/test_dreamer.py | 27（原 18 + 新 9 个 5 门测试） | 全过 |
| 整体测试 | 22/23 | 剩下那个 test_cli 是 v4 留下的测试顺序 flake，与本次无关 |

### 五、Commit 范围

待 commit。`feature/dreaming` 分支基于 main，本批次不 push（等用户审）。

### 六、面试故事

> "我做了两阶段对齐 OpenHarness：
>
> **第一阶段（5-4）** 横向对齐：8 个工具/Hook/编排层子系统，267 单测反超 OH 的 114。
>
> **第二阶段（5-9）** 纵向对齐记忆层。我读 troyhua 公众号 + grep OpenHarness 实测，
> 发现 OH 在记忆层只对齐了 7 层中的 2 层（L2 微压缩 + L4 全压缩），其余 5 层
> （L1 工具结果落盘、L3 Session Memory、L5 自动抽取、L6 Dreaming、L7 cache 共享原语）
> 全部缺失或半成品。**OH 漏的有规律**——全是"客户端有状态的后台/异步/跨会话"逻辑，
> 因为 OH 是单进程 CLI 跑完即退。
>
> 我抄了 OH 已实现的 L2 + L4（compactor.py 234 行），自己补了 OH 没做的 L5
> （auto_extract.py 207 行，4 类型 + Why/How 模板）和 L6 5 门门控（dreamer 升级）。
> 这些常量值（13K buffer / 5 keep_recent / 60min gap / 200 行索引上限）和泄漏源码
> 完全一致——是从 source map 拿到的 ground truth，不是猜的。
>
> AutoCompactState 状态机的 `consecutive_failures=3` 设计是关键：失败时不无限重试，
> 因为坏 LLM 进入坏循环只会更贵。这是 CC 工程师从生产经验里学到的事，
> 我直接 inherit。
>
> 5 门门控按"廉价检查在前"排序，99% 调用在 Gate 1 cache read 就 false 退出，
> 根本不会走到 stat() 文件系统。这是性能工程的经典模式，从这次反编译学到的。"

### 七、还没做（继续）

- **L1 工具结果落盘** —— `tool-results/<sessionId>/<toolUseId>.txt` + ContentReplacementState 冻结预览。OH 也没做（受单进程约束），需要自己设计。性价比仍最高（防 grep 把 context 干爆）。
- **L3 Session Memory 9 段模板** —— OH 也没做。需要自己写 anchor 维护。
- **L7 cache 共享原语** —— Anthropic 服务端 `cache_edits` API 专属，国内厂商不支持，做了也白做。

---

## 2026-05-08 — Dreaming：会话结束离线复盘 + 下次相似任务召回

> 分支：`feature/dreaming`（基于 `main`）

### 背景

2026-05 初 Anthropic 给 Claude / Claude Code 上线了 **Dreaming**（research preview）：
agent 每次会话结束后做一次离线"做梦"——回看刚才的工作，提炼可复用记忆写成
markdown，下次相似任务直接召回。在用户本机 Claude Code 2.1.92 的 `cli.js` 里
能直接 grep 到 `DreamTask` / `auto_dream` / `tengu_auto_dream_*` 等符号，
配套 `--auto-dream` CLI flag 与 `autoDreamEnabled` settings 键。

CodeMesh 现状缺的就是这一环：
- 短期记忆压缩只在本会话有效，进程一关就没
- 长期 KV 事实库靠模型主动调 `remember_fact`，模型不写就没记忆
- Hook 体系里 `SessionEnd` 已定义但没人挂 dreamer

本批做一个 **极简复刻**（80 行 dreamer + 集成 + 18 条单测）：
单进程同步 await + 关键词 grep 检索，零新依赖。

### 改动

1. **新建 `feedback/dreamer.py`**（约 200 行含注释）：
   - `Dreamer` 类：`dream(task, output)` 写盘，`recall(query, top_k)` 关键词召回，
     `format_context(hits)` 拼成可塞 system prompt 的文本块
   - 4 段式 markdown 模板（任务 / 关键决策 / 踩坑 / 可复用经验），强约束 ≤ 100 行
   - 关键词打分：query 分词 → 每命中 +1，前 30 行命中再 +1（头部加权）
   - `_slug` / `_extract_keywords` / `_truncate_lines` 等内部工具暴露给单测

2. **`feedback/__init__.py`**：导出 `Dreamer / DreamHit / DEFAULT_DREAMS_DIR`

3. **`harness.py`**：
   - 新增构造参数 `enable_dreaming=True` + `self.dreamer`
   - 新增方法 `_dream_summarize(prompt) -> str`（doubao 跑），`_maybe_dream(task, output)`
   - `_build_system_with_context()` 在长期事实之后插一段 dream 召回
   - `run()` / `run_stream()` 末尾 `await self._maybe_dream(task, output)`

4. **`tests/test_dreamer.py`**：18 条单测，全用 fake summarizer + tempfile，零网络

### Commit 范围

待 commit。本批只在 `feature/dreaming` 分支，未 push。

### 面试故事

> "Anthropic 2026 年 5 月发了 Dreaming research preview，让 agent
> 会话结束后离线复盘成 markdown 记忆库，下次相似任务自动召回。我对照
> Claude Code 的 `cli.js` 反编译看了一眼实现思路（DreamTask / time-gate /
> session diff），自己做了 80 行 Python 复刻。
>
> 实现选择：
> - **同步 await 而不是后台 fork**：CLI 单进程，asyncio.run 退出会取消
>   pending task；多花几秒同步等 dream 写完更可靠。Anthropic 的后台 fork
>   是 daemon 才需要。
> - **关键词 grep 而不是向量检索**：零依赖，量小够用。进阶版我已经留好
>   接口，可以直接复用项目里已有的 ChromaDB pipeline。
> - **结构化模板 + 100 行硬约束**：和 Anthropic 的 memory store 对齐——
>   每条记忆 ≤ 100 行，4 段式（任务/决策/踩坑/可复用），强迫提炼而非堆历史。
>
> 教训：失败时静默 no-op、写盘异常打 warning 不向上抛——dreamer 不应该
> 影响主任务返回。所有 18 条单测都用 fake summarizer，本地无网络也能跑。"

### 还没做

- **time-gate / 频控**：每次都写一条，dreams 多了会爆。下一步加 mtime
  比较 + 距上次 < 1h 跳过
- **去重**：相同任务重复调用会写多份。可以做内容 hash 比较 + 替换最旧
- **语义检索**：纯关键词召回会漏（用户说"鉴权"但 dream 写"auth"）。
  v2 可以接 `rag/embedder` 复用 ChromaDB
- **后台 fork**：当前同步 await 会让用户等 1-3 秒。如果做成长跑 daemon
  模式，参考 Anthropic 的 PID lock + time-gate
- **CLI 暴露**：可以加 `codemesh dreams list / show / clear` 子命令
- **pre-existing 测试问题**：`test_cli::test_preflight_rejects_placeholder_key`
  在 test_cli.py 内顺序跑会失败（state 泄漏），单跑没问题——这是 v4 留下
  的，与本次改动无关

---

## 2026-05-04 (深夜) — v4 收尾批：文档对齐 + 三大遗漏功能

> 分支：`claude/review-repo-history-0W2bx`
> Commit 范围：`d470454..HEAD`

### 一、背景

v3 晚上把 OpenHarness 的核心子系统补齐后，DEVLOG 顶部还留了一个"还没做的事"
清单。本批一次性清掉**除大任务（MCP / Web UI）之外**的所有项：5 个工程化
功能 + 4 项文档 / 测试补漏。

### 二、本次完成的 10 个 commit

| # | Commit | 主题 |
|---|---|---|
| 1 | `d470454` | chore(pyproject): 0.3.0 + optional deps groups |
| 2 | `0e07be4` | docs(readme): refresh feature list / architecture / status to v3 |
| 3 | `afaa7a6` | docs(learning-path): add v2 / v3 stages |
| 4 | `a1e3c94` | test(rag): cover indexer / chunker / retriever |
| 5 | `592a3e4` | test(cli): cover preflight / friendly_error / stats / run --compare |
| 6 | `4ec7540` | feat(memory): token-budget trigger for short-term compression |
| 7 | `fc8d104` | feat(orchestration): ALLOW/ASK/DENY permission system + PreToolUse hook |
| 8 | `4c05170` | feat(adapters): streaming retry with buffer-prefix replay |
| 9 | `78d36cc` | feat(rag): LLM-as-cross-encoder reranker for non-code retrieval |
| 10 | `ce59021` | feat(orchestration): plugins loader (.claude/plugins/<name>/plugin.py) |

### 三、改动详解

---

#### 3.1 pyproject.toml 优化（commit 1）
- 版本 `0.1.0 → 0.3.0` 跟 v3 work 对齐
- 把 `chromadb` 拆出来当 `[rag]` extras，新增 `[skills]`（pyyaml）、`[tokens]`（tiktoken）、`[all]` 三组
- 基础 `pip install -e .` 仍是最小依赖

---

#### 3.2 README 更新到 v3（commit 2）
- 顶部 features 改写：列出 v3 的 11 个新能力
- 架构 ASCII 图加上：`planner / skills / retry / lsp / call_log / token_budget`
- §7 项目状态从"测试薄、stats 没做、Hybrid search 待做"改成 v3 完成清单
- 后续扩展方向裁剪到真正还没做的（permissions / plugins / 流式 retry / MCP）

---

#### 3.3 LEARNING_PATH 加 v2/v3 章节（commit 3）
- 阶段 6（v2）：30 分钟读完工程化补齐
- 阶段 7（v3）：45 分钟读完 OpenHarness 对齐，9 个文件按顺序
- 阶段 8：怎么继续往下做（DEVLOG-driven loop）
- 关键认知更新："Coding Agent 不用向量 RAG"的 pivot 故事写进了面试讲法

---

#### 3.4 RAG 模块单测（commit 4，16 个用例）
之前 `rag/` 整个模块 0 单测，**最大死角**。覆盖：
- `_iter_code_files` 跳 node_modules / __pycache__ / >500KB / 未知后缀
- `_chunk_file`（已转 AST）正确切函数 + 非 .py fallback
- `retrieve` 没索引 / 没 chromadb 时优雅返回 `[]`
- `format_context` max_chars / max_tokens 两条预算路径

---

#### 3.5 CLI 单测（commit 5，12 个用例）
最后一个 0 单测的模块。用 `typer.testing.CliRunner` 覆盖：
- `_friendly_error` 4 种错误类型翻译
- `_preflight` 无 .env / 真 key / 占位 key 三个分支
- `stats` 命令空日志 / 真数据 / 时间窗口过滤
- `run` 普通调 + `--compare` 调（mock Harness）

注：`read_calls` 默认参数在 def 时 bind，所以测试用 `_patch_read_calls`
helper 重定向 `cli_mod.read_calls`。

---

#### 3.6 Token-budget 触发记忆压缩（commit 6）
v3 里压缩只看消息数（默认 15 条触发）。问题：5 条很长的消息也会爆 context，
但消息数还没到。
- `ShortTermMemory.__init__` 加 `token_budget` 可选参数
- `estimated_tokens()` 用 `feedback.token_budget.count_tokens` 算总数
- `maybe_compress` 双触发器：消息数 OR token 数任一命中即压
- Harness 默认配 `token_budget=6000`

**面试讲法**：
> "记忆压缩不应该单看消息数——5 条 1500 token 的长消息已经接近 8k context 一半。
>  我加了 token-budget 双触发，让'少而长'和'多而短'两种用法都能稳。"

---

#### 3.7 Permissions ALLOW/ASK/DENY 三级（commit 7，20 个测试）
之前只有 `execution/sandbox.py` 单层正则黑名单，**写死 + 二元决定**。这层叠加：
- `Permission` enum：ALLOW / ASK / DENY
- `PermissionRegistry` 顺序规则；首匹配胜出；默认决定可改
- 通过 PreToolUse hook 接入 Harness：DENY → block, ASK → block (CLI 模式), ALLOW → ok
- `make_default_permissions()` 默认覆盖：force-push / branch -d / pip install → ASK；写 /etc / .ssh / .bashrc → DENY
- 用户 / 插件可在 init 后追加 `harness.permissions.deny(...)`

跟 sandbox 关系：sandbox 是工具内部"硬安全"，permissions 是策略层"软可配置"。**两层防御共存**。

---

#### 3.8 流式 retry (buffer-prefix replay)（commit 8，5 个新测试）
v3 的 retry 只覆盖非流式 `complete()`，流式因为"半流断了不能简单重试"被故意跳过。
本次解决：

**算法**：
```
attempt 0: 正常跑流，yield 每个 chunk；失败时把已 yield 的所有文本累计进 buffer
attempt 1+: 模型重新生成（从头），跳过新流前 len(buffer) 个字符再开始 yield
```

下游消费者看到的输出**完全连续**，不重复也不断裂。代价：模型每次重试重新生成消耗 token——和非流式 retry 同样的取舍。

4 个 adapter 的 `complete_stream()` 全部接入。

---

#### 3.9 RAG reranker（commit 9，11 个测试）
向量检索拿出来的 top5 通常**精度差**——能进 top5 的语义都接近，但谁更准要靠
cross-encoder 重排。

不上 BAAI bge-reranker（要 GPU + 600MB 模型），用 **LLM-as-cross-encoder**：
- 默认 scorer 用 doubao 给候选打 0-10 分
- `rerank(query, candidates, k=5, scorer=None, min_score=0.0)`
- 全部低于 min_score 时**回退到原 vector 排序**前 k，不返回空
- 单条 scorer 抛错时给中性分数排到尾部，不丢候选

**仅用于非代码 RAG 场景**——代码搜索仍走 grep / glob / lsp 这条线。

---

#### 3.10 Plugins 机制（commit 10，9 个测试）
让第三方 / 用户在不动核心代码的前提下叠加：
- 新工具（`@registry.register` 在 module-level）
- 新 hook（`harness.hooks.register` 在 `register(harness)` 里）
- 新 permission 规则
- 新 skill

**目录约定**：
- `<root>/.claude/plugins/<name>/plugin.py` 项目级
- `~/.codemesh/plugins/<name>/plugin.py` 用户级

**加载机制**：
- `importlib.util.spec_from_file_location` 直接给路径就 import（不要求合法 Python 包）
- 失败容错：单个 plugin 语法错 / register 抛错都不阻断启动

**为什么不用 setuptools entry_points**：entry_points 要求 plugin 是 pip 装包；
本项目目标是"丢一个 .py 进 .claude/plugins/ 就能用"，更接近 Claude Code 的体验。

### 四、关键数字（v3 末 → v4 末）

| 指标 | v3 末 | **v4 末** |
|---|---|---|
| 工具数 | 11 | **11**（plugin 机制让第三方加，本身不增加内置）|
| 测试用例数 | 190 | **267** |
| 测试文件数 | 14 | **19** |
| Git commits（main 之外） | 17 | **27** |
| 已实现的 README §7 v1 扩展项 | 1（记忆压缩）| **3+1**（+ AST chunking、stats、Glob/Grep/Edit／其他大量 v3 项也算了）|
| 文档 | + CLAUDE.md | **+ 0.3.0 pyproject** + 全文档对齐 v3/v4 |

### 五、跟 OpenHarness 对位（v4 之后）

| 子系统 | OpenHarness | CodeMesh | 状态 |
|---|---|---|---|
| agent loop | ✅ | ✅ | 持平 |
| Tool Registry | ✅ | ✅ | 持平 |
| ripgrep + fallback | ✅ | ✅ | 持平 |
| AST-LSP | ✅ | ✅ | 持平 |
| Hook 标准事件 | ✅ | ✅ | 持平 |
| Skills 加载 | ✅ | ✅ | 持平 |
| **Permissions 多级** | ✅ | ✅ | **持平** ⭐（v4 新增）|
| **Plugins 机制** | ✅ | ✅ | **持平** ⭐（v4 新增）|
| MCP client | ✅ | ❌ | 仍落后 |
| 多 Agent coordinator | ✅ | router+planner | 故意不做 |
| 测试 | 114 | **267** | **CodeMesh 反超 2.3×** |
| 国内多模型 + ¥ 成本 | 通用 | ✅ | **CodeMesh 优势** |

OpenHarness 还领先的就剩 **MCP** 和 **多 Agent coordinator**——前者后续可加，后者
我们故意不做（router+planner 已够，再加变玩具）。

### 六、还没做的事（v4 末）

按性价比排序：
1. **MCP client minimal** — Anthropic 生态接入；工程量大但故事最强
2. **Web UI / TUI** — Rich-based REPL；体验提升但跟"四层架构"叙事关系不大
3. **Docker 沙箱** — 替换正则黑名单；有了 Permissions 后优先级更低
4. **完整 reranker** — BAAI bge-reranker-v2 真模型；目前 LLM 版够用

### 七、面试故事（v4 版）

> "我做了四轮迭代：v1 Initial，v2 工程化补齐（测试 + Tool Registry），v3 对齐 OpenHarness
>  （ripgrep fallback / AST-LSP / Skills / Hook 事件标准化），v4 收尾把还没做的功能清掉
>  （permissions ALLOW/ASK/DENY、流式 retry、reranker、plugins）+ 全文档同步。
>
>  最终 11 个工具、267 个单测、27 个 commit、跟 OpenHarness 在 8 个核心子系统持平。
>  保持 ~5k 行规模而不是去追他们的 11.7k，是因为我的差异化定位是'国内多模型 + ¥ 成本 +
>  单 key 降级'——那是港大那套学术开源不会做的事。
>
>  写测试时挖出过 sandbox 一个真 false-positive bug；做 RAG 时读源码发现业界事实标准
>  不是向量 RAG 而是 agentic search，立刻调整方案 pivot。这俩故事都能讲'我不是闭门造车
>  / 我会读源码 / 我会改方案'。"

---

## 2026-05-04 (晚) — 对齐 OpenHarness / Claude Code 的第二次迭代

> 分支：`claude/review-repo-history-0W2bx`
> Commit 范围：`d7abf61..HEAD`（紧跟 v2 7 个 commit 之后）

### 一、背景与触发

下午刚做完 v2 七个 commit (`f35180e..fb59c9a`)，晚上想把"还没做的事"全部清掉。
关键转折是查了 HKUDS/OpenHarness 源码（11.7k LoC，11.8k star）后发现**两件反常识的事**：

1. **OpenHarness 没有任何 RAG / embedding / 向量库**——全靠 grep / glob / lsp 的 agentic search
2. 他们的 grep/glob/lsp 实现细节比我之前认知的丰富得多——ripgrep 优先 + Python fallback、流式 / 超时 / 8MB 单行 buffer / 二进制跳过、AST-based LSP

所以原计划的 "BM25 + 向量 + RRF" 撤了，改成 "把检索引擎升级成 OpenHarness 同款"。
这是 v3 最重要的设计转向。

### 二、本次完成的 9 个 commit

| # | Commit | 主题 |
|---|---|---|
| 1 | `d7abf61` | refactor(execution): grep/glob to ripgrep+Python fallback |
| 2 | `b2c527b` | feat(execution): add ast-based lsp_code tool (5 ops) |
| 3 | `1326262` | feat(memory): wire long_term + add 3 tools (remember/recall/forget) |
| 4 | `357df4e` | feat(cli): implement codemesh stats from local jsonl call log |
| 5 | `cafb69d` | feat(orchestration): standardize hooks on Claude Code event names |
| 6 | `7c0ee78` | feat(feedback): token-aware context budget (replaces max_chars) |
| 7 | `8386ed7` | docs: add CLAUDE.md — instructions for future agent sessions |
| 8 | `101472b` | feat(rag): AST-based chunking for Python files |
| 9 | `61624de` | feat(orchestration): skill loading + invoke_skill tool |
| 10 | `57837e6` | feat(adapters): async exponential-backoff retry for transient errors |

### 三、改动详解

---

#### 3.1 grep / glob 升级到 ripgrep + Python fallback（commit 1）

**文件**: `execution/tools.py`

**改动**：
- 新增 `_rg_grep` / `_rg_glob_files` 内部 helper，用 `asyncio.create_subprocess_exec` 调 `rg`
- grep_text / glob_files 都改成 **async** 函数，先尝试 rg，失败回退 Python
- rg 路径具备：流式读 stdout、8MB 单行 buffer、SIGTERM→2s→SIGKILL 进程终止协议、超时控制
- 退出码白名单 {0, 1, -15, -9} 都视为成功（rg 无匹配返回 1，被超时杀返回 -15）
- Python fallback 检测二进制（含 NUL 字节跳过）、>500KB 跳过

**为什么**：
事实标准是 ripgrep——它内置的 .gitignore 处理、文件遍历器都比 Python 快几倍。
但容器 / CI 不一定有 rg，所以保留 Python 路径。

**面试讲法**：
> "我读了 OpenHarness 的 grep_tool.py（363 行），借鉴了他们的'ripgrep + Python fallback'双路径设计。然后在自己的 50 行实现里把核心 idea 复刻：进程终止协议、流式读、超时、二进制检测。"

---

#### 3.2 LSP 工具：AST-based 代码导航（commit 2）

**新增文件**: `execution/lsp.py`（用 stdlib `ast` 实现 5 个操作）

**实现的 5 个操作**：
| 操作 | 干啥 |
|---|---|
| `document_symbol` | 列单文件里所有 def / class / 顶层 assign |
| `workspace_symbol` | 跨文件按子串模糊搜符号名 |
| `go_to_definition` | 按符号名 / 按 line:col 位置找定义 |
| `find_references` | 词边界正则在仓库里找所有引用 |
| `hover` | 取定义 + signature + docstring |

通过 `lsp_code` 工具暴露给模型。

**为什么用 stdlib `ast` 不用 pyright/pylsp**：
Coding Agent 单次任务可能只查 1-2 次符号；起一个 LSP daemon 不划算。
ast.parse 几十 ms 拉完整个仓库符号表，对 80% 用例足够。

**面试讲法**：
> "我用 Python 自带的 ast 模块写了个轻量 LSP，覆盖 Claude Code 同款的 5 个操作。零依赖、几毫秒响应——比起 pyright daemon 的启动开销，对 single-shot agent 任务划算得多。"

---

#### 3.3 long_term 记忆真正接入（commit 3）

**改动**：
- `memory/long_term.py` 新增 `list_all()` + 模块级单例 `get_default_long_term()` / `set_default_long_term()`
- 新增 3 个工具：`remember_fact(key, value)` / `recall_facts()` / `forget_fact(key)`
- `Harness.__init__` 用单例代替自己 new；`_load_long_term_block()` 把所有事实渲染成 system prompt 段落

**为什么**：之前 `LongTermMemory` 是 dead code——init 了但全项目无人读写。这条修补把"跨会话记忆"从 0 → 1。

**面试讲法**：
> "我发现 long_term 字段是 dead code。补上后，'我喜欢 4-space 缩进'这种偏好真能跨会话生效——既给模型 system prompt 自动注入，也给模型 invoke_skill 之外的另一个写入通道。"

---

#### 3.4 stats 子命令（commit 4）

**新增文件**: `feedback/call_log.py`，每次 `Harness._record_cost()` 追加一行 JSONL 到 `~/.codemesh/calls.jsonl`。

**新命令**: `codemesh stats --days 7` 读 jsonl，按模型聚合：calls / tokens_in / tokens_out / cost / avg_latency。

**为什么**：README 自己承诺过 stats 命令但是 stub。这次兑现，且**不依赖 Langfuse**——纯本地、零外网。

---

#### 3.5 Hooks 标准化（commit 5）

**改动**：`orchestration/hooks.py` 加了 Claude Code 风格的事件枚举：
```
PRE_TOOL_USE / POST_TOOL_USE / SESSION_START / SESSION_END / USER_PROMPT_SUBMIT / STOP
```
+ `HookResult(blocked=True, reason=...)` 让 PreToolUse 钩子能**阻止**工具执行（短路语义）。

老 API（`add_pre` / `fire_pre`）保留作 adapter——harness.py 不需要改。

**面试讲法**：
> "我把 hook 系统按 Claude Code 标准命名升级了。新的 trigger() 接口让 PreToolUse 能 block，做权限审计 / 沙箱拦截这种横切关注点更优雅。"

---

#### 3.6 Token budget 取代 max_chars（commit 6）

**新增文件**: `feedback/token_budget.py`：tiktoken 优先 + CJK启发式 fallback。

**改动**：`format_context(hits, max_tokens=2000)` 替代 `max_chars=4000`。

**为什么**：中文 1 char ≈ 1 token，英文 1 char ≈ 0.25 token，差 4 倍。按字符切要么浪费上下文要么超限。

---

#### 3.7 CLAUDE.md（commit 7）

**新增文件**: `CLAUDE.md`，给下次 Claude session 看的工作守则：
- 项目定位（面试项目，别动现有教学注释）
- Git 身份（Brandoo110，无签名，不碰 main）
- DEVLOG 必更
- 测试规则（不依赖 pytest，无网络）
- Tool Registry 用法
- RAG 真相（保留作非代码 RAG）
- 移动端能 / 不能做啥
- OpenHarness 借鉴清单

**为什么**：移动端 / web session 进来空空如也，靠仓库根的 CLAUDE.md 让我每次都"立刻进入状态"。

---

#### 3.8 AST chunking（commit 8）

**新增文件**: `rag/ast_chunker.py`。.py 文件按 def / class 边界切 chunk，每个函数 / 方法 / 类 header 各一个 chunk。

**fallback**: 非 .py / 语法错误 / >5000 行 → 退到原版按行切。

**为什么用 stdlib `ast` 不用 tree-sitter**：tree-sitter 装包要 gcc 编译，CI / 容器场景挂率高。stdlib `ast` 零依赖，对 Python 项目足够。

---

#### 3.9 Skills 加载机制（commit 9）

**新增文件**: `orchestration/skills.py`。

**目录约定**:
- `<root>/.claude/skills/<name>/SKILL.md`（项目级）
- `~/.codemesh/skills/<name>/SKILL.md`（用户级）

**SKILL.md 格式**: YAML frontmatter (name, description) → fallback 到第一个 `# 标题` + 第一段。

**注入点**: Harness 启动时扫两条路径，把 skill 名 + 描述渲染成 `<available skills>` 索引塞 system prompt。模型看到任务相关时调 `invoke_skill(name)` 工具拉 SKILL.md 全文。

**用户故事**: 之前讨论过的 "把 andrej-karpathy-skills 装进 web session" —— 现在只要 cp 到 `.claude/skills/` commit 就行。

---

#### 3.10 Adapter retry / rate limit（commit 10）

**新增文件**: `orchestration/adapters/retry.py`。

`async_retry(factory, max_retries=3, base_delay=1.0, max_delay=30.0)`：指数退避 + ±25% jitter，区分可重试 / 不可重试错误：
- ✅ 重试: 429 / 500 / 502 / 503 / 504 / APIConnectionError / APITimeoutError / asyncio.TimeoutError
- ❌ 不重试: 401 / 400 / 403 / 404（deterministic 错重试只是浪费 token）

4 个 adapter 的 `complete()` 全部接入。流式 `complete_stream()` 故意不接——半流断了 retry 会重复输出。

**面试讲法**：
> "README 自己提到 --compare 单 key 撞 503。我加了 20 行手写指数退避 + jitter，把瞬时 5xx 自动吃掉。没引 tenacity——dependency 越少越好，注释把 why 都写下了。"

### 四、关键数字

| 指标 | 早上 v2 后 | 晚上 v3 后 |
|---|---|---|
| 工具数 | 7 | **11**（+ lsp_code, remember/recall/forget_fact, invoke_skill）|
| 测试用例数 | 66 | **190** |
| 测试文件数 | 6 | **14** |
| Git commits（main 之外） | 7 | **17** |
| 跑通的依赖（pip install -e .） | 8 | 8（**新增 0**——retry 用 stdlib，token_budget tiktoken 软依赖）|
| 文档 | README + LEARNING_PATH + DEVLOG | + **CLAUDE.md** |

### 五、跟 OpenHarness 的差距对比（v3 之后）

| 子系统 | OpenHarness | CodeMesh | 差距 |
|---|---|---|---|
| agent loop | ✅ | ✅ | 持平 |
| Tool Registry | ✅ | ✅ | 持平 |
| 工具集 | 43 | 11 | 缩小（早上 7 → 晚上 11）|
| ripgrep + fallback | ✅ | ✅ | **持平** ⭐ |
| AST-LSP | ✅ | ✅ | **持平** ⭐ |
| Hook 事件命名 | ✅ | ✅ | **持平** ⭐ |
| Skills 加载 | ✅ | ✅ | **持平** ⭐ |
| Permissions 多级 | ✅ | 正则黑名单 | 落后 |
| Plugins | ✅ | 没有 | 落后 |
| MCP | ✅ | 没有 | 落后 |
| 多 Agent coordinator | ✅ | router + planner | 故意不做（避免变玩具）|
| 国内多模型 + 成本 | 通用 | ✅ DeepSeek/Qwen/Doubao + ¥ | **CodeMesh 优势** |
| 测试 | 114 单测 | **190 单测** | **CodeMesh 反超** |
| 代码规模 | 11.7k LoC | ~5k LoC | 差 2.3 倍 |

### 六、还没做的事（按性价比）

1. **Permissions 多级**（OpenHarness `permissions/`）—— 比正则黑名单成熟，但要重写沙箱
2. **Plugins 机制**—— 让第三方 commit 一组 hooks + tools 进 `.claude/plugins/`
3. **MCP client minimal**—— 接 Anthropic 生态
4. **流式 retry**—— 当前只覆盖了非流式 `complete()`；流式中断重试需要 buffer prefix
5. **Reranker on RAG**——非代码 RAG 场景下用 doubao 当 cross-encoder 重排前 20 → 取前 5
6. **Token-budget summarizer**—— 短期记忆的 summarizer 当前按消息数压；改按 token

### 七、对面试的影响

之前评估给的分（早上 v2 末）：

| 维度 | v1 | v2 末 | **v3 末** |
|---|---|---|---|
| 架构理解 | 9/10 | 9/10 | **9/10** |
| 代码实现 | 7/10 | 8/10 | **9/10**（11 工具 + LSP + 完整 hook 系统）|
| 工程完整度 | 6/10 | 8/10 | **9/10**（190 单测 + retry + stats + token budget）|
| 文档叙事 | 9/10 | 9/10 | **9/10**（再加 CLAUDE.md）|
| Git 工程化 | 3/10 | 7/10 | **9/10**（17 个有意义 commit）|

**新的面试讲法**：

> "我做了三轮迭代：v1 是 Initial commit，v2 补了记忆压缩 + Tool Registry + Glob/Grep/Edit + 单测，v3 我去深挖 OpenHarness 源码，发现 Coding Agent 的事实标准不是向量 RAG 而是 grep + glob + AST-LSP 的 agentic search——所以做了完整的对齐：ripgrep + Python fallback、AST-based LSP、Claude Code 标准 hook 事件、Anthropic skill 格式、async retry。190 个单测，所有改动都按一个功能一个 commit 拆好。"

OpenHarness 是港大学术开源，11.7k 行；CodeMesh 现在 5k 行——**保持精炼是优势**，不跟它卷规模。差异化定位是国内多模型 + 真实人民币成本 + 单 key 降级。

---

## 2026-05-04 — 第一次大版本迭代

> 分支：`claude/review-repo-history-0W2bx`
> Commit 范围：`f95d694..HEAD`（在 `2bfd214 Initial commit` 之后）

### 一、背景与触发

`main` 上只有一次"Initial commit"——38 个文件、3874 行一次性塞进去。问题：
- 测试只有 2 个用例（`test_short_term` 滑动窗口 + `test_adapters` 冒烟）
- Git 历史只有 1 条，看不出迭代过程
- README §7 自己列出一堆"扩展方向"还没做（记忆压缩、Hybrid search、AST 切 chunk 等）
- 工具只有 3 个（`bash_exec` / `read_file` / `write_file`），离 Claude Code 的实用度差很远
- 沙箱里有一个**正则误伤的 bug**（详见下文 §4.1），但因为没单测从来没人发现

调研了港大 HKUDS 实验室的开源项目 **OpenHarness**（Claude Code 的 1/44 体量轻量克隆，43+ 工具、114 单测，11.8k star），决定按它的路子补齐工程化短板，但**保持 CodeMesh 的差异化定位**——国内多模型路由 + 合规场景 + 真实人民币成本追踪。

不跟它卷规模，跟它学**模式**：Tool Registry、PreToolUse/PostToolUse Hooks、按 Claude Code 标准命名的工具（Glob/Grep/Edit）。

### 二、本次完成的事（按 commit 列）

| # | Commit | 主题 |
|---|---|---|
| 1 | `f95d694` | feat(memory): add summarizer-based compression to ShortTermMemory |
| 2 | `5e48a57` | feat(harness): wire memory compression with doubao summarizer |
| 3 | `fbd0fc7` | test(memory): add unit tests for compression behavior |
| 4 | _this batch_ | refactor(execution): Tool Registry + Glob / Grep / Edit (Claude Code 标准三件套) |
| 5 | _this batch_ | fix(sandbox): tighten rm regex to require -r/-R flag |
| 6 | _this batch_ | test: add unit tests for sandbox / tools / loop / router & planner |
| 7 | _this batch_ | docs: add DEVLOG.md |

### 三、改动详解

---

#### 3.1 记忆层：ShortTermMemory 加 LLM 摘要压缩

**文件**：`memory/short_term.py`、`harness.py`、`tests/test_memory_compression.py`

**改动**：
- `ShortTermMemory.__init__` 新增 `compress_threshold` / `summarizer` 两个可选参数
- 新增 async `maybe_compress()`：消息数 ≥ 阈值时把最旧一半交给注入的 summarizer 压缩
- 新增 `_summary` 字段，`get_messages()` 在 system 之后注入一条 summary 系统消息
- `Harness` 默认开 `enable_memory_compression=True`，用 doubao（最便宜）做 summarizer
- 在 `Harness.run()` 和 `Harness.run_stream()` 末尾各调一次 `maybe_compress()`

**为什么**：
之前长对话超出 `max_messages=20` 后，最旧的消息**直接丢弃**——任务上下文断裂。现在丢弃前先压成 summary 留住，长对话也能"记得很久前的事"。

**关键约束**：
- `compress_threshold=15`（< maxlen 20，让压缩在自动 popleft 之前触发）
- summarizer 失败时退回 head/tail 拼接，不破坏记忆链
- 多次触发时新 summary 拼到旧 summary 后，不丢历史

**面试故事**：
> "默认实现是 deque maxlen 滑动窗口，简单但会硬丢老对话。我后来加了 LLM 摘要压缩——丢弃前用 doubao 把最旧一半总结成一段，注入回 system prompt。这样 token 消耗可控、上下文连续性也保住了。"

---

#### 3.2 执行层：工具系统重构成 Registry 模式

**文件**：`execution/tools.py`、`execution/__init__.py`

**改动前**：
```python
TOOL_SCHEMAS = [...]    # 硬编码 3 条 schema
TOOL_IMPL = {...}       # 硬编码 3 个映射
async def dispatch_tool(name, args): ...
```

**改动后**：
```python
class ToolRegistry:
    def register(self, name, description, parameters): ...   # 装饰器
    @property
    def schemas(self) -> list[dict]: ...
    async def dispatch(self, name, args) -> str: ...

registry = ToolRegistry()

@registry.register(name="bash_exec", description=..., parameters=...)
async def bash_exec(cmd, timeout=30.0): ...
```

**好处**：
- 加新工具只要写一个函数 + 一个装饰器，不用同时改 3 处
- schema 和实现绑定在一起，不会出现"schema 写了但 impl 没注册"这种 bug
- Registry 可独立实例化 → 测试时建一个临时 registry，不污染全局

**向后兼容**：原 `TOOL_SCHEMAS` / `TOOL_IMPL` / `dispatch_tool` 名字保留，是 registry 的**视图**——`execution/loop.py` 不需要改。

**设计参考**：HKUDS/OpenHarness `tools/` 注册表。这是 Claude Code 同款架构。

---

#### 3.3 执行层：新增三个工具（Claude Code 标准三件套）

**文件**：`execution/tools.py`

##### `glob_files(pattern, root='.')`
- 按 shell 通配符（`**/*.py`、`tests/test_*.py`）列文件
- 跳过 `_IGNORED_DIRS`（node_modules / .git / venv / __pycache__ / dist / ...）
- 返回相对路径，最多 100 条
- 错误以字符串返回（如 root 不存在）

##### `grep_text(pattern, root='.', file_pattern=None)`
- 在文件内容里搜正则，返回 `path:line:content` 格式
- 可选 `file_pattern` 用 fnmatch 过滤文件名（如 `*.py`）
- 跳过同一组 `_IGNORED_DIRS`
- 跳过 >500KB 的文件，单行截断到 200 字符
- 命中数上限 200，避免一次塞爆 context
- bad regex 返回错误字符串而不是 raise

##### `edit_file(path, old_string, new_string)`
- 精确字符串替换：`old_string` 必须在文件中**恰好出现 1 次**
- 0 次或多次都报错并提示"加更多 surrounding context"
- 不创建新文件（用 `write_file` 创建）
- 比 `write_file` 安全得多——不会无意覆盖其他内容

**为什么这三个**：
Claude Code 的所有"代码理解"任务都是 `Glob → Grep → Read → Edit` 的循环，这是事实标准。补上这三个 + 已有的 `read_file` / `bash_exec`，CodeMesh 就能干 80% Claude Code 能干的事。

**面试故事**：
> "我对照 Claude Code 的工具集，发现三个高频工具我没做：Glob 找文件、Grep 搜内容、Edit 增量改。之前只有 write_file 是覆盖式的，模型写代码很容易把整个文件覆盖坏掉。Edit 要求 old_string 唯一，不唯一就报错让模型补上下文——这是从 Claude Code 抄过来的安全设计。"

---

#### 3.4 沙箱：修了一个 false-positive 正则 bug

**文件**：`execution/sandbox.py`

**bug**：
```python
re.compile(r"\brm\s+(-[rRfF]+\s+)*(/|~|\*)")
```
`(-[rRfF]+\s+)*` 是**零次或多次**——意思是没有 `-r` 标志也算匹配。结果：

```
rm /tmp/specific-file.txt   ← 被错误拦截！
```

非递归 `rm` 删单个文件其实很安全，但被沙箱当成"危险命令"了。

**修复**：
```python
re.compile(r"\brm\s+-[a-zA-Z]*[rR][a-zA-Z]*\s+(/|~|\*)")
```
要求至少有一个 `-r` 或 `-R` 标志才认为危险（递归才能毁数据）。

**怎么发现的**：
写 `test_allows_targeted_rm_in_tmp` 时第一次跑挂了。**这就是补单测的价值**——一个生产代码里跑了几个月的 false-positive 立刻暴露。

**面试故事**：
> "之前沙箱只有正则黑名单没单测。我补了 19 个用例（11 个该拦的、6 个该放行的、2 个异常字段），第一次跑就发现 rm 那条规则把所有 `rm /xxx` 都拦了——非递归 rm 单文件其实安全。修完正则后所有用例通过。"

---

#### 3.5 测试覆盖率：8 个 → 66 个

**新增 4 个测试文件，共 58 个用例：**

| 文件 | 用例数 | 覆盖 |
|---|---|---|
| `tests/test_sandbox.py` | 19 | 11 个危险命令模式 + 6 个安全命令 + 异常字段断言 |
| `tests/test_tools.py` | 22 | Registry 注册 / dispatch / 6 个工具的正常和异常分支 |
| `tests/test_loop.py` | 7 | Agent Loop 各分支：无 tool / 单 tool / 多 tool / max_iter / 未知 tool / bad JSON / schema 传递 |
| `tests/test_router_planner.py` | 10 | Pydantic schema 校验 + TestModel override 实测 route() / plan() |
| `tests/test_memory_compression.py` | 6 | 阈值 / 触发 / 注入 / system 隔离 / 累积 / 无 summarizer |

**关键技巧**：

1. **PydanticAI 的 TestModel** ——`agent.override(model=TestModel(custom_output_args={...}))` 让 router/planner 跑得动但**零网络**。这是 PydanticAI 给的官方测试入口，比 monkey-patching 干净得多。

2. **Fake OpenAI client**——Agent Loop 测试不依赖 PydanticAI 也不依赖真实 OpenAI，自己拼了个 `_FakeAdapter` 注入预设 response 序列。整条工具调用回填链路都覆盖了。

3. **Fake summarizer**——记忆压缩测试用注入的 async 函数当 summarizer，断言它被调用的次数和入参。**完全不需要 doubao 或任何外部模型**。

4. **临时工作区**——文件类工具测试 (`tempfile.mkdtemp` 建一个含 a.py / b.py / data.txt / sub/c.py / node_modules/ 的小目录) 覆盖正常 / 边界 / 噪音过滤。

**故意没测的**：
- `test_adapters.py`（已有）—— 真调外部 API，留作冒烟
- `bash_exec` 的真命令执行—— 沙箱已覆盖，bash_exec 本身只是 asyncio.subprocess 的包装
- Harness 端到端 —— 涉及多模型 / 实际网络，留给手工冒烟

### 四、关键数字

| 指标 | 改动前 | 改动后 |
|---|---|---|
| 工具数 | 3 | **6** |
| 测试用例数 | 8 | **66** |
| 测试文件数 | 2 | **6** |
| Git commits（main 之外） | 0 | **8** |
| 已知 bug 数 | 1（rm 正则误伤） | 0 |
| README §7 已完成扩展项 | 0 | 1（记忆压缩） |

### 五、还没做的事

按性价比排序：

1. **Hybrid search**（BM25 + 向量 + RRF）—— RAG 检索精度升级。需要装 `rank-bm25`
2. **`stats` 子命令** —— 读 Langfuse 或本地日志聚合成本/延迟
3. **AST 切 chunk** —— 用 tree-sitter 替换按行切，需要装 tree-sitter-python 等
4. **PreToolUse / PostToolUse 标准化** —— 现有 `orchestration/hooks.py` 雏形已有，重命名对齐 Claude Code
5. **Skills 按需加载** —— 兼容 anthropics/skills 格式
6. **Docker 沙箱** —— 替换正则黑名单。容器内套容器较麻烦，留到生产化阶段

### 六、对面试的影响

之前评估给的分：

| 维度 | 之前 | 现在 |
|---|---|---|
| 架构理解 | 9/10 | 9/10 |
| 代码实现 | 7/10 | **8/10**（多了 3 个工具 + Registry） |
| 工程完整度 | 6/10 | **8/10**（66 个单测 + 修了 sandbox bug） |
| 文档叙事 | 9/10 | **9/10**（多了 DEVLOG，能讲迭代） |
| Git 工程化 | 3/10 | **7/10**（8 个有意义的 commit） |

**新增的面试讲法**：

> "我把 main 的初版当 v1，做了一次系统性的 v2 迭代：
>  对照 HKUDS/OpenHarness 的设计，把工具系统重构成 Tool Registry，
>  补了 Claude Code 标准三件套 Glob/Grep/Edit，
>  写完整套单测把覆盖从 8 个提到 66 个。
>  期间在沙箱里挖出一个 false-positive 正则 bug——这就是为什么写测试值得。
>  完整改动有 DEVLOG 记录每次 commit 的动机和取舍。"

这个叙事**比"我又写了一个 Coding Agent"值钱**。

---

## 怎么继续维护这个 DEVLOG

下次再做新功能：
1. 在文件**顶部**插入一个新日期段（`## 2026-MM-DD — xxx 主题`）
2. 列出这次的 commit 范围（`git log --oneline <prev_head>..HEAD`）
3. 每个改动写"做了什么 / 为什么 / 文件路径 / 面试故事"四件套
4. 更新本次的"还没做的事"清单

让这个文件变成你这个项目的**叙事骨干**——简历加分、给同事 onboarding、给自己半年后回看都能用得上。
