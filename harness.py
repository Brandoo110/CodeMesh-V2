"""
Harness 顶层组装
==================

【这个文件的角色】
Harness 一词来自马具"挽具"——把马和车连起来的装置。
在 Agent 领域就是把"模型 + 工具 + 记忆 + 反馈 + 编排"组装成系统的壳。

【四层协作流程】
    任务进入
       ↓
    [反馈]  observer.start_trace
       ↓
    [RAG]   可选前置检索 → 拼 context
       ↓
    [编排]  router.route() 决定用哪个模型 + complexity
       ↓
    simple  → [执行] 单轮 streaming 输出（不走 agent loop，更快更便宜）
    complex → [编排] planner.plan() 拆步骤
              → 每步 [执行] run_agent_loop（可能调工具）
       ↓
    [反馈]  observer.log_llm_call（带真实 token）
    [反馈]  observer.end_trace
       ↓
    [成本]  compute_cost 汇总

【四个新能力】
1. 成本追踪：每个 adapter 暴露 last_usage，compute_cost 计算 ¥
2. Streaming：simple 任务走 complete_stream，实时吐字
3. Planner：complex 任务先拆步骤再执行
4. RAG：代码库检索结果前置塞进 system prompt
"""

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from memory import (
    ShortTermMemory,
    WorkingMemory,
    LongTermMemory,
    get_default_long_term,
    set_default_long_term,
)
from execution import run_agent_loop
from orchestration import (
    route,
    plan,
    TaskPlan,
    HookRegistry,
    HookEvent,
    make_default_logging_hooks,
    load_skill_registry,
    SkillRegistry,
    PermissionRegistry,
    make_default_permissions,
    make_permission_hook,
    load_plugins,
)
from execution import set_skill_registry
from orchestration.hooks import HookResult
from orchestration.adapters import (
    DeepSeekAdapter,
    DashScopeAdapter,
    VolcEngineAdapter,
    GeminiAdapter,
    ModelAdapter,
)
from feedback import (
    Observer,
    compute_cost,
    CallCost,
    log_call,
    Dreamer,           # = SessionJournal alias（per-session 叙事 L5 变体）
    RealDreamer,       # 真 L6 dreaming（4 阶段巩固）
    AutoCompactState,
    auto_compact_if_needed,
    StepRecord,
    maybe_write_plan,
)
from memory import extract_and_save, DEFAULT_AUTO_MEMORY_DIR
from rag import retrieve
from rag.retriever import format_context


load_dotenv()


SYSTEM_PROMPT = """你是 CodeMesh，一个面向中文开发者的代码助手。
用中文回复。遇到需要查文件/跑命令的任务，请主动使用提供的工具。
工具调用要精准：路径不瞎猜，命令不写危险操作（rm -rf、sudo 等会被拒绝）。
"""


