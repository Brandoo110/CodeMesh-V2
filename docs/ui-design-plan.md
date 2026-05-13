# CodeMesh Web UI 设计方案 v1（draft）

> **Status**: Accepted (v1 final) — 用户 review 完毕，技术栈/范围/部署目标已拍板。技术栈决策见 ADR-0006。
>
> **作者**: Brandoo110 + Claude Code 协作
> **日期**: 2026-05-14
> **关联代码**: 全部复用 `harness.py` / `cli.py` / `execution/` / `orchestration/` / `feedback/` 现有能力；UI 只是这些能力的浏览器外壳。
> **已拍板**: 方案 A / MVP=Phase 0-5 / localhost 单用户无鉴权

---

## 0. 技术栈（已拍板）

经 review，技术栈拍板：**方案 A — FastAPI + Next.js 15+ + shadcn/ui + Tailwind**。

```
浏览器（Next.js + shadcn/ui）
    │ HTTP / SSE
    ▼
FastAPI（Python）
    │ 同进程 from harness import Harness
    ▼
harness.run() / run_stream() / 现有 cli 能力
```

**理由摘要**（完整决策记录见 [ADR-0006](decisions/0006-web-ui-stack-fastapi-nextjs.md)）：

- CodeMesh 是 Python 项目，FastAPI 同进程零跨进程开销
- 异步原生匹配 CodeMesh 的 async 风格
- SSE streaming 直通 `harness.run_stream()`
- shadcn/ui 提供 Claude 简洁风的免费等价组件
- "NestJS" 原话经澄清是 Next.js 的口误

**澄清的歧义**：用户原话"技术用 NestJS"——但 NestJS 是 Node 后端框架不能做 UI；UI 必须搭配前端框架（Next.js / React / Vue 等）。经 review 确认用户本意是 Next.js。

---

## 1. 整体设计目标

### 1.1 风格基调："Claude 简洁风"

