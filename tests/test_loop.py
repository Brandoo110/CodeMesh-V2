"""
Agent Loop 单元测试（不联网）
================================

跑法：
    python -m tests.test_loop

策略：
  通过一个 FakeAdapter 注入预设的"模型回复序列"，让 run_agent_loop
  在不调真实 OpenAI 的前提下走完所有分支：
    - 直接返回（无 tool_call）
    - 单轮工具调用 → 最终答案
    - 多轮工具调用
    - on_tool_call 回调被触发
    - 达到 max_iterations 上限
"""

import asyncio
from types import SimpleNamespace

from execution.loop import run_agent_loop


# ────────────────────────── Fake OpenAI client ──────────────────────────


class _FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, message):
        self.message = message


class _FakeResponse:
    def __init__(self, content=None, tool_calls=None):
        self.choices = [_FakeChoice(_FakeMessage(content, tool_calls))]


def _tool_call(tc_id: str, name: str, arguments: str):
    """构造一个长得像 OpenAI tool_call 的 SimpleNamespace。"""
    return SimpleNamespace(
        id=tc_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class _FakeCompletions:
    def __init__(self, responses):
        self._queue = list(responses)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._queue:
            raise AssertionError(
                "FakeCompletions ran out of canned responses; "
                "loop iterated more times than expected."
            )
        return self._queue.pop(0)


class _FakeAdapter:
    """
    最小可行的 ModelAdapter 替身。
    run_agent_loop 只读 .client / .model 两个属性。
    """

    def __init__(self, responses):
        self._completions = _FakeCompletions(responses)
        self.client = SimpleNamespace(
            chat=SimpleNamespace(completions=self._completions)
        )
        self.model = "fake-model"

    @property
    def calls(self):
        return self._completions.calls


# ────────────────────────── helpers ──────────────────────────


def _run(coro):
    return asyncio.run(coro)


# ────────────────────────── tests ──────────────────────────


def test_loop_returns_when_no_tool_call():
    """模型一开口就给了 final answer 且没工具调用 → 立即返回。"""
    adapter = _FakeAdapter([_FakeResponse(content="hello world")])
    out = _run(run_agent_loop(
        adapter=adapter,
        messages=[{"role": "user", "content": "hi"}],
        system="sys",
    ))
    assert out == "hello world"
    assert len(adapter.calls) == 1
    # 第一次调用应该带上完整 messages（含 system + user）
    msgs = adapter.calls[0]["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"


def test_loop_executes_one_tool_then_finishes():
    """第一轮要求 read_file，第二轮拿到结果后输出 final。"""
    # 用真实仓库里的文件验证整条链路
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".txt")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("FILE_CONTENT_42")

        responses = [
            _FakeResponse(
                content=None,
                tool_calls=[_tool_call("call_1", "read_file", f'{{"path": "{path}"}}')],
            ),
            _FakeResponse(content="the file said 42"),
        ]
        adapter = _FakeAdapter(responses)

        seen = []
        def on_tool(name, args, result):
            seen.append((name, args, result))

        out = _run(run_agent_loop(
            adapter=adapter,
            messages=[{"role": "user", "content": "what's in the file?"}],
            on_tool_call=on_tool,
        ))
        assert out == "the file said 42"
        assert len(adapter.calls) == 2
        assert len(seen) == 1
        assert seen[0][0] == "read_file"
        assert "FILE_CONTENT_42" in seen[0][2]
        # 第二次请求必须包含一条 role=tool 的消息（结果回填）
        msgs2 = adapter.calls[1]["messages"]
        assert any(m.get("role") == "tool" for m in msgs2)
    finally:
        os.unlink(path)


def test_loop_handles_multiple_tool_calls_in_one_turn():
    """一轮里有 2 个 tool_calls，都执行；第二轮模型给 final。"""
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    try:
        responses = [
            _FakeResponse(
                content=None,
                tool_calls=[
                    _tool_call("c1", "write_file",
                               f'{{"path": "{path}", "content": "AA"}}'),
                    _tool_call("c2", "read_file",
                               f'{{"path": "{path}"}}'),
                ],
            ),
            _FakeResponse(content="done"),
        ]
        adapter = _FakeAdapter(responses)
        out = _run(run_agent_loop(adapter=adapter,
                                   messages=[{"role": "user", "content": "go"}]))
        assert out == "done"
        # 第二轮 messages 应包含 2 条 tool 结果
        msgs2 = adapter.calls[1]["messages"]
        tool_msgs = [m for m in msgs2 if m.get("role") == "tool"]
        assert len(tool_msgs) == 2
    finally:
        os.unlink(path)


def test_loop_respects_max_iterations():
    """模型一直要求工具调用 → 达到 max_iterations 后返回兜底字符串。"""
    # 给 5 个永远要求 tool_call 的回复
    looping_responses = [
        _FakeResponse(
            content=None,
            tool_calls=[_tool_call(f"c{i}", "read_file",
                                   '{"path": "/nope/none"}')],
        )
        for i in range(5)
    ]
    adapter = _FakeAdapter(looping_responses)
    out = _run(run_agent_loop(
        adapter=adapter,
        messages=[{"role": "user", "content": "loop"}],
        max_iterations=3,
    ))
    assert "max_iterations" in out
    assert len(adapter.calls) == 3


def test_loop_passes_tool_schemas_to_model():
    """每次调用必须把 TOOL_SCHEMAS 通过 tools 参数喂给模型。"""
    adapter = _FakeAdapter([_FakeResponse(content="ok")])
    _run(run_agent_loop(adapter=adapter,
                         messages=[{"role": "user", "content": "x"}]))
    tools = adapter.calls[0]["tools"]
    assert isinstance(tools, list) and len(tools) >= 3
    names = {t["function"]["name"] for t in tools}
    assert {"bash_exec", "read_file", "write_file"}.issubset(names)


def test_loop_handles_unknown_tool_gracefully():
    """模型瞎写工具名 → dispatch_tool 返回错误字符串，loop 不崩，继续到下一轮。"""
    responses = [
        _FakeResponse(
            content=None,
            tool_calls=[_tool_call("c1", "no_such_tool", "{}")],
        ),
        _FakeResponse(content="recovered"),
    ]
    adapter = _FakeAdapter(responses)
    out = _run(run_agent_loop(adapter=adapter,
                               messages=[{"role": "user", "content": "x"}]))
    assert out == "recovered"
    msgs2 = adapter.calls[1]["messages"]
    tool_msgs = [m for m in msgs2 if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "[ERROR]" in tool_msgs[0]["content"]


def test_loop_handles_bad_json_arguments():
    """模型返回的 arguments 不是合法 JSON → 视作 {} 并继续。"""
    responses = [
        _FakeResponse(
            content=None,
            tool_calls=[_tool_call("c1", "read_file", "this is not json")],
        ),
        _FakeResponse(content="ok"),
    ]
    adapter = _FakeAdapter(responses)
    out = _run(run_agent_loop(adapter=adapter,
                               messages=[{"role": "user", "content": "x"}]))
    assert out == "ok"
    msgs2 = adapter.calls[1]["messages"]
    tool_msg = next(m for m in msgs2 if m.get("role") == "tool")
    # 因为 args 变成 {}，read_file 没收到 path → bad arguments
    assert "[ERROR]" in tool_msg["content"]


# ────────────────────────── runner ──────────────────────────


if __name__ == "__main__":
    import traceback

    tests = [
        v for k, v in list(globals().items())
        if callable(v) and k.startswith("test_")
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"OK  {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} loop tests passed.")
    if failed:
        raise SystemExit(1)
