# 阶段 4 · 澄清做成真正的 Human-in-the-loop（✅ 完成，2026-10-13）

> 目标：澄清不再靠"**第二跳请求把整轮重跑一遍**"，而是 `interrupt()` 挂起 → 用户回填 →
> `Command(resume=…)` **从断点继续**。检查点（阶段 3）正是它的前置条件。
> **对外契约完全不变**：前端照旧"收 `clarify` 事件 → 弹卡 → 调 `/analysis/clarify`"。

---

## 1 图上的新形态

```
extract_slot ─┬─(缺关键条件)→ clarify_card → clarify_wait ⟲ interrupt（挂起，等回填）
              │                                   │
              │                                   └─(resume=补丁)→ extract_slot（合并补丁后继续）
              └─(条件齐备)→ generate → …（阶段 2 的执行链路）
```

| 节点 | 职责 | 为什么这样切 |
| --- | --- | --- |
| `clarify_card` | 建卡 → **落 messages×2** → 发 `clarify` 事件 | **副作用必须在中断之前完成**：LangGraph 恢复时会**重跑中断节点**（`interrupt()` 之前的代码会再执行一遍），塞进中断节点就会重复落库/重复发事件 |
| `clarify_wait` | 只有 `interrupt({...})` 一个动作 | 恢复时本节点整体重跑，除读 state 外无副作用；回填值经 `clarify_resume` 传给 `extract_slot` |
| `extract_slot`（扩展） | 检测到 `clarify_resume` → 用**中断前的部分 slot + 补丁**合并归一化 | 等价既有 `preset_slot` 语义：**跳过二次增量抽取、不重复调 slot 模型** |

## 2 契约如何保持不变（逐条对齐既有实现）

| 契约 | 实现方式 |
| --- | --- |
| 澄清轮事件序列 | `clarify → meta → done(record_id=null)`（meta/done 由运行器在请求收尾补发） |
| 澄清轮**不写 `analyses`**、`status` 仍为 `success` | 与既有 `_finish_clarify` 一致（`clarify` 字段区分，`record_id` 空） |
| 澄清轮落 messages×2（user 原问题 + assistant 澄清卡） | `clarify_card` 调 `_persist_clarify`（与既有同函数） |
| 恢复请求的事件序列 | 与 legacy 澄清延续一致：**从 `status(parsing)` 开始**，随后 `slot → code → status → table → chart → narrative → meta → done` |
| **不重复写用户消息** | 恢复前 `aupdate_state({"include_user_message": False})`（对应决策 19-1）；否则会多一个用户气泡 |
| 回填值持久化到会话 `slot_json` | 与既有 `run_clarify` 一致（用户点选不丢） |
| **降级兜底** | 线程没有挂起中断（检查点被清理 / 换库 / 旧数据）→ 自动**回退为"整轮重跑"**（阶段 1–3 的行为） |

## 3 验证（零额度）

`tests/test_langgraph_hitl.py`（3 条）+ 既有用例：

| 用例 | 断言 |
| --- | --- |
| `test_clarify_stops_with_pending_interrupt` | 澄清轮后 `aget_state().next` **非空**（真挂起）、`checkpoints` 有行、`result.events` 尾部是 `meta/done` |
| `test_clarify_endpoint_resumes_and_skips_slot_llm` | `/analysis/clarify` 从断点续跑：`llm_calls` **无 slot 阶段**（不重复调模型）、事件序列含 `status→slot→…→done`、记录 `status=success` |
| `test_clarify_endpoint_falls_back_without_checkpoint` | 删掉线程检查点后回填仍成功（走回退路径） |
| `test_parity_clarify_branch`（既有） | 澄清轮两条编排**逐字段 + 逐事件**等价 |

**全量回归：`323 passed in 64.32s`**（303 既有 + 4 分发 + 9 等价性 + 4 检查点 + 3 HITL）。

## 4 本轮踩到的坑

| 编号 | 一句话 |
| --- | --- |
| `D-005` | **中断节点会在恢复时整体重跑** → 所有副作用必须放在**中断之前的独立节点**（这是 HITL 最容易写错的地方） |
| `P-023` | 条件边的**映射 key 必须等于路由返回值**（否则 `KeyError`）；而路由返回值**又应当就是节点名**（否则 Mermaid 导出里少一条边） |
| `P-024` | 异常分支里 `final` 可能**未绑定**（`_stream` 抛异常时不会赋值）→ 必须先初始化 |
| `P-025` | 运行器补发的事件**既要推送、也要进 `result.events`**（非 SSE 调用方读的是后者） |

## 5 阶段 4 验收

| 项 | 状态 |
| --- | --- |
| 澄清是真 HITL（挂起 + 断点续跑） | ✅ 挂起任务与线程检查点均有断言 |
| 对外契约不变（事件 / 落库 / 前端零改动） | ✅ 与 legacy 澄清逐项对齐，前端未改一行 |
| 不重复调模型（省额度） | ✅ 恢复后 `llm_calls` 无 slot 阶段 |
| 降级兜底 | ✅ 无检查点仍可完成澄清 |
| 回归 | ✅ `323 passed`；额度消耗 **0** |
| 可回退 | ✅ `ORCHESTRATOR=legacy` 一行切换；新增节点独立 |

## 6 待同步的文档（阶段 7）

| 文档 | 要改什么 |
| --- | --- |
| 架构文档 | 4.2 图结构（澄清环改为 `interrupt`/`resume`）；ADR 追加"为什么用 HITL 而不是两跳重跑"（含 `D-005` 的约束） |
| API 文档 | 对外契约**不变**，但建议补一句实现注记："澄清恢复走检查点断点续跑，前端无感" |
| UIUX 文档 | 无需改（交互一致）；可在澄清卡章节补一句"回填即恢复" |
| 讲稿 / PPT | 这是**技术亮点**：可在"完整链路"页加一句"澄清是 Human-in-the-loop 的断点续跑" |
