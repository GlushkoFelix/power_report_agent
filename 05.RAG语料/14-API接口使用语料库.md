# API接口使用语料库

**主题：** 接口清单、调用约定与调试方法
**适用范围：** 前后端开发者、集成测试者
**版本：** v1.0

---

## Q: 系统共有多少条接口？怎么分组？

**A:** 共 **39 条**端点，分 7 组：
- **auth**（认证与个人中心）：注册 / 登录 / 当前用户 / 编辑资料 / 修改密码 /
  清空历史记录 / 删除账号；
- **reports**（报表）：列表 / 详情 / 预览 / 质量 / 字段字典 / 上传 / 确认入库 / 删除 / 切换活跃；
- **conversations**（会话）：列表 / 新建 / 详情 / 修改 / 删除 / 消息历史 / 置顶；
- **analysis**（分析）：对话分析（SSE）/ 澄清继续（SSE）/ 记录列表 / 记录详情 / 重跑；
- **charts**（图表）：重渲染（换图型/粒度）/ 图片导出；
- **eval**（评测）：题库 / 发起批次 / 批次列表 / 批次详情 / 报告下载；
- **system**（系统）：健康检查 / Schema 注入文本。
完整端点表由工具从 OpenAPI 同源生成（见《API 接口文档》附录 A），可随代码自动同步。

**标签：** 接口清单, 端点分组, 39 条, OpenAPI

---

## Q: 调用接口的通用约定有哪些？

**A:** ① **鉴权**：除注册/登录与健康检查外都要 `Authorization: Bearer <token>`；
② **统一包络**：`{code, message, data, request_id}`——`code` 与 HTTP 同值（成功 0），
业务错误码在 `data.error_code`，修复建议在 `data.hint`；
③ **业务编号**：资源用对外编号（`report_code` / `conversation_code` / `record_code` / `run_code`），
**不使用自增主键**；
④ **业务失败仍可能是 HTTP 200**：SSE 接口的失败通过 `error` 事件下发，
普通接口则用 400/403/404 状态码 + `E_*`；
⑤ 参数越界返回 **400 `E_VALIDATION`**（不是框架 422）。

**标签：** 通用约定, 鉴权, 包络, 业务编号, 400 与 422

---

## Q: 系统里有哪两种分页？分别用在哪？

**A:** 两种，按数据形态选：
- **offset 分页**：用于**分析记录列表**（`page` / `page_size` + 组合筛选 + 稳定排序），
  因为它是"可跳页的明细列表"；
- **游标分页**：用于**会话消息历史**（默认最近 50 条，`?before_seq=` 向历史翻页，
  返回 `has_more` / `oldest_seq` / `newest_seq`）——消息是**只追加**的流，
  游标分页直接命中 `UNIQUE(conversation_id, seq_no)`，**不会出现 offset 漂移**
  （翻页时若有新消息，offset 会导致重复或漏项）。
会话列表与报表列表则是**全量返回不分页**（数量可控），带搜索与日期筛选。

**标签：** 分页, offset, 游标分页, 无漂移

---

## Q: SSE 接口（对话分析）怎么调用？

**A:** `POST /api/v1/analysis/chat`，请求体：
`{ "conversation_code": "...", "message": "...", "report_code": "r_daily"?, "slot_patch": {...}? }`，
响应 `text/event-stream`。事件共 **10 个**：
`status` / `slot` / `clarify` / `code` / `table` / `chart` / `narrative` / `meta` / `error` / `done`。
要点：
① **HTTP 200 不代表成功**，必须监听 `error` 事件；
② 浏览器端不能用原生 `EventSource`（它只支持 GET、不能带鉴权头）——
用 `@microsoft/fetch-event-source`；
③ 客户端断开即**取消链路**（`AbortController`）；
④ `done` 事件带 `record_id`（澄清轮为 `null`）。

**标签：** SSE 调用, 事件清单, error 事件, 断开取消

---

## Q: 用 PowerShell 怎么发起一次对话并看事件？

**A:** 三步（先登录拿 token）：

