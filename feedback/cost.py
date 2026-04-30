"""
成本计算（Harness 反馈层）
=============================

【为什么需要成本表】
多模型调度的核心卖点之一是"便宜"。但要讲清楚"便宜多少"必须有数据。
这个模块做两件事：
  1. 维护一份各厂商官方价格表（¥ / 百万 token）
  2. 给定 (model, tokens_in, tokens_out) 算出本次调用花了多少钱

【价格数据从哪来】
各厂商官网定价页。价格会变，所以常量里写清"截至 xxx"。
生产环境可以把这张表做成外部配置，调价时改 YAML 不改代码。

【面试点】
"Q: 你怎么做多模型成本对比？"
→ 每次 API 返回都有 usage 字段（prompt_tokens、completion_tokens）。
  拿实际 token 数 × 各家单价，加总到 Langfuse trace。
  `--compare` 模式下直接展示三家成本差。

"Q: 为什么 input/output 价格不一样？"
→ 推理（output）比预填充（input）贵 2–4 倍，因为 output 是逐 token 生成，
  无法并行；input 可以一次性过 KV cache。缓存命中的 input 还会更便宜。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPricing:
    """某个模型的单价（人民币 / 百万 token）。frozen=True 让它不可变，避免被误改。"""
    input_per_m: float
    output_per_m: float


# 价格表（截至 2026-04，各家官网公开价）
# 注意：真实项目务必定期核对更新。
PRICING: dict[str, ModelPricing] = {
    "deepseek": ModelPricing(input_per_m=1.0, output_per_m=2.0),    # deepseek-chat
    "qwen":     ModelPricing(input_per_m=2.0, output_per_m=6.0),    # qwen-coder-turbo
    "doubao":   ModelPricing(input_per_m=0.8, output_per_m=2.0),    # doubao-pro
    # Gemini 2.5 Flash 官方价：$0.075/M in, $0.30/M out；按 ~7.2 汇率换算到人民币
    "gemini":   ModelPricing(input_per_m=0.55, output_per_m=2.2),
}


@dataclass
class CallCost:
    """一次调用的成本明细。"""
    model: str
    tokens_in: int
    tokens_out: int
    cost_rmb: float

    def format_line(self) -> str:
        """格式化一行展示。成本保留 4 位小数，token 数千分位加逗号。"""
        return (
            f"{self.model:<10} "
            f"in={self.tokens_in:>6,}  out={self.tokens_out:>6,}  "
            f"¥{self.cost_rmb:.4f}"
        )


def compute_cost(model: str, tokens_in: int, tokens_out: int) -> CallCost:
    """
    按模型单价算成本。
    未知模型按 0 计（安全默认：宁愿少算也不假造数据）。
    """
    price = PRICING.get(model)
    if price is None:
        return CallCost(model=model, tokens_in=tokens_in, tokens_out=tokens_out, cost_rmb=0.0)

    cost = (
        tokens_in / 1_000_000 * price.input_per_m
        + tokens_out / 1_000_000 * price.output_per_m
    )
    return CallCost(
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_rmb=cost,
    )