class Harness:
    """Harness 组装类：持有四层实例，暴露 run / compare / run_stream / run_plan 方法。"""

    def __init__(
        self,
        enable_logging_hooks: bool = True,
        use_rag: bool = False,
        enable_memory_compression: bool = True,
        enable_dreaming: bool = True,
    ):
        # 记忆层（可选开启 LLM 摘要压缩，避免长对话直接丢历史）
        # 触发条件双管齐下：
        #   消息数 >= 15  或  累计 token 数 >= 6000
        # 两条都监控是为了应对"少而长"和"多而短"两种用法，不让任何一种悄悄爆 context。
        self.enable_memory_compression = enable_memory_compression
        if enable_memory_compression:
            self.short_term = ShortTermMemory(
                max_messages=20,
                compress_threshold=15,
                summarizer=self._summarize,
                token_budget=6000,
            )
        else:
            self.short_term = ShortTermMemory(max_messages=20)
        self.short_term.set_system(SYSTEM_PROMPT)
        self.working = WorkingMemory()
        # 长期记忆走模块级单例：tools (remember_fact / recall_facts / forget_fact)
        # 和 Harness 共用同一份 SQLite，避免双写不一致
        self.long_term = get_default_long_term()
        set_default_long_term(self.long_term)

        # 反馈层
        self.observer = Observer()

        # 编排层
        self.hooks = HookRegistry()
        if enable_logging_hooks:
            make_default_logging_hooks(self.hooks)

        # 权限层：默认规则集（force-push / pip install / 系统路径写入...），
        # 通过 PreToolUse hook 在工具执行前检查。用户可以通过 self.permissions
        # 在 init 之后追加 / 修改规则。
        self.permissions: PermissionRegistry = make_default_permissions()
        self.hooks.register(HookEvent.PRE_TOOL_USE, make_permission_hook(self.permissions))

        # RAG 开关（需要先跑 `codemesh index` 建索引才有效）
        self.use_rag = use_rag

        # Skills：扫 .claude/skills/ + ~/.codemesh/skills/，把名字+描述塞进 system prompt
        # invoke_skill 工具使用同一个 registry 加载 SKILL.md 全文
        self.skills: SkillRegistry = load_skill_registry(project_root=Path("."))
        set_skill_registry(self.skills)

        # Plugins：扫 .claude/plugins/ + ~/.codemesh/plugins/，import + 调 register(harness)
        # 让插件能往 hooks / permissions / tools / skills 任一处叠加
        # （插件失败不阻断启动；warning 已经在 plugins.py 里打了）
        self.plugins = load_plugins(harness=self, project_root=Path("."))

        # 每次 run 累计的成本
        self.last_costs: list[CallCost] = []

        self._adapters: dict[str, ModelAdapter] = {}

        # 记忆层第二阶段对齐 OpenHarness（2026-05-09）：分两个互补层
        #
        # 1) SessionJournal（旧名 Dreamer）——L5 叙事变体
        #    每会话末写一条 4 段式 markdown，下次召回拼 system prompt
        #    默认 min_hours=0、min_sessions=1（让每会话都能写）
        #
        # 2) RealDreamer ——真 L6 dreaming（CC 同款 4 阶段 consolidation）
        #    扫 auto_memory/ → grep 信号 → LLM 给 plan → 机械执行 + 重建索引
        #    默认 24h / 5 sessions（稀疏触发，避免反复调贵的整理 LLM）
        #
        # 两者共享 .consolidate-lock 文件锁，避免同时改记忆库
        self.enable_dreaming = enable_dreaming
        self.session_journal = (
            Dreamer(summarizer=self._dream_summarize, min_hours=0, min_sessions=1)
            if enable_dreaming else None
        )
        self.real_dreamer = (
            RealDreamer(summarizer=self._dream_summarize)
            if enable_dreaming else None
        )
        # 老代码兼容：harness.dreamer 仍可用，指向 session_journal
        self.dreamer = self.session_journal

        # Auto memory extraction（2026-05-09 新增）：会话结束后 LLM 抽取跨会话事实。
        # 4 类型 user/feedback/project/reference + Why/How 双段模板，对齐 CC source map。
        self.enable_auto_extract = enable_dreaming  # 复用同一开关
        self.auto_memory_dir = DEFAULT_AUTO_MEMORY_DIR

        # AutoCompactState：跨 query loop 的压缩状态机（2026-05-09 新增）。
        # 跟踪 consecutive_failures，连续 3 次失败后停止自动压缩防止坏 LLM 反复浪费钱。
        self.auto_compact_state = AutoCompactState()

        # v5 Phase 6.4：工具白名单（每 step 可独立设置）
        # None / ["*"] = 全开（兼容现有 run / run_stream_full 行为）
        # [] = 完全禁用（纯文本生成步骤）
        # ["grep_text", "read_file"] = 显式白名单
        self.tool_allowlist: "list[str] | None" = None

        # v5 Phase 6.4：preferred_model — 编排器强制指定的模型（绕开 router）。
        # None = 走 router 自动决策（chat 行为不变）。
        # 设了非空字符串 = 所有 route() 都被 short-circuit 成这个 model。
        self.preferred_model: "str | None" = None

    def set_tool_allowlist(self, allowlist: "list[str] | None") -> None:
        """v5：编排器在每 step 创建临时 harness 后调用，filter agent loop 的工具。"""
        self.tool_allowlist = allowlist

    def set_preferred_model(self, model: "str | None") -> None:
        """v5：编排器在每 step 创建临时 harness 后调用，强制指定模型（绕 router）。"""
        self.preferred_model = model

    async def _decide_route(self, task: str):
        """
        Router 决策的统一入口。

        如果 preferred_model 已设置（工作流场景），就 short-circuit 返回一个
        RouteDecision，跳过 LLM 决策。否则照常走 route()。

        complexity 推断规则（preferred_model 模式下）：
          - tool_allowlist == [] → simple（纯文本步骤）
          - 其他（None / 数组）→ complex（agent loop，可调工具）
        """
        if self.preferred_model:
            from orchestration.router import RouteDecision
            complexity = "simple" if self.tool_allowlist == [] else "complex"
            return RouteDecision(
                model=self.preferred_model,
                complexity=complexity,
                reason=f"workflow override (tool_allowlist={self.tool_allowlist})",
            )
        return await route(task)

    async def _ensure_long_term(self) -> None:
        await self.long_term.init()

    def _get_adapter(self, name: str) -> ModelAdapter:
        """
        拿适配器。如果目标厂商 key 未配置但 GEMINI_API_KEY 在，就透明降级到 Gemini。
        这样学习/演示阶段只需一个 Gemini key 即可跑通全部四层。
        """
        if name in self._adapters:
            return self._adapters[name]

        native_key_env = {
            "deepseek": "DEEPSEEK_API_KEY",
            "qwen":     "DASHSCOPE_API_KEY",
            "doubao":   "VOLC_API_KEY",
        }.get(name)

        # 真实 key 一般 30+ 字符；设阈值 20 过滤常见占位符
        def _valid(v: str) -> bool:
            return len(v.strip()) >= 20
        has_native = _valid(os.getenv(native_key_env, "")) if native_key_env else False
        has_gemini = _valid(os.getenv("GEMINI_API_KEY", ""))

        if not has_native and has_gemini and name != "gemini":
            print(f"[harness] fallback: {name} → gemini（未检测到 {native_key_env}）")
            adapter: ModelAdapter = GeminiAdapter()
        else:
            match name:
                case "deepseek": adapter = DeepSeekAdapter()
                case "qwen":     adapter = DashScopeAdapter()
                case "doubao":   adapter = VolcEngineAdapter()
                case "gemini":   adapter = GeminiAdapter()
                case _:
                    print(f"[harness] unknown model {name!r}, fallback to deepseek")
                    adapter = DeepSeekAdapter()
        self._adapters[name] = adapter
        return adapter

    # ─────────────── 记忆压缩 ───────────────

    async def _summarize(self, messages: list[dict]) -> str:
        """
        把一批旧消息压缩成中文摘要。用 doubao（最便宜）做这件事。
        失败时退回一个简陋的字符串摘要，保证记忆链不断。
        """
        adapter = self._get_adapter("doubao")
        # 把消息线性化成可读文本喂给模型
        joined = "\n".join(
            f"{m.get('role', '?')}: {m.get('content', '')}" for m in messages
        )
        prompt = (
            "请把下面这段多轮对话压缩成不超过 200 字的中文摘要，"
            "保留任务关键信息、已确定的事实和未解决的问题。直接输出摘要正文，不要解释。\n\n"
            f"{joined}"
        )
        try:
            text = await adapter.complete(
                messages=[{"role": "user", "content": prompt}],
                system="你是一个对话摘要助手。",
            )
            self._record_cost(adapter)
            return text.strip()
        except Exception as e:
            print(f"[memory] summarize failed ({type(e).__name__}); fallback to head/tail")
            # 兜底：最早 1 句 + 最近 1 句拼起来，至少不丢锚点
            head = (messages[0].get("content") or "")[:80] if messages else ""
            tail = (messages[-1].get("content") or "")[:80] if messages else ""
            return f"早期: {head} ... 近期: {tail}"

    # ─────────────── Dreaming：会话复盘 ───────────────

    async def _dream_summarize(self, prompt: str) -> str:
        """
        Dreamer 用的 summarizer。和 _summarize 的差别：
          - _summarize    吃 messages 列表（短期记忆压缩用）
          - _dream_summarize 吃已经构造好的整段 prompt（dream 用，结构化提示）
        共用 doubao adapter（最便宜，会话末尾这一刀不能贵）。
        """
        adapter = self._get_adapter("doubao")
        text = await adapter.complete(
            messages=[{"role": "user", "content": prompt}],
            system="你是一个工程笔记助手。",
        )
        self._record_cost(adapter)
        return text.strip()

    async def _maybe_journal(self, task: str, output: str) -> None:
        """
        会话结束钩子：写一条叙事日志（SessionJournal）。
        失败 / 5 门未通过 / 关闭都静默 —— 不影响主回复。
        """
        if not self.enable_dreaming or self.session_journal is None:
            return
        try:
            path = await self.session_journal.dream(task=task, output=output)
            if path:
                print(f"[journal] saved → {path.name}")
        except Exception as e:
            print(f"[journal] failed ({type(e).__name__}: {e}); skip")

    async def _maybe_real_dream(self) -> None:
        """
        真 L6 dreaming：跑 4 阶段 consolidation 整理 auto_memory/。
        默认 24h / 5 sessions 才触发，所以大部分会话不会动 LLM 成本。
        """
        if not self.enable_dreaming or self.real_dreamer is None:
            return
        try:
            await self.real_dreamer.dream()
        except Exception as e:
            print(f"[real-dreamer] failed ({type(e).__name__}: {e}); skip")

    # 旧名兼容：harness._maybe_dream 仍然只跑 journal（不破坏外部调用）
    async def _maybe_dream(self, task: str, output: str) -> None:
        await self._maybe_journal(task, output)

    async def _maybe_extract_memories(self, task: str, output: str) -> None:
        """
        会话结束钩子：调 LLM 抽取跨会话事实，按 4 类型 + Why/How 模板写盘。
        失败静默 —— 这是"锦上添花"层，不影响主回复。
        """
        if not self.enable_auto_extract:
            return
        try:
            paths = await extract_and_save(
                task=task,
                output=output,
                summarizer=self._dream_summarize,   # 复用 doubao 摘要器
                memory_dir=self.auto_memory_dir,
            )
            if paths:
                print(f"[auto_extract] saved {len(paths)} memory entries")
        except Exception as e:
            print(f"[auto_extract] failed ({type(e).__name__}: {e}); skip")

    async def _maybe_autocompact_short_term(self) -> None:
        """
        Query loop 前置钩子：检查 short_term 是否需要自动压缩。
        先 microcompact（cheap）→ 还不够才 full compact（expensive）。
        """
        # 拿 short_term 当前完整 messages 列表
        messages = self.short_term.get_messages()
        new_messages, was_compacted = await auto_compact_if_needed(
            messages,
            summarizer=self._dream_summarize,
            state=self.auto_compact_state,
        )
        if was_compacted:
            print("[compactor] auto-compact applied")
            # 把压缩后的 messages 写回 short_term
            # short_term 没有 set_messages 接口，简化做法：clear + 逐条 add
            self.short_term.clear()
            for m in new_messages:
                role = m.get("role", "user")
                content = m.get("content", "")
                if role == "system":
                    # system 由 set_system 管理；compact 后的 summary 用 user role 已是约定
                    continue
                if isinstance(content, str):
                    self.short_term.add(role, content)

    # ─────────────── 成本记账 ───────────────

    def _record_cost(self, adapter: ModelAdapter) -> CallCost:
        """从 adapter.last_usage 取 token，算成本，加到本轮 last_costs；同时落本地日志。"""
        u = adapter.last_usage
        cost = compute_cost(adapter.name, u.prompt_tokens, u.completion_tokens)
        self.last_costs.append(cost)
        # 写本地 jsonl 日志，stats 子命令读这个文件聚合
        log_call(
            model=adapter.name,
            tokens_in=cost.tokens_in,
            tokens_out=cost.tokens_out,
            cost_rmb=cost.cost_rmb,
            task=getattr(self, "_current_task", None),
        )
        return cost

    # ─────────────── RAG 前置 ───────────────

    async def _load_long_term_block(self) -> str:
        """
        把长期记忆的全部 KV 渲染成一段 system 内容。
        没有任何事实时返回空串。

        这样设计的好处：模型每次进 run() 自动看到"它过去记下来的事"，
        不需要主动调 recall_facts。tools 仍然可用（remember_fact / forget_fact）
        来动态写入。
        """
        try:
            facts = await self.long_term.list_all()
        except Exception:
            return ""
        if not facts:
            return ""
        lines = "\n".join(f"  - {k}: {v}" for k, v in facts.items())
        return (
            "\n\n<previously remembered facts>\n"
            f"{lines}\n"
            "</previously remembered facts>"
        )

    async def _build_system_with_context(self, task: str) -> str:
        """
        组装 system prompt：
          基础 SYSTEM_PROMPT
            + 长期记忆事实（如果有）
            + Dream 召回（如果有，过往相似任务的复盘笔记）
            + 可用 skills 索引（如果有，让模型知道有哪些 SKILL.md 可 invoke_skill）
            + RAG 检索片段（如果开了 --rag 且 query 命中）
        """
        system = SYSTEM_PROMPT + await self._load_long_term_block()

        # Dream 前置：从历史 dreams/ 里 grep 出最相关的 top-3，拼到 system。
        # 这是"agent 越用越聪明"的关键：上次的踩坑、下次自动看到。
        if self.dreamer is not None:
            try:
                hits = self.dreamer.recall(task, top_k=3)
                ctx = self.dreamer.format_context(hits)
                if ctx:
                    system += "\n\n" + ctx
            except Exception as e:
                print(f"[dreamer] recall failed ({type(e).__name__}: {e}); skip")

        skill_index = self.skills.render_index()
        if skill_index:
            system += "\n\n" + skill_index
        if not self.use_rag:
            return system
        hits = await retrieve(task, top_k=5)
        if not hits:
            return system
        # 用 token 预算而非字符数：中英混排时更精确，避免超 context 上限
        ctx = format_context(hits, max_tokens=2000)
        return system + "\n\n以下是可能相关的代码片段，优先参考:\n" + ctx

    # ─────────────── 主入口：自动分流 ───────────────

    async def run(self, task: str) -> str:
        """
        主入口。根据 router 决策：
          simple  → 单轮 streaming（不打印，返回完整字符串）
          complex → planner → executor loop
        """
        await self._ensure_long_term()
        self.last_costs = []
        self._current_task = task   # 让 _record_cost 把它写进 jsonl 日志
        self.observer.start_trace(task)
        self.hooks.trigger(HookEvent.SESSION_START, task=task)
        self.hooks.trigger(HookEvent.USER_PROMPT_SUBMIT, prompt=task)

        decision = await self._decide_route(task)
        print(
            f"[router] model={decision.model} "
            f"complexity={decision.complexity} reason={decision.reason}"
        )

        self.working.update(task_description=task, step=0)
        self.short_term.add("user", task)
        # query 前置：检查是否需要自动压缩 short_term（micro/full）
        await self._maybe_autocompact_short_term()
        system = await self._build_system_with_context(task)
        adapter = self._get_adapter(decision.model)

        t0 = time.perf_counter()
        if decision.complexity == "simple":
            answer = await self._run_single_turn(adapter, task, system)
        elif self.preferred_model:
            # v5 工作流场景：每个 step 已是最小单位，跳过 planner 二次拆分，
            # 直接进 agent loop。planner 输出 "【步骤 N】..." 在工作流里没意义。
            #
            # 同时把 tool_call 桥接到 harness.hooks——这样 _stream_complex 的
            # PRE/POST_TOOL_USE 监听才能拿到 tool 事件转 SSE。
            def _hook_bridge(name: str, args: dict, result: str) -> None:
                self.hooks.fire_pre(name, args)
                self.hooks.fire_post(name, result)

            answer = await run_agent_loop(
                adapter=adapter,
                messages=[{"role": "user", "content": task}],
                system=system,
                on_tool_call=_hook_bridge,
                tool_allowlist=self.tool_allowlist,
            )
            # record costs from the agent loop
            self._record_cost(adapter)
        else:
            answer = await self._run_planned(task, system, decision.model)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        self.short_term.add("assistant", answer)
        # 长对话压缩：达到阈值时把最旧一半消息交给 doubao 总结成 summary
        if self.enable_memory_compression:
            compressed = await self.short_term.maybe_compress()
            if compressed:
                print("[memory] compressed older messages into summary")
        self.observer.log_llm_call(
            model=decision.model,
            tokens_in=sum(c.tokens_in for c in self.last_costs),
            tokens_out=sum(c.tokens_out for c in self.last_costs),
            latency_ms=elapsed_ms,
            route_reason=decision.reason,
        )
        self.observer.end_trace(success=True, output=answer)
        self.hooks.trigger(HookEvent.STOP, task=task, output=answer)
        self.hooks.trigger(HookEvent.SESSION_END, success=True, task=task)
        # 三个会话结束钩子：
        #   1. journal: 写叙事 markdown（高频，每会话）
        #   2. extract_memories: 抽 4 类结构化事实（高频，每会话）
        #   3. real_dream: 整理 auto_memory/ 4 阶段巩固（稀疏，~24h 一次）
        await self._maybe_journal(task, answer)
        await self._maybe_extract_memories(task, answer)
        await self._maybe_real_dream()
        return answer

    async def _run_single_turn(
        self, adapter: ModelAdapter, task: str, system: str
    ) -> str:
        """simple 任务：单轮调用，走流式拿完整文本 + usage。"""
        buf: list[str] = []
        async for chunk in adapter.complete_stream(
            messages=[{"role": "user", "content": task}],
            system=system,
        ):
            buf.append(chunk)
        self._record_cost(adapter)
        return "".join(buf)

    async def _run_planned(self, task: str, system: str, default_model: str) -> str:
        """complex 任务：planner 拆步骤，逐步 executor。"""
        task_plan = await plan(task)
        print(f"[planner] {task_plan.summary}")
        for i, step in enumerate(task_plan.steps, 1):
            print(f"  step {i}/{len(task_plan.steps)}: [{step.suggested_model}] {step.description}")

        # 逐步执行：每一步跑一次 agent loop
        # 同时收集 timeline 数据，env CODEMESH_HTML_PLAN=1 时落盘成可视化 HTML
        results: list[str] = []
        step_records: list[StepRecord] = []
        for i, step in enumerate(task_plan.steps, 1):
            adapter = self._get_adapter(step.suggested_model)

            def on_tool_call(name: str, args: dict, result: str) -> None:
                self.hooks.fire_pre(name, args)
                self.hooks.fire_post(name, result)
                self.working.update(step=self.working.step + 1)

            t0 = time.monotonic()
            err_msg = ""
            text = ""
            try:
                if step.needs_tools:
                    # 需要工具 → 跑完整 agent loop
                    text = await run_agent_loop(
                        adapter=adapter,
                        messages=[{"role": "user", "content": step.description}],
                        system=system,
                        on_tool_call=on_tool_call,
                        tool_allowlist=self.tool_allowlist,  # v5
                    )
                else:
                    # 纯思考 → 单轮 stream 省钱省时
                    buf: list[str] = []
                    async for chunk in adapter.complete_stream(
                        messages=[{"role": "user", "content": step.description}],
                        system=system,
                    ):
                        buf.append(chunk)
                    text = "".join(buf)
            except Exception as e:
                err_msg = f"{type(e).__name__}: {e}"
                text = f"[ERROR] {err_msg}"

            duration_ms = (time.monotonic() - t0) * 1000
            cost = self._record_cost(adapter)
            step_records.append(StepRecord(
                n=i,
                description=step.description,
                suggested_model=step.suggested_model,
                needs_tools=step.needs_tools,
                status="error" if err_msg else "done",
                output=text,
                duration_ms=duration_ms,
                cost_rmb=cost.cost_rmb,
                error=err_msg,
            ))
            results.append(f"【步骤 {i}】{step.description}\n{text}")

        # 可选：env CODEMESH_HTML_PLAN=1 时把 plan timeline 落盘到 .codemesh/plans/
        # 失败静默——锦上添花层不影响主返回
        try:
            plan_path = maybe_write_plan(
                task=task,
                summary=task_plan.summary,
                steps=step_records,
            )
            if plan_path is not None:
                print(f"[plan-html] saved → {plan_path}")
        except Exception:
            pass

        return "\n\n".join(results)

    # ─────────────── 流式对外接口（CLI 直接用）───────────────

    async def run_stream(self, task: str):
        """
        对外流式接口。yield 文本 chunk。只支持 simple 任务。
        complex 任务走 run() 获得完整结果即可。
        """
        await self._ensure_long_term()
        self.last_costs = []
        self._current_task = task
        self.observer.start_trace(task)
        self.hooks.trigger(HookEvent.SESSION_START, task=task)
        self.hooks.trigger(HookEvent.USER_PROMPT_SUBMIT, prompt=task)

        decision = await self._decide_route(task)
        print(
            f"[router] model={decision.model} "
            f"complexity={decision.complexity} reason={decision.reason}"
        )
        adapter = self._get_adapter(decision.model)
        system = await self._build_system_with_context(task)
        self.short_term.add("user", task)
        # query 前置：检查是否需要自动压缩 short_term（v2 升级用 compactor 模块）
        await self._maybe_autocompact_short_term()

        full_buf: list[str] = []
        async for chunk in adapter.complete_stream(
            messages=[{"role": "user", "content": task}],
            system=system,
        ):
            full_buf.append(chunk)
            yield chunk
        cost = self._record_cost(adapter)
        full = "".join(full_buf)
        self.short_term.add("assistant", full)
        if self.enable_memory_compression:
            compressed = await self.short_term.maybe_compress()
            if compressed:
                print("[memory] compressed older messages into summary")
        self.observer.log_llm_call(
            model=adapter.name,
            tokens_in=cost.tokens_in,
            tokens_out=cost.tokens_out,
            latency_ms=0,
            route_reason=decision.reason,
        )
        self.observer.end_trace(success=True, output=full)
        self.hooks.trigger(HookEvent.STOP, task=task, output=full)
        self.hooks.trigger(HookEvent.SESSION_END, success=True, task=task)
        await self._maybe_journal(task, full)
        await self._maybe_extract_memories(task, full)
        await self._maybe_real_dream()

    # ─────────────── 流式 + 结构化事件（Web UI Phase 3 / ADR-0006）───────────────

    async def run_stream_full(self, task: str, model_override: "str | None" = None):
        """
        完整流式接口：yield 结构化字典事件。给 Web UI SSE 接 (web/routes/chat_stream.py)。

        v5 Phase 6 修复：
          - 加 model_override 参数。编排器在工作流场景每步指定 model 时传入，
            绕开 router 决策——这是 v5 "每步选不同模型" 卖点的真正落地。
          - 同时绕 complexity 决策：如果 tool_allowlist == [] 强制 simple（纯文本）；
            否则按 tools 可见性走 complex（agent loop）。
        """
        if model_override:
            # 编排器明确指定了 model：跳过 router；用 tool_allowlist 推断 complexity。
            # tool_allowlist=[] → simple（无工具，纯文本生成）
            # tool_allowlist=None/[...] → complex（agent loop）
            complexity = "simple" if self.tool_allowlist == [] else "complex"
            model = model_override
            reason = f"workflow step override (allowlist={self.tool_allowlist})"
            print(f"[router/stream] OVERRIDE model={model} complexity={complexity}")
        else:
            decision = await self._decide_route(task)
            print(
                f"[router/stream] model={decision.model} "
                f"complexity={decision.complexity} reason={decision.reason}"
            )
            model = decision.model
            complexity = decision.complexity
            reason = decision.reason

        try:
            if complexity == "simple":
                async for ev in self._stream_simple(task, model, reason):
                    yield ev
            else:
                async for ev in self._stream_complex(task, model):
                    yield ev
        except Exception as e:
            yield {"type": "error", "data": {"message": f"{type(e).__name__}: {e}"}}
            return

        yield {"type": "done", "data": {}}

    async def _stream_simple(self, task: str, model_name: str, route_reason: str):
        """Simple 任务流式：adapter.complete_stream 透出 token + 末尾 usage。"""
        await self._ensure_long_term()
        self.last_costs = []
        self._current_task = task
        self.observer.start_trace(task)
        self.hooks.trigger(HookEvent.SESSION_START, task=task)
        self.hooks.trigger(HookEvent.USER_PROMPT_SUBMIT, prompt=task)

        adapter = self._get_adapter(model_name)
        system = await self._build_system_with_context(task)
        self.short_term.add("user", task)
        await self._maybe_autocompact_short_term()

        full_buf: list[str] = []
        async for chunk in adapter.complete_stream(
            messages=[{"role": "user", "content": task}],
            system=system,
        ):
            full_buf.append(chunk)
            yield {"type": "token", "data": {"delta": chunk}}

        cost = self._record_cost(adapter)
        full = "".join(full_buf)
        self.short_term.add("assistant", full)
        if self.enable_memory_compression:
            await self.short_term.maybe_compress()

        yield {
            "type": "usage",
            "data": {
                "prompt": cost.tokens_in,
                "completion": cost.tokens_out,
                "cost_rmb": round(cost.cost_rmb, 4),
                "model": adapter.name,
            },
        }

        self.observer.log_llm_call(
            model=adapter.name,
            tokens_in=cost.tokens_in,
            tokens_out=cost.tokens_out,
            latency_ms=0,
            route_reason=route_reason,
        )
        self.observer.end_trace(success=True, output=full)
        self.hooks.trigger(HookEvent.STOP, task=task, output=full)
        self.hooks.trigger(HookEvent.SESSION_END, success=True, task=task)
        await self._maybe_journal(task, full)
        await self._maybe_extract_memories(task, full)
        await self._maybe_real_dream()

    async def _stream_complex(self, task: str, model_name: str):
        """
        Complex 任务：跑 run() 同时 hook 中转 tool events。

        实现：
          1. 注册 PRE/POST_TOOL_USE callbacks 写入 internal asyncio.Queue
          2. 启动 self.run(task) 作为后台 task
          3. 主循环 wait_for(queue.get, timeout=0.1) 中转 events，直到 run 完成
          4. run 完成后 yield 完整答案（一次性 token）+ usage
          5. try/finally 清理 hook callbacks（手动 list.remove）
        """
        import asyncio

        queue: asyncio.Queue = asyncio.Queue()

        def on_pre_tool(*, tool_name: str = "", args: Any = None, **_):
            queue.put_nowait({
                "type": "tool_start",
                "data": {"name": tool_name, "args": args if isinstance(args, dict) else {}},
            })
            return HookResult.ok()

        def on_post_tool(*, tool_name: str = "", result: str = "", **_):
            # result 截断防爆（read_file 一个 10000 行文件会撑爆 SSE）
            shown = result if len(result) < 2000 else result[:2000] + f"\n…[truncated, {len(result)} chars total]"
            queue.put_nowait({
                "type": "tool_end",
                "data": {"name": tool_name, "result": shown, "ok": not result.startswith("[ERROR]")},
            })
            return HookResult.ok()

        # 注册 + 记录引用方便清理
        on_pre_tool.__name__ = "stream_full_pre"
        on_post_tool.__name__ = "stream_full_post"
        self.hooks.register(HookEvent.PRE_TOOL_USE, on_pre_tool)
        self.hooks.register(HookEvent.POST_TOOL_USE, on_post_tool)

        try:
            run_task = asyncio.create_task(self.run(task))

            # 边跑边中转 events
            while not run_task.done():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.1)
                    yield event
                except asyncio.TimeoutError:
                    continue

            # 排空残留事件（防 final tool 没及时出队）
            while not queue.empty():
                yield queue.get_nowait()

            # 拿完整结果
            answer = await run_task
            yield {"type": "token", "data": {"delta": answer}}

            total_cost = sum(getattr(c, "cost_rmb", 0.0) for c in (self.last_costs or []))
            total_in = sum(getattr(c, "tokens_in", 0) for c in (self.last_costs or []))
            total_out = sum(getattr(c, "tokens_out", 0) for c in (self.last_costs or []))
            yield {
                "type": "usage",
                "data": {
                    "prompt": total_in,
                    "completion": total_out,
                    "cost_rmb": round(total_cost, 4),
                    "model": model_name,
                },
            }
        finally:
            # hooks.py 没 unregister 方法 —— 直接从 _handlers list remove
            try:
                self.hooks._handlers[HookEvent.PRE_TOOL_USE].remove(on_pre_tool)
            except ValueError:
                pass
            try:
                self.hooks._handlers[HookEvent.POST_TOOL_USE].remove(on_post_tool)
            except ValueError:
                pass

    # ─────────────── 并发对比 ───────────────

    async def compare(self, task: str) -> dict[str, dict]:
        """
        并发调三家模型，返回每家 {text, cost}。
        总耗时 ≈ 最慢那家，而不是三家之和（asyncio.gather 的力量）。
        """
        names = ["deepseek", "qwen", "doubao"]
        adapters = [self._get_adapter(n) for n in names]

        async def one(adapter: ModelAdapter) -> dict:
            t0 = time.perf_counter()
            try:
                text = await adapter.complete(
                    messages=[{"role": "user", "content": task}],
                    system=SYSTEM_PROMPT,
                )
            except Exception as e:
                return {
                    "text": f"[ERROR] {type(e).__name__}: {e}",
                    "cost": compute_cost(adapter.name, 0, 0),
                    "latency_ms": 0,
                }
            latency = (time.perf_counter() - t0) * 1000
            cost = compute_cost(
                adapter.name,
                adapter.last_usage.prompt_tokens,
                adapter.last_usage.completion_tokens,
            )
            return {"text": text, "cost": cost, "latency_ms": latency}

        results = await asyncio.gather(*(one(a) for a in adapters))
        return dict(zip(names, results))
