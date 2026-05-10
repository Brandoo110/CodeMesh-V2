# ADR-0003：Dreaming 4 阶段巩固循环

## Status

Accepted — 2026-05-09（v4 dreaming 功能落地）。**注意**：开发期实际很难真触发（24h + 5 sessions 双门控），被面试官问"跑过几次"要诚实回答。

## Context

2026-05 调研 Anthropic 的 [Dreaming research preview](https://www.anthropic.com)，看到他们在 cli.js 里已经有：

- `DreamTask` 类
- `auto_dream` 触发逻辑
- `tengu_auto_dream_*` 一系列 telemetry event

这是 Anthropic 给 Claude Code 加的"长期记忆巩固"机制——类似人睡眠时大脑整理白天经历。本地 Claude Code v2.1.92 cli.js 反编译确认这套已经落地（不是 vaporware）。

CodeMesh 当时已经有 7 层 memory（见 ADR-0004），但缺的是**"整理已记的事"** 这层。L5（auto_extract）只负责"记新事"——抓取 session 中的 facts 写入；没有任何机制把多个 session 的 facts 合并、去重、抽象。

如果不做巩固：
- L5 facts 会越积越多，重复 / 矛盾 / 过期
- 检索时 noise > signal
- 不符合 Anthropic 路线（教学和讲述价值低）

## Decision

复刻 80 行 Python 版 dreaming，**4 阶段流水线**：

```
L5 (raw facts) → SCAN → CONSOLIDATE → INDEX → REPORT → L6 (dreams)
```

| 阶段 | 干啥 | 实现 |
|------|------|------|
| **SCAN** | 扫描 L5 所有 session journals + auto-extracted facts | `feedback/dreamer.py:scan()` |
| **CONSOLIDATE** | LLM call 把同主题 facts 合并成 abstract dream（去重 / 抽象 / 矛盾解决） | `feedback/dreamer.py:consolidate()` |
| **INDEX** | 给 dream 打主题 tag，方便后续按主题检索 | `feedback/dreamer.py:index()` |
| **REPORT** | 写 markdown 报告进 `.codemesh/dreams/<ts>.md`（人看的） | `feedback/dreamer.py:report()` |

**触发条件（双门控）**：

1. 距上次 dream **>= 24 小时**
2. 距上次 dream 之间至少有 **5 个 session**

两个条件 AND，缺一个不触发。

**为什么是 dream 不是 summary**：

- Summary 是"压缩"，dream 是"重构"
- Dream 允许**抽象**（多个具体 facts → 一条规律）
- Dream 是**异步后台任务**，不阻塞用户当前 session
- 名字直接对齐 Anthropic 命名，便于面试时讲述

## Consequences

### 好处

1. **L5 不爆炸**：facts 定期被 dream 吸收，长期质量稳定
2. **检索 signal 提升**：dream 是抽象层，比 raw fact 更有用
3. **教学价值**：人睡眠 → 大脑巩固记忆这个隐喻好讲
4. **路线对齐 Anthropic**：v4 不是闭门造车

### 坏处（要诚实）

1. **开发期实际很难真触发** —— 24h + 5 sessions 这种门控在 dev 环境基本撞不上
2. **测试覆盖靠 mock** —— 18 个单测都用 mock 时间和 fake LLM call，**没有 end-to-end 真跑过**
3. **生产价值未验证** —— 用户跑过几次才有效果，目前 0 真实数据
4. **如果被问"跑过几次"** —— 必须诚实回答 "开发期没真触发；逻辑用 mock 验证"，**不要装作在生产跑过**

### Mitigation

- `feedback/dreamer.py` 提供 `force_dream()` 方法绕过双门控，给开发期 / demo 用
- 18 个单测覆盖每阶段逻辑（SCAN / CONSOLIDATE / INDEX / REPORT 独立 + 端到端 mock）
- `CLAUDE.md` 项目级写明此为 **v4 加得最快但故事最虚的一块**
- 长期方案：等用户回流真触发后补 e2e 测试

## 参考

- Anthropic Claude Code v2.1.92 `cli.js`（本地反编译，DreamTask / auto_dream / tengu_auto_dream_*）
- `Wiki/Claude Code/Claude-Code-Dreaming.md`（特性 + 启用方式调研笔记）
- `feedback/dreamer.py` 实现（80 行）
- `tests/test_dreamer.py`（18 单测）

## 相关 ADR

- ADR-0004（Memory 7 层）—— L5 → L6 这条数据流的上下文
- ADR-0002（HTML 给人看）—— dream report 也是 markdown，agent 不消费
