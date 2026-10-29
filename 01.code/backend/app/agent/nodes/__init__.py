"""LangGraph 节点包。

模块划分：

- `pipeline.py`：一轮问答的主干节点（注入 Schema / slot / 澄清卡与等待 / 图表 / 解读 / 留痕）；
- `execution.py`：**生成 → 守卫 → 执行 → 形态自检 → 重写环**，由条件边串起来。

澄清是 **Human-in-the-loop**（阶段 4）：`clarify_card`（建卡 + 落库 + 发事件）→
`clarify_wait`（`interrupt()` 挂起）→ 用户回填 → 恢复后回到 `extract_slot` 合并补丁继续。

> 注（**2026-10-14 死代码清理**）：阶段 1 的"整环一个节点"版本 `pipeline.node_generate_execute`
> 及其配套的 `pipeline.route_after_generate`（返回 `persist`/`chart`，与现行条件边映射不符）
> **已删除** —— 生成环统一由 `execution.py` 提供。
> 删除流程（可复用）：① 先证明"零引用"（`node_generate_execute` 仅剩定义与旧注释、
> `pipeline.route_after_generate` 仅剩定义——图用的是本包导出的 `execution` 版）；
> ② 按行精确删除 + `compile()` 自检（脚本见 `02.data/tmp/_drop_dead_node.py`）；
> ③ 全量回归 + lint 验证。记录见 `大修工作进度/00-进度总览.md`。
"""

from app.agent.nodes.execution import (
    node_execute,
    node_generate,
    node_guard,
    node_publish_table,
    node_rewrite,
    node_shape_check,
    route_after_execute,
    route_after_generate,
    route_after_guard,
    route_after_rewrite,
    route_after_shape,
)
from app.agent.nodes.pipeline import (
    node_chart,
    node_clarify_card,
    node_clarify_wait,
    node_extract_slot,
    node_inject_schema,
    node_narrate,
    node_persist,
    route_after_slot,
)

__all__ = [
    # 主干
    "node_inject_schema",
    "node_extract_slot",
    "node_clarify_card",
    "node_clarify_wait",
    "node_chart",
    "node_narrate",
    "node_persist",
    "route_after_slot",
    # 生成 / 守卫 / 执行 / 自检 / 重写环
    "node_generate",
    "node_guard",
    "node_execute",
    "node_shape_check",
    "node_publish_table",
    "node_rewrite",
    "route_after_generate",
    "route_after_guard",
    "route_after_execute",
    "route_after_shape",
    "route_after_rewrite",
]
