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
    make_default_logging_hooks,
)
from orchestration.adapters import (
    DeepSeekAdapter,
    DashScopeAdapter,
    VolcEngineAdapter,
    GeminiAdapter,
    ModelAdapter,
)
from feedback import Observer, compute_cost, CallCost
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
    ):
        # 记忆层（可选开启 LLM 摘要压缩，避免长对话直接丢历史）
        self.enable_memory_compression = enable_memory_compression
        if enable_memory_compression:
            self.short_term = ShortTermMemory(
                max_messages=20,
                compress_threshold=15,
                summarizer=self._summarize,
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

        # RAG 开关（需要先跑 `codemesh index` 建索引才有效）
        self.use_rag = use_rag

        # 每次 run 累计的成本
        self.last_costs: list[CallCost] = []

        self._adapters: dict[str, ModelAdapter] = {}

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

    # ─────────────── 成本记账 ───────────────

    def _record_cost(self, adapter: ModelAdapter) -> CallCost:
        """从 adapter.last_usage 取 token，算成本，加到本轮 last_costs。"""
        u = adapter.last_usage
        cost = compute_cost(adapter.name, u.prompt_tokens, u.completion_tokens)
        self.last_costs.append(cost)
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
            + RAG 检索片段（如果开了 --rag 且 query 命中）
        """
        system = SYSTEM_PROMPT + await self._load_long_term_block()
        if not self.use_rag:
            return system
        hits = await retrieve(task, top_k=5)
        if not hits:
            return system
        ctx = format_context(hits, max_chars=4000)
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
        self.observer.start_trace(task)

        decision = await route(task)
        print(
            f"[router] model={decision.model} "
            f"complexity={decision.complexity} reason={decision.reason}"
        )

        self.working.update(task_description=task, step=0)
        self.short_term.add("user", task)
        system = await self._build_system_with_context(task)
        adapter = self._get_adapter(decision.model)

        t0 = time.perf_counter()
        if decision.complexity == "simple":
            answer = await self._run_single_turn(adapter, task, system)
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
        results: list[str] = []
        for i, step in enumerate(task_plan.steps, 1):
            adapter = self._get_adapter(step.suggested_model)

            def on_tool_call(name: str, args: dict, result: str) -> None:
                self.hooks.fire_pre(name, args)
                self.hooks.fire_post(name, result)
                self.working.update(step=self.working.step + 1)

            if step.needs_tools:
                # 需要工具 → 跑完整 agent loop
                text = await run_agent_loop(
                    adapter=adapter,
                    messages=[{"role": "user", "content": step.description}],
                    system=system,
                    on_tool_call=on_tool_call,
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

            self._record_cost(adapter)
            results.append(f"【步骤 {i}】{step.description}\n{text}")

        return "\n\n".join(results)

    # ─────────────── 流式对外接口（CLI 直接用）───────────────

    async def run_stream(self, task: str):
        """
        对外流式接口。yield 文本 chunk。只支持 simple 任务。
        complex 任务走 run() 获得完整结果即可。
        """
        await self._ensure_long_term()
        self.last_costs = []
        self.observer.start_trace(task)

        decision = await route(task)
        print(
            f"[router] model={decision.model} "
            f"complexity={decision.complexity} reason={decision.reason}"
        )
        adapter = self._get_adapter(decision.model)
        system = await self._build_system_with_context(task)
        self.short_term.add("user", task)

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