参考 [claude.ai](https://claude.ai) 的视觉语言：

- **极简留白** —— 大量负空间，不堆功能按钮
- **暗色优先** —— 默认深色主题（更适合阅读代码），可切换到浅色
- **柔和品牌色** —— Anthropic 用的橙色（#cc785c）做点缀，主体黑/灰
- **打字机感** —— 等宽字体处理代码块，无衬线字体处理对话
- **没有花哨动画** —— 只有必要的 fade-in / 流式打字效果
- **键盘优先** —— 所有功能可键盘操作（Cmd+K / Cmd+Enter 等）

### 1.2 UI 的 3 个核心场景

CodeMesh 的 CLI 已经覆盖了所有功能，UI 不要重新发明轮子。UI 比 CLI 强的地方就 3 处：

1. **流式输出可视化** —— 工具调用 / planner timeline / 模型切换的实时呈现，CLI 的彩色文字到 UI 更好看
2. **跨会话历史浏览** —— 翻看之前的对话、ADR 决策时找历史素材方便
3. **Stats dashboard 嵌入** —— `codemesh stats --html` 已经有 HTML，UI 直接嵌入

其他功能优先级低或不做（见 §5 实施阶段）。

### 1.3 设计原则（karpathy 4 条对照）

| 原则 | UI 设计应用 |
|------|------------|
| Think Before Coding | 这份方案就是；每个组件 surface 假设 |
| Simplicity First | MVP 不做用户系统、不做多语言、不做主题 marketplace、不做插件 UI |
| Surgical Changes | 后端 API 只暴露现有 Python 函数，不重写 harness |
| Goal-Driven Execution | 每个 Phase 有可验证的 demo（见 §5） |

---

## 2. 视觉设计

### 2.1 配色（参考 Claude + Anthropic 品牌）

#### 暗色主题（默认）

```css
--bg-primary:      #1a1a1a   /* 主背景 */
--bg-secondary:    #232323   /* 侧边栏 / 卡片背景 */
--bg-tertiary:     #2d2d2d   /* hover / 输入框 */
--border-subtle:   #333333   /* 分割线 */
--text-primary:    #ececec   /* 主文字 */
--text-secondary:  #a0a0a0   /* 次要文字 / 时间戳 */
--text-tertiary:   #6e6e6e   /* placeholder / disabled */
--accent:          #cc785c   /* Anthropic orange，按钮 / link / 强调 */
--accent-hover:    #d68b73   /* hover 时稍亮 */
--success:         #4ade80   /* tool 成功 */
--error:           #f87171   /* tool 失败 */
--warning:         #fbbf24   /* 提示 */

/* 模型品牌色（复用 feedback/render_html.py 已有的 MODEL_COLORS） */
--model-deepseek:  #5b8def
--model-qwen:      #7c3aed
--model-doubao:    #ef4444
--model-gemini:    #10b981
```

#### 浅色主题（备选）

```css
--bg-primary:      #faf9f7   /* Claude 的奶白色 */
--bg-secondary:    #f0eee6
--bg-tertiary:     #e8e6dd
--border-subtle:   #d6d3cb
--text-primary:    #2d2a26
--text-secondary:  #6b6764
--text-tertiary:   #9a9591
--accent:          #cc785c
/* 其他色照搬，对比度调高即可 */
```

### 2.2 字体

| 用途 | 字体 |
|------|------|
| 主体（西文） | Inter（fallback: SF Pro, system-ui） |
| 主体（中文） | 思源黑体 / PingFang SC / system-ui |
| 代码 / 等宽 | JetBrains Mono（fallback: SF Mono, Consolas） |
| 数字 / 统计 | Inter 数字 tabular 变体（`font-variant-numeric: tabular-nums`） |

字号阶梯（基于 16px base）：

```
xs:   12px   /* 时间戳、breadcrumb */
sm:   14px   /* 次要文字、按钮 */
base: 16px   /* 正文 */
lg:   18px   /* 卡片标题 */
xl:   24px   /* 页面标题 */
2xl:  32px   /* hero title */
```

### 2.3 间距系统（8px grid）

```
xs:  4px    /* 紧贴元素 */
sm:  8px    /* 按钮内边距 */
md:  16px   /* 卡片内边距 */
lg:  24px   /* 卡片间距 */
xl:  32px   /* 大区块间距 */
2xl: 48px   /* 页面边距 */
```

### 2.4 圆角

```
sm: 4px    /* 按钮、tag */
md: 8px    /* 卡片、输入框 */
lg: 12px   /* 消息气泡 */
full: 9999px /* 头像、徽章 */
```

### 2.5 阴影（暗色主题用得少，浅色主题用）

```
sm: 0 1px 2px rgba(0,0,0,.05)
md: 0 4px 12px rgba(0,0,0,.08)
lg: 0 12px 32px rgba(0,0,0,.12)
```

---

## 3. 布局结构

### 3.1 整体布局

```
┌─────────────────────────────────────────────────────────────────┐
│ ┌─────────┐ ┌─────────────────────────────────────────────────┐ │
│ │         │ │  Top bar: 模型选择 / Compare / Stats / Settings  │ │
│ │ Sidebar │ ├─────────────────────────────────────────────────┤ │
│ │         │ │                                                 │ │
│ │  新对话 │ │                                                 │ │
│ │  ──────│ │              Chat / Stats / History              │ │
│ │  历史1  │ │                  (主内容区)                      │ │
│ │  历史2  │ │                                                 │ │
│ │  历史3  │ │                                                 │ │
│ │   ...   │ │                                                 │ │
│ │         │ ├─────────────────────────────────────────────────┤ │
│ │ Settings│ │  Input bar: 文本框 + 工具开关 + 发送             │ │
│ └─────────┘ └─────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
   240px              flex 1（剩余宽度）
```

### 3.2 侧边栏（Sidebar）

- 宽度：240px
- 可折叠到 0px（顶栏汉堡按钮 / 快捷键 `Cmd+\`）
- 内容：
  - 顶部 "新对话" 按钮（`Cmd+N`）
  - 历史对话列表（按时间倒序，title 截断 25 字符）
    - hover 显示完整 title + 时间戳
    - 右键菜单：rename / delete / export
    - 当前选中项左侧加 2px accent 色边
  - 底部固定：Settings 入口

### 3.3 顶栏（Top bar）

- 高度：56px
- 左侧：当前对话标题（可点击 inline 编辑）
- 中间：模型选择器（dropdown，显示当前模型 + 切换）
  - DeepSeek（默认）/ Qwen / Doubao / Gemini fallback
  - 每个模型旁显示小圆点（在线状态：✓ key 已配 / ✗ 未配）
  - Compare 模式开关（开启后此 dropdown 变 multi-select）
- 右侧：
  - Stats 按钮（打开 stats dashboard 抽屉，内嵌 `codemesh stats --html`）
  - 主题切换（暗/亮）
  - 设置图标

### 3.4 主内容区

3 种 view，根据顶栏切换：

#### 3.4.1 Chat View（默认，最重要）

```
┌──────────────────────────────────────────┐
│                                          │
│  [user]  问题文本                         │ ← 用户消息（右对齐，淡灰背景）
│                                          │
│  [assistant]                             │ ← 模型回答（左对齐，无背景）
│      流式输出的文本...                   │
│                                          │
│      ┌──────────────────────────┐        │
│      │ 🔧 tool: grep_text       │        │ ← 工具调用卡片（可折叠）
│      │   pattern: "harness"     │        │
│      │   ─────────────────      │        │
│      │   ▶ output (3 hits)       │        │
│      └──────────────────────────┘        │
│                                          │
│      继续输出文本...                     │
│                                          │
│  [assistant]                             │ ← Compare 模式时多个并排
│      [Doubao] ...   [Qwen] ...           │
│                                          │
└──────────────────────────────────────────┘
```

**消息组件细节**：

| 类型 | 视觉 | 交互 |
|------|------|------|
| 用户消息 | 右侧 max-width 720px，圆角 12px，背景 `bg-tertiary` | hover 显示编辑/复制按钮 |
| 模型消息 | 左侧 max-width 720px，无背景，前置头像（圆形 24px） | hover 显示复制/重新生成 |
| 工具调用 | 嵌入模型消息内，卡片样式，左侧 4px 模型色边 | 点击折叠展开 input/output |
| 系统消息 | 居中，灰色斜体，xs 字号 | 不可交互 |
| 错误 | 红色边框卡片，emoji `⚠` | 点击展开 stack trace |

**代码块**：

- 用 `react-syntax-highlighter`（或 Shiki）
- 主题用 `one-dark-pro`（暗色）/ `github-light`（浅色）
- 右上角：复制按钮 + 语言 tag
- 长代码超过 20 行自动折叠（"显示 N 行"）

**Planner Timeline**（当模型走 planner 路径时）：

```
   ① grep_text "Harness"  [400ms ✓]
   │
   ② read_file harness.py  [120ms ✓]
   │
   ③ edit_file harness.py  [800ms ⏳ 进行中]
   │
   ④ run_tests              [pending]
```

- 复用 `feedback/planner_timeline.py` 已有的 step 数据结构
- 实时滚动展开，已完成步骤可点击查看 output
- 失败 step 显示红色 + 错误信息

#### 3.4.2 Stats View

- 直接 iframe 嵌入后端返回的 `stats --html` HTML 文件
- 顶部加日期范围选择器（默认近 30 天）
- 数据来源：`feedback/call_log.py` 的 jsonl + `feedback/stats_report.py` 渲染

#### 3.4.3 History View

- 表格列：时间 / title / model / 消息数 / 总 cost / 操作
- 可搜索 / 可按 model 筛选
- 点击行进入对话详情（只读模式）

### 3.5 输入栏（Input bar）

- 固定在底部，宽度同主内容区
- 文本框：
  - 多行（auto-grow，最大 200px 高度，超过滚动）
  - placeholder："问点什么...（Cmd+Enter 发送，Cmd+/ 切模型）"
  - 支持 markdown 输入预览（次要功能，按需）
  - 支持拖拽文件上传（v2 再做）
- 左下：工具开关
  - `🔧 Tools` —— 默认开启所有工具，可单独 toggle（grep / read / edit / lsp）
  - `🌐 Web` —— v2 再做
- 右下：
  - Token 估算（实时算 prompt token 数，超 8k 警告）
  - 发送按钮（`Cmd+Enter`）

---

## 4. 后端 API 设计（方案 A: FastAPI）

### 4.1 文件结构

```
codemesh/
├── harness.py                  ← 不动
├── cli.py                      ← 不动
├── execution/                  ← 不动
├── orchestration/              ← 不动
├── feedback/                   ← 不动
├── memory/                     ← 不动
├── web/                        ← 新增
│   ├── __init__.py
│   ├── server.py               ← FastAPI app 入口
│   ├── routes/
│   │   ├── chat.py             ← /api/chat, /api/chat/stream
│   │   ├── models.py           ← /api/models
│   │   ├── sessions.py         ← /api/sessions/*
│   │   ├── stats.py            ← /api/stats
│   │   └── settings.py         ← /api/settings/*
│   ├── schemas.py              ← Pydantic 模型
│   └── deps.py                 ← 依赖注入（Harness 单例 / Settings）
└── frontend/                   ← 新增（Next.js 项目）
    ├── app/
    │   ├── layout.tsx
    │   ├── page.tsx
    │   ├── stats/page.tsx
    │   └── settings/page.tsx
    ├── components/
    │   ├── Sidebar.tsx
    │   ├── ChatView.tsx
    │   ├── MessageBubble.tsx
    │   ├── ToolCallCard.tsx
    │   ├── PlannerTimeline.tsx
    │   ├── ModelSelector.tsx
    │   └── ui/                 ← shadcn/ui generated
    ├── lib/
    │   ├── api.ts              ← fetch wrappers
    │   ├── sse.ts              ← SSE client
    │   └── store.ts            ← Zustand store
    └── package.json
```

### 4.2 API endpoints

#### Chat（核心）

```
POST /api/chat
{
  "session_id": "uuid",
  "messages": [...],
  "model": "deepseek",        // 或 ["deepseek", "qwen"] 走 compare
  "tools_enabled": true,
  "stream": false
}
→ 200 { "message": {...}, "usage": {...}, "tool_calls": [...] }
```

```
POST /api/chat/stream
{ 同上, stream: true }
→ SSE: text/event-stream
  event: token        data: {"delta": "你好"}
  event: tool_start   data: {"name": "grep_text", "args": {...}, "call_id": "x"}
  event: tool_end     data: {"call_id": "x", "output": "...", "ok": true}
  event: planner_step data: {"n": 1, "desc": "...", "status": "done", "duration_ms": 400}
  event: usage        data: {"prompt": 1024, "completion": 256, "cost_rmb": 0.0034}
  event: done         data: {}
  event: error        data: {"message": "..."}
```

#### Sessions

```
GET    /api/sessions              → 历史列表
GET    /api/sessions/{id}         → 单个对话详情
POST   /api/sessions              → 新建（返回 uuid）
PATCH  /api/sessions/{id}         → 改 title
DELETE /api/sessions/{id}         → 删
GET    /api/sessions/{id}/export  → 导出 markdown
```

#### Models

```
GET /api/models
→ [
  {"id": "deepseek", "name": "DeepSeek V4 Pro", "configured": true, "color": "#5b8def"},
  {"id": "qwen", "name": "Qwen 3 Max", "configured": true, "color": "#7c3aed"},
  {"id": "doubao", "name": "Doubao Pro", "configured": false, "color": "#ef4444"},
  {"id": "gemini", "name": "Gemini 2.5 Pro", "configured": true, "color": "#10b981"}
]
```

#### Stats

```
GET /api/stats?range=30d&format=html
→ 200 text/html  (直接返回 stats_report.render_stats_dashboard 的 HTML)

GET /api/stats?range=30d&format=json
→ 200 { records: [...], by_model: {...}, totals: {...} }
```

#### Settings

```
GET    /api/settings            → 当前所有设置（API keys 用 `***` 遮罩）
PATCH  /api/settings            → 改单项
GET    /api/settings/tools      → 工具列表 + 启用状态
PATCH  /api/settings/tools/{id} → 单个工具开关
GET    /api/settings/permissions → 当前 permissions 规则
```

### 4.3 数据存储

| 数据 | 存哪 | 备注 |
|------|------|------|
| 对话历史 | `~/.codemesh/web_sessions.db`（SQLite） | 复用现有 memory 层 |
| 历史 jsonl | `~/.codemesh/calls.jsonl` | 复用 `feedback/call_log.py` |
| 用户设置 | `~/.codemesh/settings.json` | 新增，存非敏感设置（主题 / 默认模型 / 工具开关） |
| API keys | `~/.codemesh/.env` | 沿用现有，**不通过 web 写入**（提示用户去文件改） |

**API key 处理边界**：UI 显示是否配了（`configured: true/false`），但 **不接受 web 写入**——用户必须自己改 `.env`。理由：HTTP 经 web 写 key 会被本地浏览器扩展拦到，且 SQLite 落盘明文不安全。

### 4.4 Streaming 实现

复用现有 `harness.run_stream()`：

```python
@router.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    async def event_generator():
        async for event in harness.run_stream(req.messages, model=req.model):
            # event 是 dict，类型有 token / tool_start / tool_end / done / error
            yield {
                "event": event["type"],
                "data": json.dumps(event["data"], ensure_ascii=False)
            }
    return EventSourceResponse(event_generator())
```

依赖：`sse-starlette` 库（FastAPI 标准 SSE 工具）。

### 4.5 错误处理

| 场景 | 后端响应 | 前端表现 |
|------|----------|----------|
| API key 没配 | 400 + `{"code": "missing_api_key", "model": "qwen"}` | 顶栏弹 toast + 跳设置页 |
| 模型 timeout | 504 + 错误详情 | 消息气泡显红色，给"重试"按钮 |
| Tool 执行失败 | tool_end event with `ok: false` | 工具卡片红边 + 展开错误信息 |
| 后端崩溃 | 500 | 顶栏红色 banner + reload 按钮 |
| Token 超 context | 400 + `{"code": "context_overflow", "tokens": 12345}` | 提示压缩历史 / 新建对话 |

### 4.6 鉴权（MVP）

**MVP 不做鉴权**——只跑在 localhost，假设单用户本地使用。

如果未来要部署到公网或多用户：
- 最小方案：单 token（启动时随机生成，要带在 header），Bearer auth
- 进一步：OAuth / 邮箱密码（这是另一个项目了，超出 CodeMesh 范围）

---

## 5. 前端架构

### 5.1 技术栈细节

| | 选型 | 理由 |
|---|---|---|
| 框架 | Next.js 15+ (app router，实际装 16.x) | 文件路由 + RSC + 内置 dev server |
| UI 库 | shadcn/ui | Claude 风格的等价开源；复制即用，可改样式 |
| CSS | Tailwind CSS | shadcn/ui 默认依赖 |
| 状态 | Zustand | 比 Redux 轻 100 倍，对话级状态足够 |
| 数据 fetch | TanStack Query | 缓存 + 重试 + invalidation |
| SSE | 原生 `EventSource` API + 自封装 hook | 不需要 socket.io |
| Markdown | `react-markdown` + `remark-gfm` | 渲染对话 markdown |
| 代码高亮 | `shiki` | 比 react-syntax-highlighter 快，主题切换流畅 |
| 图标 | `lucide-react` | shadcn/ui 默认 |
| 字体 | next/font 加载 Inter + JetBrains Mono | SSR 友好 |

### 5.2 状态管理

**全局状态（Zustand store）**：

```ts
{
  currentSessionId: string,
  sessions: Session[],
  models: Model[],
  settings: Settings,
  ui: {
    sidebarOpen: boolean,
    theme: "dark" | "light",
    view: "chat" | "stats" | "history"
  }
}
```

**消息流（不入全局，组件本地）**：

- ChatView 内部用 `useReducer` 管理当前对话的 messages
- SSE event 流入用 reducer action：`{type: "token", delta}` / `{type: "tool_start", ...}`
- 这避免大量 token event 触发全局 store 更新

### 5.3 关键组件细节

#### `<MessageBubble>`

```tsx
<MessageBubble
  role="assistant" | "user" | "system"
  content={string}      // markdown
  toolCalls={ToolCall[]}
  model?={string}       // assistant 时显示
  streaming?={boolean}  // streaming 时尾部加光标
/>
```

- 用 `react-markdown` 渲染
- 代码块用自定义 component 替换 → 加复制按钮 + Shiki 高亮
- 工具调用 inline 嵌入（不是 sibling，是 markdown 末尾）

#### `<ToolCallCard>`

```tsx
<ToolCallCard
  name={"grep_text"}
  args={{...}}
  output={string | null}     // null 表示 pending
  status={"pending" | "ok" | "error"}
  durationMs={number}
  modelColor={string}
/>
```

- 默认折叠显示一行：`🔧 grep_text "harness" [3 hits, 400ms ✓]`
- 点击展开：显示 args（JSON 高亮）+ output（限制 50 行预览，超出显示"展开全部"）
- pending 时显示 loading dots

#### `<PlannerTimeline>`

```tsx
<PlannerTimeline
  steps={StepRecord[]}        // 复用 feedback/planner_timeline.py 的 StepRecord
  currentStep={number}
/>
```

- 垂直时间线，每个 step 圆点 + 连线
- 圆点颜色：成功绿 / 失败红 / 进行中橙脉冲 / 待定灰
- 已完成 step 点击展开看 output（同 ToolCallCard）

#### `<ModelSelector>`

```tsx
<ModelSelector
  models={Model[]}
  selected={string | string[]}  // compare 模式 array
  compareMode={boolean}
  onSelect={(m) => void}
  onToggleCompare={() => void}
/>
```

- 单选：dropdown
- compare 模式：checkbox list + 显示已选 N 个

### 5.4 键盘快捷键

| 快捷键 | 操作 |
|--------|------|
| `Cmd+Enter` | 发送消息 |
| `Cmd+N` | 新对话 |
| `Cmd+/` | 焦点切到模型选择器 |
| `Cmd+\` | 折叠/展开侧栏 |
| `Cmd+K` | 命令面板（v2 再做） |
| `Cmd+,` | 打开设置 |
| `Esc` | 取消当前请求 / 关闭抽屉 |
| `↑` 在空输入框 | 编辑上一条用户消息 |

---

## 6. 移动端

**MVP 不做移动端**。但 Tailwind 默认响应式，确保：

- < 768px 时侧栏自动折叠
- 输入框、消息气泡随 viewport 缩放
- 触摸友好的最小点击区域 44×44

后续做 PWA：v2 再说。

---

## 7. 实施计划（按 Phase）

> 每个 Phase 有可验证 demo。完成才进下一个。**MVP = Phase 0-5（已拍板范围）**。Phase 6-8 为可选扩展。

### Phase 0：环境准备（30 min） ✅ 2026-05-14 完成

- [x] 创建 `web/` Python 包（`__init__.py` / `server.py` / `routes/health.py`）
- [x] 创建 `frontend/` Next.js 16.2.6 项目（TypeScript + Tailwind + App Router）
- [x] pyproject.toml 加 `[web]` extras：fastapi / uvicorn / sse-starlette
- [x] 装好依赖：`pip install -e ".[web]"` ✅
- [x] FastAPI `/api/health` smoke test 通过（200 + 正确 JSON）
- [ ] shadcn/ui 初始化（推迟到 Phase 2 用到时再装）

**Python interpreter 注意事项**：
用户机器 `python3 = /usr/local/bin/python3`（系统）但 `pip = miniconda`。
后端命令一律用 **miniconda python**：
```bash
/Users/junjieli/miniconda3/bin/python3 -m uvicorn web.server:app --reload --port 8000
```
或在 shell 里 `alias py=/Users/junjieli/miniconda3/bin/python3`。

**Demo**：`pnpm dev` + `uvicorn web.server:app` 双起来 → 浏览器看 Next.js 默认页 + curl 后端 `/api/health` 返回 `{"status": "ok"}`。

### Phase 1：后端 API 骨架（2-3h）

- [ ] `web/server.py` FastAPI app + CORS
- [ ] `web/routes/models.py`：`GET /api/models`
- [ ] `web/routes/chat.py`：`POST /api/chat`（非流式，先跑通）
- [ ] `web/routes/sessions.py`：CRUD + SQLite schema
- [ ] `web/deps.py`：Harness 单例
- [ ] `tests/test_web/`：每个 endpoint 1 个测试

**Demo**：`curl -X POST localhost:8000/api/chat -d '{...}'` 拿到完整模型回答。

### Phase 2：前端对话主页面（3-4h）

- [ ] Layout（Sidebar + Top bar + 主内容）
- [ ] `<MessageBubble>` + markdown 渲染 + Shiki 代码高亮
- [ ] `<ModelSelector>`
- [ ] 输入栏 + Cmd+Enter 发送
- [ ] 调 `/api/chat` 非流式拿响应

**Demo**：浏览器里点新对话，发问题，看到回答，看到工具调用卡片（占位）。

### Phase 3：流式输出 + 工具调用可视化（2-3h）

- [ ] 后端 `/api/chat/stream` + SSE
- [ ] 前端 `useSSE` hook
- [ ] 流式渲染 token
- [ ] `<ToolCallCard>` 实时 pending → ok/error 状态变化
- [ ] `<PlannerTimeline>` 集成（planner 模式时显示）

**Demo**：发"grep harness 然后总结"，看到工具调用 pending → 输出展开 → 模型继续输出。

### Phase 4：Stats Dashboard 嵌入（1h）

- [ ] 后端 `/api/stats?format=html` 返回 `stats_report.render_stats_dashboard()` 的 HTML
- [ ] 前端 Stats 页面 iframe 嵌入
- [ ] 日期范围选择器（query string 透传后端）

**Demo**：点 Stats 按钮，看到嵌入的成本面板 / 模型对比图。

### Phase 5：历史会话浏览（1-2h）

- [ ] 侧栏历史列表（fetch `/api/sessions` + Zustand 缓存）
- [ ] 历史详情只读视图
- [ ] rename / delete / export

**Demo**：上次的对话能从侧栏点开继续。

**MVP 完成节点（Phase 0-5）** —— 已拍板的范围在这里截止。后续 Phase 6-8 为可选扩展。

### Phase 6（可选）：Compare 模式 UI（2h）

- [ ] ModelSelector 切到 compare 模式
- [ ] ChatView 多模型并排展示（2-4 列）
- [ ] 复用 `harness._run_compare()`

**Demo**：选 DeepSeek + Qwen + Doubao，问同一个问题，看到 3 个回答并排 + 各自 cost。

### Phase 7（可选）：设置页（1-2h）

- [ ] Settings view：主题 / 默认模型 / 工具开关 / Permissions 规则只读展示
- [ ] API key 状态显示（不支持 web 写）+ "去 .env 改" 提示
- [ ] 导出 / 重置数据按钮

**Demo**：能切换主题、看到 permissions 规则、看到所有工具列表。

### Phase 8（可选）：部署 + 打包（MVP 后）

- [ ] `Dockerfile`（多 stage：build frontend → 静态资源 + Python 后端）
- [ ] 或：单命令 `codemesh ui` 起一个本地服务 + 自动开浏览器
- [ ] 文档加 README 一段

---

## 8. 工程纪律

### 8.1 测试要求（对齐 CodeMesh CLAUDE.md）

| 层 | 测试方式 |
|----|----------|
| 后端 routes | `httpx.AsyncClient` + FastAPI TestClient；不调真实 API（mock harness） |
| 后端 Harness 集成 | 复用现有 `tests/test_loop.py` 的 `_FakeAdapter` |
| 前端组件 | Vitest + React Testing Library（按需，MVP 可以不写） |
| E2E | Playwright（Phase 4+ 再加） |

**铁律**：后端测试**不允许调真实模型 API**——所有 chat endpoint 测试用 `_FakeAdapter` 注入预设 response。

### 8.2 Commit 规范（沿用 CodeMesh CLAUDE.md）

- 一个 Phase 一个 commit（feat/web: ...）
- Author: Brandoo110 / GPG 关 / 英文标题
- 完成一个 Phase 写 DEVLOG.md 顶部段（背景 / 改动 / commit 范围 / 面试故事 / 还没做）

### 8.3 ADR 钩子

执行完 Phase 3（MVP）后写：

- **ADR-0006: Web UI 技术栈选 FastAPI + Next.js**（解释为什么不是 NestJS / 不是 streamlit / 不是 gradio）

---

## 9. 已确认的决策 + 默认细节

### 9.1 用户拍板（2026-05-14）

- **Q1 技术栈**: ✅ 方案 A（FastAPI + Next.js + shadcn/ui）—— "NestJS"原话是 Next.js 口误
- **Q2 MVP 范围**: ✅ Phase 0-5（含 Stats + 历史浏览，≈11.5h）
- **Q3 部署目标**: ✅ localhost 单用户，无鉴权

### 9.2 沿用默认（无需特别拍板）

- **Q4 主题**: 默认暗色，提供浅色切换
- **Q5 多语言**: 中英混合（UI 文案中文，code/工具名英文）；不做 i18n
- **Q6 移动端**: MVP 不做，响应式预留
- **Q7 历史持久化**: SQLite 本地
- **Q8 Compare 模式**: 标 Optional Phase 6（MVP 不做）

### 9.3 后续可做但 MVP 不做

- 命令面板（Cmd+K）
- 插件市场（Plugins UI）
- ADR 浏览器（在 UI 里读 `docs/decisions/`）
- 文件上传 / 多模态
- 协作（多用户共享对话）
- Vim 模式

---

## 10. 风险 / 已知坑（诚实段，对照 ADR 模板）

| 风险 | 影响 | Mitigation |
|------|------|------------|
| Python async + Next.js SSE 在 streaming 中断时的清理 | 资源泄漏 | 后端用 `request.is_disconnected()` 检查；前端 unmount 时 abort |
| Markdown 渲染 XSS | 用户输入被注入 | `react-markdown` 默认不渲染 raw HTML；用 `rehype-sanitize` 加固 |
| 工具调用 output 巨大（read_file 一个 10000 行文件） | 浏览器卡 | 后端截断 + 加"完整文件下载"链接 |
| 历史对话越攒越大 SQLite 慢 | 后期搜索慢 | MVP 不做搜索；后期加 FTS5 |
| Streaming 中关闭浏览器 tab，模型还在跑 | 浪费 API 调用 | 后端绑定 session_id，cancel 路由 `DELETE /api/chat/{session_id}` |
| Next.js 15+ RSC 和 SSE 配合的坑 | 学习曲线 | SSE 用 client component；RSC 只做静态 layout |
| 单人项目维护 frontend deps 太重 | npm audit / dependabot 噪音 | 锁定 Next.js / shadcn 主版本，季度更新 |

---

## 11. 时间预估

| Phase | 单人投入 | 累计 |
|-------|---------|------|
| 0 | 0.5h | 0.5h |
| 1 后端骨架 | 2.5h | 3h |
| 2 前端主页 | 3.5h | 6.5h |
| 3 流式 + 工具卡 | 2.5h | 9h |
| **MVP 截止** | | **9h** |
| 4 Stats | 1h | 10h |
| 5 历史 | 1.5h | 11.5h |
| 6 Compare | 2h | 13.5h |
| 7 设置 | 1.5h | 15h |
| 8 部署 | 2h | 17h |

**MVP 9h 可分 3 个集中下午做完**。Phase 4-7 各自 1-2h 可零碎做。

---

## 12. 此方案如何被使用

✅ **2026-05-14 已 review 完毕**：Q1-Q3 拍板（方案 A / Phase 0-5 / localhost），ADR-0006 已写。

接下来：

1. ✅ ADR-0006 `docs/decisions/0006-web-ui-stack-fastapi-nextjs.md` 已写
2. ✅ 项目根 `CLAUDE.md` 的 ADR 列表已加 0006
3. 🔄 开 `feature/web-ui` 分支按 §7 Phase 顺序执行：
   - Phase 0: 环境准备
   - Phase 1: 后端 API 骨架
   - Phase 2: 前端对话主页面
   - Phase 3: 流式输出 + 工具调用可视化
   - Phase 4: Stats Dashboard 嵌入
   - Phase 5: 历史会话浏览（MVP 截止）
4. 每完成一个 Phase：commit（feat(web): Phase N - ...）+ 阶段性 push
5. MVP 完成（Phase 5）：merge `feature/web-ui` 到 main，更新 README v5，写 DEVLOG 总段

**Phase 6-8 是可选扩展，MVP 完成后再决定是否做**。