```powershell
# 1) 登录
$t = (Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/auth/login" -Method POST `
      -ContentType "application/json" -Body '{"username":"demo_ui","password":"demo12345"}').data.token

# 2) 建会话（拿 conversation_code）
$c = (Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/conversations" -Method POST `
      -Headers @{ Authorization = "Bearer $t" } -ContentType "application/json" `
      -Body '{"title":"接口调试"}').data.conversation_code

# 3) 看 SSE：命令行不便直接读流，推荐用项目自带探针或前端工作台
.venv\Scripts\python.exe 01.code\tools\sse_probe.py
```

> `sse_probe.py` 会打印各事件的到达时间与首字延迟，并能识别"攒批回放"（伪流式）。

**标签：** PowerShell 调用, 登录, 建会话, SSE 探针

---

## Q: 发起评测批次怎么调？结果怎么看？

**A:** 两步：
① `POST /api/v1/eval/runs`，请求体 `{"impl":"selfcheck|real","limit":N | "question_codes":[...],"name":"..."}`
→ 返回 `run_code`（**立即返回，后台跑**）；
② 轮询 `GET /api/v1/eval/runs/{run_code}` 直到 `status=finished`，
逐题明细与指标在响应里；报告用 `GET /api/v1/eval/runs/{run_code}/report` 下载（Markdown）。
注意事项：`impl=real` 未配 Key 时**返回 400 并给 hint**（不静默回落）；
运行期**没有逐题进度端点**，所以界面只显示"运行中 · 共 N 题 + 已耗时"，不编造 x/N。

**标签：** 评测接口, 发起批次, 轮询, 报告下载

---

## Q: 常见的错误码有哪些？分别代表什么？

**A:** 分组记忆（完整 27 个 `E_*` 见《API 接口文档》错误码总表）：
- **鉴权类**：`E_AUTH`（未登录/token 失效 401）、`E_CREDENTIALS`（账号或密码错）、
  `E_AUTH_LOCKED`（锁定中，带剩余秒数）、`E_ACCOUNT_DISABLED`（账号停用）、`E_FORBIDDEN`（越权 403）；
- **参数类**：`E_VALIDATION`（参数不合法，含 hint）；
- **报表类**：`E_FILE_TOO_LARGE`（413）、`E_FILE_TYPE`（类型不支持）、
  `E_NO_TIME_COLUMN`（缺时间列）、`E_INGEST_FAILED`（入库失败）；
- **分析类**：`E_SQL_FORBIDDEN`（SQL 未过守卫）、`E_CODE_FORBIDDEN`（代码未过 AST 预检）、
  `E_SQL_EXEC` / `E_CODE_EXEC`（执行失败）、`E_SANDBOX_TIMEOUT`（沙箱超时）、`E_LLM`（模型调用失败）；
- **资源类**：`E_NOT_FOUND`（不存在或无权访问，均返回 404 以防探测）。

**标签：** 错误码, E_ 前缀, 分组记忆, 27 个

---

## Q: 越权访问为什么统一返回 404 而不是 403？

**A:** 因为 403 会**泄露资源是否存在**——攻击者可以用"403 vs 404"的差异来枚举他人的
报表 / 会话 / 记录的编号。项目的约定是：
**访问他人资源一律返回 404**（文案也不透露存在性）；
只有明确的"已认证但无权限操作"场景（如尝试操作预置报表的删除）才用 403。
配合短编号 + 归属校验，构成基本的越权防护。

**标签：** 越权防护, 404 而非 403, 防枚举, 归属校验

---

## Q: 调试接口时的排障抓手是什么？

**A:** 三个：
① **`request_id`**：每个响应都带（同时在响应头），后端日志里按 `rid=` 串全过程，
   界面报错也展示它——把它抄给开发能快速定位；
② **`GET /system/health`**：一眼看进程、两库、模型配置状态；
③ **`GET /system/schema-text`**：看当前会话实际注入给模型的 Schema 文本
   （`text_source` 标明来源），排查"模型为什么选了某个字段"时非常有用。
另外：业务失败先看 `data.error_code` 与 `data.hint`，多数情况 hint 已写明下一步怎么做。

**标签：** 排障, request_id, health, schema-text
