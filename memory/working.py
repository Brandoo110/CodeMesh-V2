"""
工作记忆：当前任务状态（Harness 记忆层）
==========================================

【工作记忆 vs 短期记忆】
  - 短期记忆 = 对话历史（messages 列表）
  - 工作记忆 = 当前任务的「结构化状态」（正在改哪个文件、第几步、已完成哪些子任务）

短期记忆是模型视角看到的东西；工作记忆是 Harness 自己维护的「元信息」，
用于日志、调试、中断恢复。

【为什么用 dataclass 而不是 dict】
dict 没有类型约束，容易打错 key（比如把 `current_file` 写成 `curr_file`）。
dataclass 有字段定义，IDE 能自动补全，类型检查能报错。
对教学型项目来说，类型清晰比灵活性更重要。

【设计要点】
"工作记忆典型有哪些字段？"
→ 正在操作的资源引用（文件路径、URL、PR 号）、任务进度计数、子任务列表、
  关键决策点（比如「用户已批准删除 xxx」）。
  工作记忆让 Agent 能「记得自己在做什么」，即便模型的 context 窗口被压缩了。
"""

from dataclasses import dataclass, field, asdict


@dataclass
class WorkingMemory:
    """
    当前任务的结构化状态。

    字段说明：
      - task_description: 任务目标的自然语言描述（用户最初输入）
      - current_file    : 当前 Agent 正在操作的文件路径（工具调用时会更新）
      - step            : 已经执行了多少步（每调一次工具 +1）
      - notes           : 自由文本，给 Agent 自己记录中间结论
    """

    task_description: str = ""
    current_file: str = ""
    step: int = 0
    # field(default_factory=list) 是 dataclass 写可变默认值的标准姿势
    # 不这么写会触发「可变默认值陷阱」：所有实例共享同一个 list
    notes: list[str] = field(default_factory=list)

    def update(self, **kwargs) -> None:
        """
        部分字段更新。用 kwargs 的好处是调用方不用全部字段都传。
        例子：mem.update(current_file="x.py", step=mem.step + 1)
        """
        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)

    def snapshot(self) -> dict:
        """
        导出为 dict，方便序列化（日志、Langfuse 埋点）。
        asdict 会递归把嵌套 dataclass 也转成 dict。
        """
        return asdict(self)
