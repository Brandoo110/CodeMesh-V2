"""
Agent Loop：执行层的心脏（Harness 执行层）
==============================================

【什么是 Agent Loop】
一个极其简洁但威力无穷的循环：

    while True:
        response = call_model(messages, tools)
        if response 没有 tool_call:
            break                    # 模型说完了，退出
        for tool_call in response:
            result = run_tool(tool_call)
            messages.append(result)  # 把结果塞回对话
    return response.text

这就是 Claude Code、Cursor、Devin 这类 Agent 的底层循环。
它的威力在于：模型可以通过反复调用工具，逐步完成复杂任务。
比如"修复这个 bug" → read_file → bash_exec(pytest) → 分析失败 → write_file → bash_exec(pytest) → 完成。

【本项目的实现细节】
  - 走 OpenAI tool-calls 协议（三家国内厂商都兼容这个）
  - 我们不直接用 ModelAdapter.complete（它只返回文本），而是自己调 raw client
    因为 tool use 需要完整的 response 对象（有 tool_calls 字段）
  - 为了让适配器层保持极简，这里 Agent Loop 自己管 client
    → 生产代码可以考虑给适配器加一个 complete_with_tools 方法

【停止条件】
  1. finish_reason == "stop"（模型说完）
  2. 没有 tool_calls
  3. 迭代次数超过 max_iterations（防死循环）

【面试点】
"Agent Loop 死循环怎么防？"
→ max_iterations 硬上限 + 每轮检查 tool_call 是否重复（同一文件读第 3 次）。
  更进阶的做法是让另一个"监工" Agent 判断主 Agent 是不是卡住了。

"如果工具调用很慢，能并行吗？"
→ 可以。同一轮里多个 tool_calls 可以 asyncio.gather 并发执行。
  这里为教学清晰用顺序执行。
"""

import json

from orchestration.adapters.base import ModelAdapter

from .tools import TOOL_SCHEMAS, dispatch_tool


# 最大迭代轮数，防止模型无限调工具
MAX_ITERATIONS = 15


async def run_agent_loop(
    adapter: ModelAdapter,
    messages: list[dict],
    system: str = "",
    max_iterations: int = MAX_ITERATIONS,
    on_tool_call=None,   # 可选回调：hook 系统会传进来
    tool_allowlist: "set[str] | list[str] | None" = None,   # v5 Phase 6.4：工具白名单
) -> str:
    """
    运行 Agent Loop 直到模型停止调工具或达到上限。

    Args:
        adapter : 模型适配器（带 .client 属性的那种）
        messages: 初始消息列表（不含 system）
        system  : 系统提示词
        max_iterations: 最多循环多少轮
        on_tool_call  : (tool_name, args, result) -> None 的回调，用于打日志/埋点

    Returns:
        最终 assistant 文本回复
    """
    # 把 system 拼到 messages 头部（loop 内部维护完整消息历史）
    convo: list[dict] = []
    if system:
        convo.append({"role": "system", "content": system})
    convo.extend(messages)

    # 这里我们直接用 adapter.client（AsyncOpenAI）来发请求
    # 因为需要完整 response 对象以拿到 tool_calls
    client = adapter.client  # type: ignore[attr-defined]
    model = adapter.model    # type: ignore[attr-defined]

    # v5 Phase 6.4 工具白名单 filter（向后兼容：None 或 ["*"] 全开）
    #
    # 设计：在循环外算一次 effective_tools 和 allowed_names。前者给 model 看（list_tools），
    # 后者用于 dispatch 时的兜底拒绝（防止模型硬调被禁的工具）。
    allow_all = tool_allowlist is None or (
        isinstance(tool_allowlist, (list, tuple)) and "*" in tool_allowlist
    )
    if allow_all:
        effective_tools = TOOL_SCHEMAS
        allowed_names: "set[str] | None" = None
    else:
        allowed_names = set(tool_allowlist or ())
        effective_tools = [
            s for s in TOOL_SCHEMAS
            if s.get("function", {}).get("name") in allowed_names
        ]

    # tools=[] 时 OpenAI 协议会报错，必须传 None 才能跳过 function calling
    # 这一支用于 enable_tools=[] 的"纯文本生成"步骤
    tools_arg = effective_tools if effective_tools else None
    tool_choice_arg = "auto" if effective_tools else "none"

    for iteration in range(max_iterations):
        resp = await client.chat.completions.create(
            model=model,
            messages=convo,
            tools=tools_arg,
            tool_choice=tool_choice_arg,
            temperature=0.3,
        )

        msg = resp.choices[0].message

        # 先把 assistant 消息回填对话历史
        # 注意：有 tool_calls 时 content 可能是 None，需要防御
        assistant_entry: dict = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            # OpenAI 协议要求把 tool_calls 原样带上，下一轮工具结果才能对上号
            assistant_entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
        convo.append(assistant_entry)

        # 没有工具调用 = 模型说完了，跳出循环
        if not msg.tool_calls:
            return msg.content or ""

        # 依次执行每个工具调用，结果塞回 messages
        for tc in msg.tool_calls:
            tool_name = tc.function.name
            # arguments 是 JSON 字符串，要反序列化
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            # v5 Phase 6.4：白名单二次防御
            # 一般模型看不到被禁的工具，但仍要兜底拒绝硬调（如旧 cache / prompt 注入）
            if allowed_names is not None and tool_name not in allowed_names:
                result = (
                    f"[ERROR] tool {tool_name!r} is not allowed in this step. "
                    f"Allowed tools: {sorted(allowed_names) or '(none)'}"
                )
            else:
                result = await dispatch_tool(tool_name, args)

            # 触发外部回调（hook 在这里起作用）
            if on_tool_call is not None:
                on_tool_call(tool_name, args, result)

            # 把工具结果作为 role="tool" 消息塞回去。
            # tool_call_id 是关键：OpenAI 协议要求匹配上面的 assistant tool_call.id
            convo.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

    # 达到最大迭代还没停 —— 防止无限跑
    return "[AGENT LOOP] reached max_iterations without final answer"
