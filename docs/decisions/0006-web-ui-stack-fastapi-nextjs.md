# ADR-0006：Web UI 技术栈采用 FastAPI + Next.js 而非 NestJS

## Status

Accepted — 2026-05-14（用户 review 设计方案 v1 后确认）。

## Context

v4 末决定给 CodeMesh 加 Web UI，目标是让对话 / 流式输出 / 工具调用可视化 / Stats dashboard 在浏览器里更直观。CLI 已经覆盖功能，UI 比 CLI 强的就 3 处：

1. 流式输出 / 工具调用 / planner timeline 的实时呈现
2. 跨会话历史浏览（翻 ADR / 找历史素材）
3. Stats dashboard 嵌入

用户初问"技术用 NestJS"——但 **NestJS 是 Node 后端框架**，不能直接做 UI 渲染。UI（浏览器 HTML/CSS/JS）必须搭配前端框架。

结合 CodeMesh 是 **Python 项目**（harness.py / orchestration / execution / feedback / memory），评估三种技术栈：

### 方案 A：FastAPI（Python）+ Next.js + shadcn/ui

```
浏览器（Next.js）→ FastAPI（Python，同进程）→ harness.py
```

直接 `from harness import Harness`，单语言后端，异步原生匹配。

### 方案 B：NestJS（Node BFF）+ FastAPI（Python）+ Next.js

```
浏览器 → NestJS → FastAPI → harness
```

多一层 BFF。NestJS 在这里做的事和 FastAPI 重复（转发 / 鉴权 / 日志），单人项目过度工程。

### 方案 C：NestJS spawn Python subprocess

```
浏览器 → NestJS → child_process.spawn("python", ["-m", "codemesh"]) → 新 Python 进程
```

每次任务 spawn 新 Python 进程，**冷启动 1-3s**；CodeMesh 的 memory 7 层 / hooks / observer 跨进程失效。工程上最差。

### 同时评估的替代赛道

| 方案 | 否决理由 |
|------|---------|
| Streamlit | 上手最快但样式难定制，做不出 Claude 简洁风 |
| Gradio | 同 Streamlit；且 7 层 memory 难嵌入 |
| 纯前端 + CLI 调用 | 交互受限，做不了 SSE streaming |

## Decision

**采用方案 A：FastAPI + Next.js 15 (App Router) + shadcn/ui + Tailwind CSS**

**后端栈**：

- FastAPI（同进程 `from harness import Harness`）
- sse-starlette（处理 SSE 流）
- 复用现有 `feedback/call_log.py` 的 SQLite + jsonl 存储

**前端栈**：

- Next.js 15 App Router
- shadcn/ui（Claude 简洁风的等价开源）
- Tailwind CSS
- Zustand（状态）
- TanStack Query（数据 fetch + 缓存）
- Shiki（代码高亮）
- react-markdown + remark-gfm

**MVP 范围**：Phase 0-5（环境准备 / 后端骨架 / 前端主页 / SSE 流式 / Stats 嵌入 / 历史浏览），约 11.5h。

**部署**：localhost 单用户，无鉴权。

完整组件细节 / API spec / Phase 计划见 `docs/ui-design-plan.md`。

## Consequences

### 好处

1. **单语言后端** —— 调试链最短（浏览器 → FastAPI → harness 同进程）
2. **异步原生匹配** —— CodeMesh 本来就是 async，FastAPI 天生匹配
3. **SSE streaming 零成本** —— `StreamingResponse` 直接接 `harness.run_stream()`
4. **7 层 memory / hooks / observer 全在同进程** —— 无跨进程协调
5. **shadcn/ui 提供 Claude 风格的免费等价组件** —— 不用从零写
6. **复用现有 stats dashboard** —— `feedback/stats_report.py` 渲染的 HTML 可以 iframe 嵌入

### 坏处（要诚实）

1. **用户原本想学 NestJS** —— 这个方案没有 NestJS 工程化经验沉淀（DI / Module / Guard / Pipe 这些没机会摸）
2. **单人维护 npm 依赖** —— Next.js / shadcn 长期维护成本（季度更新 / npm audit 噪音）
3. **两套工具链** —— Python（pip）+ Node（pnpm/npm）开发环境
4. **不能完全 SSR** —— SSE streaming 必须用 client component，会牺牲一部分 Next.js 15 RSC 的优势

### Mitigation

- **关于 NestJS 经验**：另起一个独立 Node 项目学 NestJS（比如做一个 Slack/Telegram bot），CodeMesh 不强行套
- **npm 依赖**：`package.json` 锁主版本，季度更新；不用 alpha / canary
- **两套工具链**：用 Makefile / 单命令 `codemesh ui` 启动两个进程；文档化
- **RSC 妥协**：SSE 组件用 `"use client"`，静态 layout 用 RSC

## 参考

- `docs/ui-design-plan.md` — 完整方案 12 章节
- ADR-0001 — Python-first 一致原则（agentic search over RAG）
- shadcn/ui: https://ui.shadcn.com
- FastAPI SSE: https://github.com/sysid/sse-starlette
- Next.js 15 App Router: https://nextjs.org/docs/app

## 相关 ADR

- ADR-0001（agentic search） —— 同样 Python-first 决策
- ADR-0002（HTML for humans） —— Web UI 是 HTML 给人看原则的延伸场景
