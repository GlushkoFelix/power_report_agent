# 智电析报——基于LLM的能源电力报表智能分析Agent  API 接口文档

| 项目名称 | 智电析报（PowerReport Agent） |
| --- | --- |
| 文档版本 | v1.0.0（第一版） |
| 文档状态 | 待评审（交付物 10：**接口契约成文**；端点总表由工具从 OpenAPI 自动生成） |
| 编写日期 | 2026-09-19 |
| 对应任务书 | 《能源电力报表智能分析Agent项目任务书 V1.0.0》 |
| 事实源 | 代码（FastAPI OpenAPI schema）+《PRD》9.1 / 9.2 / 9.3 章 |
| 适用范围 | 前端联调、第三方集成、验收测试、部署自检 |

---

## 1. 文档说明

### 1.1 编写目的与读者

本文档把**运行中的后端契约**写成可查、可核对、可同步的文字，服务三类读者：

| 读者 | 关心的部分 |
| --- | --- |
| 前端 / 联调方 | 请求体字段、响应结构、SSE 事件序列、错误码与呈现方式（第 2、4–12 章） |
| 集成 / 二次开发 | 鉴权方式、分页与幂等约定、错误码总表（第 2、3 章 + 附录 B） |
| 验收 / 评审 | 端点总表与变更记录（附录 A、C），用于核对"需求 → 端点"的覆盖情况 |

### 1.2 事实源与同步方式（**本文档为什么不会过时**）

接口文档最常见的失败模式是"写完就过时"：手工清单不会随代码一起改。本项目做了两件事：

1. **端点总表由工具生成**：`01.code/tools/api_doc.py` 直接读 FastAPI 的 OpenAPI schema，
   把 39 条端点的「方法 / 路径 / 说明 / 是否鉴权 / 路径参数 / 查询参数 / 请求体」渲染成
   **附录 A**；改接口后重跑

   ```powershell
   Set-Location e:\power_report_agent
   .venv\Scripts\python.exe 01.code\tools\api_doc.py --write
   ```

   即可同步（幂等：只替换附录 A 两个标记之间的内容）。工具会**交叉校验**"本应免鉴权的端点是否意外带上了鉴权依赖"，异常时在 stderr 提示。

2. **端点摘要写在装饰器上**：39 条路由的 `summary=` 用中文描述业务含义，因此
   `http://127.0.0.1:8000/docs`（Swagger UI）与本文档附录 A **同源同文案**，
   不存在"文档一份、Swagger 一份"的偏差。

> **另有一层保障**：`pytest` 里有 293 条后端用例钉住接口行为（契约字段、错误码、级联效果），
> 改坏契约会先在这里失败。文档描述的是"代码当前的实际行为"，不是设想。

### 1.3 术语

| 术语 | 含义 |
| --- | --- |
| 包络（envelope） | 所有响应的统一外层结构 `{code, message, data, request_id}` |
| `report_code` / `conversation_code` / `record_code` / `run_code` | 报表 / 会话 / 分析记录 / 评测批次的**业务编号**（URL 一律用它，不用自增 id） |
| slot | 结构化查询条件（指标、时间范围、粒度、过滤、对比等 8 个字段），多轮对话中按增量合并 |
| SSE | Server-Sent Events，`POST /analysis/chat` 用它把"解析 → 执行 → 出表 → 出图 → 解读"分阶段推给前端 |
| `E_*` | 业务错误码，放在 `data.error_code`（见第 3 章） |
| `hint` | 错误响应 `data.hint` 里的**可执行修复建议**（UIUX 9.4「原因 + 下一步」的第二行） |

---

## 2. 通用约定

### 2.1 基础信息

| 项 | 值 |
| --- | --- |
| 统一前缀 | `/api/v1`（`settings.api_v1_prefix`） |
| 本机基址 | `http://127.0.0.1:8000`；前端 dev 经 Vite 代理 `/api` 转发（见 2.7） |
| 协议 | HTTP/1.1（`POST /analysis/chat`、`/analysis/clarify` 返回 `text/event-stream`） |
| 请求编码 | 请求体 `application/json; charset=utf-8`；上传接口为 `multipart/form-data` |
| 交互文档 | `/docs`（Swagger UI）、`/redoc`、`/openapi.json` |
| 请求 ID | 每个响应带 `request_id` 字段与 `X-Request-ID` 响应头（也可由调用方传入 `X-Request-ID` 指定） |

### 2.2 鉴权

- 方式：`Authorization: Bearer <token>`，token 由 `POST /auth/register`（注册即登录）
  或 `POST /auth/login` 签发。
- **有效期 24 小时**（`jwt_expire_hours`，默认 24），签发时在 `data.expires_at` 里回显（ISO8601 UTC）。
- `HS256` 签名，载荷含 `sub`（用户 id）/ `username` / `iat` / `exp` / `jti`。
- 免鉴权端点**仅 3 条**：`POST /auth/register`、`POST /auth/login`、`GET /system/health`
  （`/docs`、`/openapi.json` 等框架自带文档路由同样免鉴权）。
- 鉴权失败语义：token 缺失 / 非法 / 过期 → **401 `E_AUTH`**；访问他人资源 → **403 / 404**
  （资源不存在与无权限**同样返回 404**，不泄露"该资源是否存在"，见 4.8 与各记录接口）。

> **改密后 token 仍然有效**：本项目 JWT **无服务端黑名单/会话表**，`POST /auth/password`
> 成功后已签发的 token 不会失效，其它设备不会被踢下线。接口在成功文案里**如实说明**这一点，
> 前端也会提示用户；不要把它当成"有服务端会话"设计。同理，`POST /auth/logout` 只是前端清 token + 落审计，
> 服务端不做失效处理。

### 2.3 统一响应包络

**成功**

```json
{ "code": 0, "message": "ok", "data": { }, "request_id": "9f3c1a7b2d4e5f60" }
```

**失败**（双轨制：`code` = HTTP 状态码，业务错误码在 `data.error_code`）

```json
{
  "code": 400,
  "message": "请求参数不合法：password 密码必须同时包含字母与数字",
  "data": { "error_code": "E_VALIDATION", "hint": "请设置 8 位以上、同时包含字母与数字的密码" },
  "request_id": "9f3c1a7b2d4e5f60"
}
```

要点：

- **HTTP 状态码有语义**（400 / 401 / 403 / 404 / 409 / 413 / 422 / 429 / 500 / 501），
  前端可直接按状态码做统一拦截；业务分支判断请用 `data.error_code`。
- `data` 里除 `error_code` 外还能带**结构化上下文**（如 `locked_seconds`、`line_no`、
  `duplicate_columns`、`supported`）与 `hint` 修复建议。
- `422` 专用于**框架级**结构与类型校验失败（Pydantic），业务级校验用 `400 E_VALIDATION`；
  两者 `error_code` 同为 `E_VALIDATION`，但 `message` 会指出具体字段。

### 2.4 分页与筛选

项目里存在**两种分页**，按数据形态选用，不要混淆：

| 形态 | 使用端点 | 参数 | 响应字段 |
| --- | --- | --- | --- |
| **offset 分页** | `GET /analysis/records`、`GET /eval/questions`、`GET /eval/runs` | `page`（从 1 起）、`page_size` | `total` / `page` / `page_size` / `items` |
| **游标分页** | `GET /conversations/{code}/messages` | `limit`（默认 50，上限 200）、`before_seq`（取序号更小的更早消息） | `items` / `limit` / `has_more` / `oldest_seq` / `newest_seq` |
| **不分页**（全量） | `GET /reports`、`GET /conversations` | 关键词 / 时间筛选 | `total` + `items` |

- 排序约定：记录类接口固定 `created_at DESC, id DESC`（`id` 作次键，避免同一秒内翻页错位）。
- 游标分页用于"对话历史越翻越早"的场景：它不会因为期间新增消息而导致**翻页漂移**（offset 分页的通病）。
- 列表类响应一律**只给轻字段**（如分析记录列表不含 SQL 与解读正文），正文走详情接口。

### 2.5 时间与字段格式

| 类型 | 格式 | 示例 |
| --- | --- | --- |
| 数据库时间列（`created_at` / `updated_at` / `last_message_at`） | `YYYY-MM-DD HH:MM:SS`，**本地时间**（SQLite `datetime('now','localtime')`） | `2026-09-19 12:46:17` |
| token 过期时间（`expires_at`） | ISO8601 **UTC** | `2026-09-20T04:46:17+00:00` |
| 筛选日期参数（`date_from` / `date_to`） | `YYYY-MM-DD` | `2026-09-19` |
| 业务编号 | 前缀 + 日期 + 序号 | `r_daily`（预置报表）、`r_up_000003`、`c_20260919_0007`、`a_000123`、`run_20260919_04` |

### 2.6 幂等与重试

- `POST /reports/{report_code}/confirm`：**幂等**（已是 `ready` 时返回 `already_confirmed=true`，不重复入库）。
- `POST /reports/upload`：按文件 `sha256` 去重，重复上传同一文件返回 `is_duplicate=true` 并**复用**既有报表。
- `DELETE` 类：资源不存在返回 404（预置报表删除返回 403 `E_FORBIDDEN`，语义是"规则不允许"）。
- `POST /eval/runs`：发起新批次，**不幂等**（每次都是新批次，避免误判为"重试同一批次"）。
- SSE 端点（`/analysis/chat`）：**不要盲目重试**——链路里包含模型调用（烧额度）。客户端断开时
  服务端会取消链路，但已发生的模型调用不退费；UI 侧用"重试按钮"由用户显式触发。

### 2.7 跨域与前端代理

- 后端已启用 CORS（允许前端 dev 源），前端也可走 Vite 代理：`/api` → `http://127.0.0.1:8000`
  （`01.code/frontend/vite.config.ts`），因此前端代码里写**相对路径** `/api/v1/...` 即可。
- SSE 走 `POST`（不是 `GET`），**原生 `EventSource` 无法使用**：它不支持 POST 且不能带
  `Authorization` 头。前端用 `@microsoft/fetch-event-source` 发送并解析流（见 7.3）。

---

## 3. 错误码规范

### 3.1 双轨制说明

PRD 9.3 的 `E_*` 是**业务语义**，HTTP 状态码是**传输语义**，两者刻意分离：

- 网关 / 运维 / 前端全局拦截看 **HTTP 状态码**（401 就跳登录、429 就倒计时）；
- 业务分支（"是密码错还是账号锁了？"）看 **`data.error_code`**。

### 3.2 状态码 ↔ 错误码映射总表

| HTTP | `code` | `error_code` | 场景 |
| --- | --- | --- | --- |
| 400 | 400 | `E_VALIDATION` | 业务级参数不合法（密码强度、粒度不可再聚合、报表不可重跑等） |
| 400 | 400 | `E_FILE_TYPE` / `E_HEADER_INVALID` / `E_NO_TIME_COLUMN` / `E_NO_NUMERIC_COLUMN` / `E_PARSE` / `E_ENCODING` | 上传文件的格式 / 表头 / 时间列 / 数值列 / 解析 / 编码问题（6 个细分码，各带 `hint`） |
| 400 | 400 | `E_SQL_FORBIDDEN` / `E_CODE_FORBIDDEN` | 生成的 SQL / pandas 代码未通过安全守卫 |
| 400 | 400 | `E_OUT_OF_SCOPE` | 问题超出报表分析范围（如询问与报表无关的事） |
| 400 | 400 | `E_CHART_UNSUPPORTED` / `E_GRANULARITY_UNAVAILABLE` | 图表类型与数据形状不符 / 无法从快照重聚合到目标粒度 |
| 401 | 401 | `E_AUTH` | 未登录、token 缺失/非法/过期 |
| 401 | 401 | `E_CREDENTIALS` | 用户名或密码错误（**不区分账号是否存在**，防枚举） |
| 403 | 403 | `E_FORBIDDEN` | 越权访问他人资源；**预置报表不可删除**；沙箱/守卫拦截 |
| 403 | 403 | `E_ACCOUNT_DISABLED` | 账号已停用（`users.is_active = 0`） |
| 404 | 404 | `E_NOT_FOUND` | 资源不存在（**或不属于当前用户**） |
| 409 | 409 | `E_USERNAME_TAKEN` | 用户名已被占用 |
| 413 | 413 | `E_FILE_TOO_LARGE` | 上传文件超过 200MB 上限 |
| 422 | 422 | `E_VALIDATION` | 请求体结构 / 类型 / 长度校验失败（Pydantic；`message` 指出字段） |
| 429 | 429 | `E_AUTH_LOCKED` | 账号锁定中（连续失败 5 次锁 5 分钟；`data.locked_seconds` 给出剩余秒数） |
| 500 | 500 | `E_SQL_EXEC` / `E_CODE_EXEC` / `E_INGEST_FAILED` / `E_SANDBOX_TIMEOUT` / `E_LLM` / `E_INTERNAL` | 执行期错误：SQL 执行失败 / 沙箱代码报错 / 入库失败 / 沙箱超时 / 模型调用失败 / 未预期异常 |
| 501 | 501 | `E_NOT_IMPLEMENTED` | 能力未启用（当前仅 `GET /analysis/records/{code}/chart.png`：kaleido 属预留能力） |
| 504 | 504 | `E_LLM` | 模型阶段超时（如 slot 超 6s 且规则兜底也拿不到指标） |

### 3.3 带修复建议的错误（`data.hint`）

上传与 Agent 阶段的错误码**一律带 `hint`**（可直接展示给用户作为"下一步"）：

| 错误码 | 典型 `hint` |
| --- | --- |
| `E_FILE_TOO_LARGE` | 可将数据按月或按年拆分成多个文件分别上传（单文件 ≤200MB） |
| `E_FILE_TYPE` | 仅支持 .csv / .txt；Excel 文件请先另存为 CSV（UTF-8）后上传 |
| `E_HEADER_INVALID` | 请确保首行是列名、列名非空且不重复（重复列名加序号区分，如 用电量_1 / 用电量_2） |
| `E_NO_TIME_COLUMN` | 请在表头中加入日期/时间列，例如 `Date,Time,Global_active_power` |
| `E_NO_NUMERIC_COLUMN` | 请检查用电量/功率等列是否被文本符号污染（如 `1,2` 千分位、`kWh` 单位后缀） |
| `E_PARSE` | 请检查是否存在行列数不齐、字段内含未转义的分隔符；可先用 Excel 另存为 UTF-8 CSV |
| `E_ENCODING` | 请用记事本或 Excel 将文件另存为 UTF-8 编码后重试 |
| `E_GRANULARITY_UNAVAILABLE` | 该结果不含可再聚合的明细时间列（或请求粒度比结果更细），请重新提问 |
| `E_CHART_UNSUPPORTED` | 可改用：{supported}；也可不传 `chart_type` 由后端按数据形状自动选择 |
| `E_CODE_FORBIDDEN` | 系统只允许对当前报表数据做只读计算（禁止文件/网络/系统调用与反射） |
| `E_AUTH_LOCKED` | （`message` 已含剩余分钟数，`data.locked_seconds` 供前端倒计时） |

### 3.4 前端呈现映射（UIUX 9.2 / 9.4）

| 错误类别 | 呈现位置 | 说明 |
| --- | --- | --- |
| 401 `E_AUTH` | 全局拦截：清 token → 回登录页 | 附带"登录状态已失效，请重新登录" |
| 403 / 404 | 就地提示（Toast） | 不跳页；404 文案不区分"不存在"与"无权限" |
| 400 / 422 | 表单字段级错误 + Toast | `422` 的 `message` 已含字段名，可直接定位 |
| 429 `E_AUTH_LOCKED` | 登录表单内倒计时 | 用 `data.locked_seconds` 起倒计时，锁定期间禁用提交 |
| 5xx | 结果区错误卡片 | 提示"服务内部错误，请稍后重试" + `request_id`（便于对照后端日志） |

> **`request_id` 的用法**：任意报错都可让用户报出这个值，运维在后端日志里按 `rid=` 精确检索
> 一次请求的完整链路（含模型调用流水）。

---

## 4. 认证与个人中心（`auth`）

### 4.1 用户注册 `POST /auth/register`　*免鉴权*

**请求体**

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `username` | string | 是 | `^[A-Za-z0-9_]{4,20}$`；**不区分大小写**（服务端统一转小写存储与比对） |
| `password` | string | 是 | 8–64 字符，**必须同时含字母与数字**，UTF-8 编码后 ≤72 字节（bcrypt 硬限制） |
| `display_name` | string\|null | 否 | ≤32 字符 |
| `email` | string\|null | 否 | ≤64 字符（注册不校验格式，编辑资料时才校验） |

**响应 `data`**：`TokenOut` = `{ token, token_type: "Bearer", expires_at, user: UserOut }`
（**注册成功即自动登录**，前端无需再调登录接口）。

**错误**：409 `E_USERNAME_TAKEN`；400/422 `E_VALIDATION`（密码强度、用户名不合法）。

```bash
curl -X POST http://127.0.0.1:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username":"demo_ui","password":"demo12345","display_name":"演示账号"}'
```

### 4.2 用户登录 `POST /auth/login`　*免鉴权*

**请求体**：`{ username, password }`（`username` 4–20 位，`password` 1–64 位）。

**响应 `data`**：`TokenOut`（同 4.1）。

**锁定策略（PRD F6.2）**：

- 连续失败 **5 次** → 锁定 **5 分钟**（锁定期间即使密码正确也返回 **429 `E_AUTH_LOCKED`**，且**不再校验密码**）；
- **锁定到期不清零失败计数**，只有**成功登录**才清零；
- 用户名不存在与密码错误返回**同一文案**（401 `E_CREDENTIALS`），防账号枚举；
- `is_active = 0` → 403 `E_ACCOUNT_DISABLED`。

### 4.3 退出登录 `POST /auth/logout`

- 请求体：无。响应 `data`：`null`。
- 语义：**前端清除本地 token**；服务端落一条 `auth_logout` 审计（可追溯）。服务端**不做 token 失效**
  （无黑名单，见 2.2）。

### 4.4 当前用户信息 `GET /auth/me`

**响应 `data`**：`UserOut`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | int | 用户 id（内部使用；接口标识一律用 `username`） |
| `username` | string | 用户名（登录标识，**不可修改**） |
| `display_name` | string\|null | 显示名 |
| `email` | string\|null | 邮箱 |
| `last_login_at` | string\|null | 最近登录时间（本地时间字符串） |
| `created_at` | string | 注册时间 |

### 4.5 编辑资料 `PATCH /auth/me`

**请求体**（**只允许改这两项**；用户名不可改）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `display_name` | string\|null | 否 | ≤32 字符；空串视为清空（置 `null`） |
| `email` | string\|null | 否 | ≤64 字符；轻量格式校验（`本地部@域名.顶级`）；空串视为清空 |

**语义要点**：**未提交的字段保持原值**——服务端用 `model_fields_set` 区分"没传"与"显式传 null"，
因此只想改邮箱的请求不会顺手清空显示名。两个字段都不传 → 400 `E_VALIDATION`（避免"空操作成功"的假象）。

**响应 `data`**：更新后的 `UserOut`。**审计**：`auth_profile_updated`（含 before/after 快照）。

### 4.6 修改密码 `POST /auth/password`

**请求体**

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `old_password` | string | 是 | 当前密码（二次确认身份） |
| `new_password` | string | 是 | 新密码：**与注册同一套强度规则**（8–64 位、含字母与数字、≤72 字节） |

**语义要点**：

- 当前密码错误 → 400 `E_VALIDATION`（`message` = "当前密码不正确"），并落审计 `auth_password_change_failed`；
- 新旧密码相同 → 400 `E_VALIDATION`（"新密码不能与当前密码相同"）；
- **审计与 `payload` 严禁写入密码明文**（PRD S5 脱敏）。

**响应**：`data = null`，`message = "密码已修改（其它设备上的登录不会自动退出）"`。

### 4.7 清空历史记录 `POST /auth/clear-records`

**请求体**

| 字段 | 类型 | 必填 | 取值 | 说明 |
| --- | --- | --- | --- | --- |
| `scope` | string | 否（默认 `records`） | `records` \| `all` | `records` = 只清**分析记录**；`all` = 连**会话与对话历史**一起清 |

**两种口径的差异（重要）**

| 数据 | `scope=records` | `scope=all` |
| --- | --- | --- |
| 分析记录（SQL / 代码 / 结果快照 / 解读） | 删除 | 删除 |
| 模型调用流水 `llm_calls` | 随分析记录级联删除 | 同左 |
| 会话与对话文本 | **保留**（`messages.analysis_id` 被外键置空，消息本身还在） | **删除**（含置顶会话） |
| 评测批次（`eval_runs`） | **不受影响**（属评测资产，非"个人历史"） | 不受影响 |

**响应 `data`**（如实回显清掉了多少，便于核对）

```json
{ "scope": "all", "deleted_records": 12, "deleted_conversations": 3,
  "deleted_messages": 27, "deleted_llm_calls": 31 }
```

**审计**：`profile_records_cleared`（medium）。**前端要求**：危险操作必须二次确认后提交。

### 4.8 删除账号 `DELETE /auth/me`

**请求体**（**双确认**，防误触）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `password` | string | 是 | 当前密码 |
| `confirm_username` | string | 是 | **再次输入完整用户名**（服务端小写归一后比对） |

**删除链与保留物（不可撤销）**

1. **上传报表**：走 `report_write_service.delete_report`（进程内写锁 + `DROP TABLE` + 删落盘目录）；
   **预置报表不属于任何用户，天然不受影响**；单张报表清理失败**不阻断**账号删除（否则用户永远删不掉账号）；
2. **评测批次**：`eval_runs.user_id` 外键是 `ON DELETE SET NULL`（批次不随用户走），但逐题结果属个人数据 →
   **显式删除**；
3. **业务数据行**：删除 `users` 行 → 会话 / 消息 / 分析记录 / 报表元数据由 `ON DELETE CASCADE` 清理；
4. **安全日志**：`audit_logs.user_id` 是 `ON DELETE SET NULL` → **日志行保留、归属置空**
   （PRD F6.4「安全日志保留」），并写一条 `auth_account_deleted`（high）。

**响应 `data`**

```json
{ "username": "demo_ui", "deleted_reports": 2, "deleted_conversations": 11,
  "deleted_records": 130, "deleted_eval_runs": 8, "kept_audit_logs": 258 }
```

**前端要求**：成功响应后**立即清除本地 token 并跳转登录页**（账号已不存在，后续任何请求都会 401）。

> **实现提示**：`DELETE` 带请求体的写法——浏览器 `fetch` / `axios` 用 `data`（axios）传 body 即可；
> 部分客户端库（如 httpx 的 `client.delete()`）不接受 `body` 参数，需用通用的
> `request("DELETE", url, json=...)`。后端 FastAPI 侧无限制。

---

## 5. 报表管理（`reports`）

### 5.1 报表列表 `GET /reports`

返回当前用户可见的报表：**预置 5 张（全用户共享）+ 本人上传**（不分页）。

**响应 `data`**：`ReportListOut = { total, items: ReportOut[] }`

| 字段（`ReportOut`） | 类型 | 说明 |
| --- | --- | --- |
| `report_code` | string | 业务编号（`r_daily` 等预置 / `r_up_000003` 上传） |
| `filename` | string | 文件名 |
| `report_type` | string | `minute` / `hourly` / `daily` / `monthly` / `structure` / `uploaded` |
| `table_name` | string | `reports.db` 中的物理表名 |
| `time_start` / `time_end` | string\|null | 数据时间范围 |
| `row_count` / `column_count` | int\|null | 行数 / 列数 |
| `status` | string | `pending` / `parsing` / `ready` / `failed` / `deleted` |
| `is_preset` | int | 1 = 预置（共享） |
| `is_active` | int | 1 = **系统默认活跃报表**（注意：不是"当前用户最近活跃"） |
| `table_exists` | bool | 物理表是否真实存在（元数据与实际不一致时暴露出来） |
| `is_deletable` | bool | 是否允许删除（预置报表为 `false`）；**前端不要用 `is_preset` 自行推断规则** |
| `created_at` | string | 上传 / 入库时间 |

### 5.2 报表详情 `GET /reports/{report_code}`

**响应 `data`**：`ReportDetailOut` = `ReportOut` + 以下两项

| 追加字段 | 说明 |
| --- | --- |
| `caliber_version` | 口径版本（如 `caliber-v1.0`） |
| `quality_summary` | 质量摘要：`missing_rows` / `missing_ratio` / `time_axis_gap` / `duplicate_timestamps` / `voltage_out_of_range_minutes` / `checks`（口径校验项 → 是否通过） |

### 5.3 数据预览 `GET /reports/{report_code}/preview?limit=50`

| 参数 | 说明 |
| --- | --- |
| `limit` | 返回行数：默认 **50**，上限 **200**。**越界由 service 抛 400 `E_VALIDATION`**（不是框架 422），保持错误码语义一致 |

**响应 `data`**：`PreviewOut`

| 字段 | 说明 |
| --- | --- |
| `columns` | 真实表结构（取自 `PRAGMA table_info`，不依赖 `schema_json`） |
| `rows` | 行数据（按时间列正序；未知表回退 `rowid`） |
| `total_rows` | 元数据行数（**不跑 `COUNT(*)`**，避免 207 万行扫描） |
| `returned_rows` / `limit` / `truncated` | 实际返回行数 / 生效上限 / 是否被截断 |
| `order_by` | 排序依据列 |
| `excluded_columns` | 主动过滤掉的代理键列（如自增主键），保证预览列与 Schema 字典一致 |

### 5.4 报表 Schema `GET /reports/{report_code}/schema`

**响应 `data`**：`SchemaOut` — `structured`（结构化描述）、`text`（**与注入给模型逐字一致的文本**）、
`text_source`（`schema_text.md` 原文 / `generated` 上传表生成）、`est_tokens`（估算 token，预算 ≤2000）、
`truncated`（是否因超预算裁剪；**字段清单骨架永不裁剪**）。

### 5.5 数据质量报告 `GET /reports/{report_code}/quality`

**响应 `data`**：`QualityOut` — `quality` 含 `dataset`（数据集级）与 `checks`（本表相关口径校验）两段。

### 5.6 上传报表 `POST /reports/upload`（两段式第一步）

- **Content-Type**：`multipart/form-data`，文件字段名 **`file`**；支持 `.csv` / `.txt`，单文件 ≤**200MB**。
- **响应 `data`**（`UploadOut`，摘要）：`report_code`、`table_name`、`status="pending"`、
  `encoding` / `delimiter`（探测结果）、`row_count` / `column_count` / `raw_column_count`、
  `time_column`（识别出的时间列统一为 `dt`）、`time_start` / `time_end`、
  `is_duplicate`（命中 sha256 去重则为 `true`，未重复解析）、
  `column_mapping`（原始列名 → 安全列名，仅列改变项）、`columns[]`（含 `agg` 可否求和、`derived` 是否派生列）、
  `preview`（前 20 行）、`quality`、`pending_expires_at`（**未确认的存活截止时间**，超期被惰性清理回收）。
- **入库是两段式**：本接口只解析 + 登记为 `pending`；真正建表在 5.7。
- **错误**：413 `E_FILE_TOO_LARGE` / 400 `E_FILE_TYPE`、`E_HEADER_INVALID`、`E_NO_TIME_COLUMN`、
  `E_NO_NUMERIC_COLUMN`、`E_PARSE`、`E_ENCODING` / 500 `E_INGEST_FAILED`（均带 `hint`）。

```bash
curl -X POST http://127.0.0.1:8000/api/v1/reports/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@02.data/raw/household_power_consumption.csv"
```

### 5.7 确认入库 `POST /reports/{report_code}/confirm`（两段式第二步）

**响应 `data`**：`ConfirmOut` — `status="ready"`、`row_count` / `inserted_rows`、`indexes`、
`elapsed_ms`、`intermediate_removed`、`already_confirmed`（重复确认时为 `true`，**接口幂等**）。

### 5.8 设为会话当前数据源 `POST /reports/{report_code}/activate`

**请求体**：`{ conversation_code }`（这是"报表视角的语法糖"，等价于 `PATCH /conversations/{code}` 传
`active_report_code`）。

**响应 `data`**：`{ conversation_code, active_report_code }`。

### 5.9 删除报表 `DELETE /reports/{report_code}`

**响应 `data`**：`DeleteOut` — `dropped_table`（实际被 DROP 的表，本就不存在时为 `null`）、
`dropped_indexes`、`affected_conversations`（因元数据删除而被置空活跃报表的会话数）、
`freed_pages`（回收页数，**仅报告不 VACUUM**）、`files_removed`。

**错误**：403 `E_FORBIDDEN`（预置报表不可删除，`hint` 指向"改用其它活跃报表"）。

---

## 6. 会话（`conversations`）

### 6.1 新建会话 `POST /conversations`

**请求体**

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `title` | string\|null | 否 | ≤60 字符；不传时由首问摘要生成 |
| `active_report_code` | string\|null | 否 | 不传则按**三级回落**推导：显式 > 该用户最近会话 > 系统默认 |

**响应 `data`**：`ConversationCreateOut` — 会话字段 + **`active_report_source`**
（`explicit` / `last_conversation` / `system_default`），调用方永远知道活跃报表是怎么来的。

### 6.2 会话列表 `GET /conversations?q=&date_from=&date_to=`

- `q`：标题关键词模糊筛选；`date_from` / `date_to`：更新时间区间（`YYYY-MM-DD`）。
- 排序：**置顶组在前**（按 `pinned_at` 倒序），其余按更新时间倒序；不分页。
- **响应 `data`**：`{ total, items: ConversationOut[] }`；`ConversationOut` 含
  `conversation_code` / `title` / `active_report_code` / `is_pinned` / `pinned_at` /
  `message_count`（**实时分组 COUNT，不用快照列**）/ `last_message_at` / `created_at` / `updated_at`。

### 6.3 会话详情 `GET /conversations/{conversation_code}`

**响应 `data`**：`ConversationDetailOut` = 列表字段 + `slot`（该会话当前的结构化查询条件）。

### 6.4 重命名 / 置顶 / 切换数据源 `PATCH /conversations/{conversation_code}`

**请求体**（三项都可选，按需提交）：`{ title?, active_report_code?, is_pinned? }`

- 置顶上限 **每用户 5 条**，超出自动挤出最早的置顶；
  **响应 `data`** 里的 `evicted_pinned_code` 会告知被挤出的会话编号（正常为 `null`）。

### 6.5 删除会话 `DELETE /conversations/{conversation_code}`

级联清理消息与分析记录（安全日志保留）。**响应 `data`** 为删除影响的计数对象，`message = "会话已删除"`。

### 6.6 消息历史 `GET /conversations/{conversation_code}/messages?limit=50&before_seq=`

**游标分页**：默认最近 50 条（上限 200），`before_seq` 取序号更小的更早消息。

**响应 `data`**：`MessageListOut` — `items[]`（`MessageOut`：`seq_no` / `role` / `content` /
**`record_code`** / `created_at`）、`limit`、`has_more`、`oldest_seq`、`newest_seq`。

> `record_code` 的用途：前端"刷新页面后还原结果"用它调 `GET /analysis/records/{record_code}`，
> 把表格、图表与执行信息一并回放（只给数字 `analysis_id` 无法反查业务编号）。

---

## 7. 对话与分析（`analysis`）

### 7.1 提问 `POST /analysis/chat` — **SSE 流式**

**请求体**（`ChatIn`）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `conversation_code` | string | 是 | 会话编号（如 `c_20260919_0001`） |
| `report_code` | string\|null | 否 | 显式指定报表；与会话当前活跃报表不同则**先切换再分析**（PRD F1.6） |
| `message` | string | 是 | 用户问题，1–2000 字符 |
| `slot_patch` | object\|null | 否 | 条件芯片编辑后的回填（键白名单：8 个 slot 字段；先合入会话 slot 再做本轮增量抽取） |

**响应**：`Content-Type: text/event-stream`，帧格式与事件序列见 **7.3**。

**典型事件序列（正常成功）**

```
status(parsing) → slot → code(sql|pandas) → status(executing) → table → chart → narrative → meta → done
```

**澄清分支**（模型判断信息不足）

```
status(parsing) → slot(部分槽位) → clarify → meta → done(record_id=null)
```

**失败**：`error` 事件（业务失败：`BizError` 的 `code`/`message`/`detail`）→ `done`。
注意 **HTTP 状态码此时已是 200**（流已建立），失败信息在 `error` 事件里，前端必须监听它（不能只看 HTTP 状态）。

**示例（curl 观察流）**

```bash
curl -N -X POST http://127.0.0.1:8000/api/v1/analysis/chat \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"conversation_code":"c_20260919_0001","message":"全期总用电量是多少？"}'
```

### 7.2 澄清回填 `POST /analysis/clarify` — **SSE 流式**

用户点了澄清卡选项（或补全了条件）后调用，**就地继续执行**（不是重新提问）。

**请求体**（`ClarifyIn`）：`{ conversation_code, question, slot_patch }`
（`question` 是**被澄清的原始问题**，由前端从澄清卡上下文回传；`slot_patch` 是回填的条件）。

**响应**：同上，事件序列从 `slot` 继续（不再重复 `clarify`），直至 `done`（此时 `record_id` 为新记录编号）。

### 7.3 SSE 事件契约（chat / clarify 共用）

**帧格式**（标准 SSE；`data` 是 JSON **字符串**，需二次 `JSON.parse`）

```
event: table
data: {"columns":[{"name":"year","type":"INT"}],"rows":[{"year":2006,"total_kwh":1234.5}],"row_count":5,"truncated":false}

```

**事件一览**

| 事件 | 出现时机 | `data` 关键字段 | 前端处理建议 |
| --- | --- | --- | --- |
| `status` | 阶段切换 | `stage`：`parsing`（解析问题）/ `executing`（执行查询） | 映射为时间线上的"进行中"步骤 |
| `slot` | 槽位解析完成 | 8 个 slot 字段（澄清轮为**部分槽位**，空位如实留空） | 渲染条件芯片；空位显示"待补充" |
| `clarify` | 需要澄清 | `question`、`options[]`（选项含 `value` / `label`，指标类选项为全量指标清单，上限 8 项） | 弹澄清卡，用户提交后调 7.2 |
| `code` | 生成 SQL / 代码完成 | `route`：`sql`\|`pandas`，及 `sql` 或 `code` 正文 | 代码/ SQL 折叠块；**展示即所执行**（含守卫补的 LIMIT） |
| `table` | 执行完成 | `columns` / `rows` / `row_count` / `truncated` | 渲染结果表（数值对齐、分页由前端控制） |
| `chart` | 图表就绪 | Plotly `figure` + 口径标记（`layout.meta`） | `Plotly.react` 渲染；**失败只降级不出图，不影响表格** |
| `narrative` | 解读完成 | `text`、`key_numbers[]`、`anomalies[]` | 打字机式渲染；`anomalies` 作为"如实告知"提示展示 |
| `meta` | 结果元信息 | `elapsed_ms`、`retries`（自愈重试次数）、`llm_tokens`、`model`、`llm_impl` | 执行信息区展示耗时/成本 |
| `error` | 链路失败 | `code`（`E_*`）、`message`、`detail` | 结果区错误卡片；**必须监听**（HTTP 已是 200） |
| `done` | 流结束 | `record_id`（分析记录编号；澄清轮为 `null`） | 收尾；用 `record_id` 调详情接口固化回放数据 |

**连接与取消**

- 服务端**边跑边推**：首帧（`status: parsing`）在毫秒级送达（实测 11–16ms），不是等链路跑完再回放。
- **客户端断开 → 服务端取消链路**（少烧 token 与算力）；已发生的模型调用不退费。
- 阶段超时兜底：`slot` 超 **6s** → 按宽松规则兜底继续（拿不到指标才失败）；
  `narrative` 超 **8s** → **跳过解读**（表格与图表照给，`status` 仍成功，`anomalies` 里如实说明）。

---

## 8. 分析记录（`analysis/records`）

### 8.1 记录列表 `GET /analysis/records`

| 参数 | 说明 |
| --- | --- |
| `conversation_code` / `report_code` / `status` / `q` | 组合筛选（会话 / 报表 / 状态 / 问题关键词） |
| `date_from` / `date_to` | 创建时间区间（`YYYY-MM-DD`） |
| `page` / `page_size` | offset 分页（`page` 从 1 起，`page_size` 上限 100，默认 20） |

**响应 `data`**：`RecordListOut` — `total`、`page`、`page_size`、
**`status_count`**（当前筛选条件下的成功/失败计数，个人中心直接展示）、
`items[]`（`RecordItem`：`record_code` / `question` / `conversation_code` / `report_code` /
`route` / `status` / `error_code` / `elapsed_ms` / `llm_tokens` / `llm_model` / `created_at`）。

> 列表项是**轻字段**：不含 SQL 与解读正文（避免列表响应膨胀），正文走 8.2。

### 8.2 记录详情 `GET /analysis/records/{record_code}`

**响应 `data`**：`RecordDetailOut`，关键字段：

| 分组 | 字段 |
| --- | --- |
| 元信息 | `record_code` / `question` / `route` / `status` / `error_code` / `error_message` / `llm_model` / `elapsed_ms` / `prompt_version` / `schema_version` / `retries` / `path_switched` / `created_at` |
| 查询条件 | `slot`（PRD F2.1 的 8 个字段） |
| 执行内容 | `sql`（**实际执行的 SQL**，含守卫补的 LIMIT）、`code_text`（pandas 路径） |
| 结果 | `result`：`{ columns, rows, row_count, truncated }`（未执行时 `columns`/`rows` 为空、`row_count` 为 `null`） |
| 解读 | `narrative`、`key_numbers[]`、`anomalies[]` |
| 图表 | `chart`（Plotly figure） |
| 成本 | `llm_calls[]`（`stage` / `model_name` / `prompt_tokens` / `completion_tokens` / `total_tokens` / `elapsed_ms` / `status`）、`llm_tokens` |
| 重跑 | `can_replay`（原报表仍存在且 `ready`）、`replayed_from`（血缘）、`replay_note`（落地说明） |

### 8.3 重跑 `POST /analysis/records/{record_code}/replay`

**语义（决策 2）**：原问题**整轮重跑**，**报表锁定为原记录的报表**（不跟随会话当前活跃报表，
否则结果不可比），落一条**新记录**并返回新记录详情（非流式）。

**只读会话的去向（重要）**：若原记录属于**评测批次会话**（编号前缀 `c_eval_`，系统生成、仅供留痕），
重跑会**另起一个新会话**承载（`title = "重跑：<原问题前 36 字>"`），并在响应的
`replay_note` 里说明去向 —— 避免把提问与回答写进评测批次会话、污染留痕。

**错误**：400 `E_VALIDATION`（原报表已删除或不可用，无法重跑）。

### 8.4 删除记录 `DELETE /analysis/records/{record_code}`

**响应 `data`**：`RecordDeleteOut` — `{ record_code, deleted_messages, deleted_llm_calls }`
（`llm_calls` 级联删除；关联**消息保留**，仅解除引用，故 `deleted_messages` 表示"受影响的消息数"）。

---

## 9. 图表（`charts`）

### 9.1 图表重绘 `POST /charts/rerender`

用**已返回的数据**重绘，**不调用大模型**（PRD F3.2）。

**请求体**（`RerenderIn`）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `record_code` | string | 分析记录编号（放 body，因为该接口不属于 `/analysis/records/{code}` URL 树） |
| `chart_type` | string\|null | `line` / `bar` / `area` / `pie` / `scatter` / `table`；**显式指定即"指令"**，与形状不符 → 400 `E_CHART_UNSUPPORTED`（不静默兜底）；不传则沿用记录里的提示与形状兜底 |
| `granularity` | string\|null | 小时 / 日 / 月 / 年 / 全期；从**结果快照**重聚合（**只能由细变粗**），不可再拆细 → 400 `E_GRANULARITY_UNAVAILABLE` |
| `top_n` | int\|null | 分类类图的 TopN，默认 12，范围 3–50 |
| `sort` | string\|null | `natural` / `value_desc` / `value_asc` |

- 取值范围**不在 Pydantic 上校验**，统一由 service 抛 400 `E_VALIDATION`（避免框架 422 与业务错误码混用）。
- 成功后把新 figure **回写 `analyses.chart_json`**（右栏切过的图在"记录详情"里保持一致）。
- **量纲规则**：`kWh`/分钟求和、`kW`/`V`/`A` 取均值、**比率列丢弃**（R6 要求分子分母重算，而快照里没有）。

### 9.2 导出图表 PNG `GET /analysis/records/{record_code}/chart.png`

**当前返回 501 `E_NOT_IMPLEMENTED`**：kaleido 属**预留能力**（如实回报，不静默降级、不返回假图）。

---

## 10. 评测中心（`eval`）

### 10.1 题库列表 `GET /eval/questions`

| 参数 | 说明 |
| --- | --- |
| `qtype` | `stat` / `agg` / `compare` / `anomaly` / `structure` / `multi_turn` |
| `difficulty` | `easy` / `medium` / `hard` |
| `is_trap` | 是否口径陷阱题 |
| `page` / `page_size` | offset 分页（`page_size` 默认 50，上限 200） |

题库是**全局资产**（不做用户隔离），但**需登录**。返回题面、题型、难度、目标报表、`gold_sql` 与关键数值。

### 10.2 发起评测批次 `POST /eval/runs`（**异步**）

**请求体**（全部可选）

```json
{ "impl": "auto|real|selfcheck", "name": "批次备注名",
  "question_codes": ["q001","q002"], "limit": 20, "include_narrative": true }
```

- `impl=real` 且未配置模型 Key（`LLM_API_KEY`）→ 400 `E_VALIDATION`，`hint` 指向 `selfcheck`
  （**不静默回落到脚本模型**，避免"以为在测真实模型，其实在测桩"）。
- 题库为空 → 400（`hint` 给出建库命令 `04_build_eval_set.py`）。

**响应 `data`**：`{ run_code, status: "pending", impl, model_name, question_total }`
—— **立即返回**，前端轮询 10.3 直到 `finished` / `failed`（PRD 9.1 未定义评测进度 SSE 事件）。

### 10.3 批次详情 `GET /eval/runs/{run_code}?include_questions=true`

- `include_questions=false`：只要指标汇总（**轮询期间建议关掉**，避免每次拉全量逐题明细）。
- **响应 `data`**：指标汇总（`question_total` / `exec_correct_count` / `exec_correct_rate` /
  `narrative_hit_rate` / `avg_elapsed_ms` / `p95_elapsed_ms` / `retry_total` / `prompt_version` /
  `schema_version` / `model_name` / `temperature`）+ `metrics`（分类正确率、差异分布、错误分布、
  判分档、归因分布）+ `questions[]`（逐题：`generated_sql_or_code` / `matched_tier` / `diff_type` /
  `key_hit_rate` / `elapsed_ms` / `retries` / `error_code` / `narrative`）。
- `status=running` 时明细为空数组（继续轮询）。

### 10.4 批次列表 `GET /eval/runs?page=1&page_size=20`

**仅本人**，按 `created_at DESC, id DESC`。

### 10.5 下载评测报告 `GET /eval/runs/{run_code}/report`

- `Content-Type: text/markdown; charset=utf-8`，带 `Content-Disposition: attachment`。
- 批次未完成 → 400；报告文件缺失 → 404。

### 10.6 下载评测报告（PDF）`GET /eval/runs/{run_code}/report.pdf`

- `Content-Type: application/pdf`，与 Markdown 报告**同源渲染**（`report_pdf.to_pdf`，`reportlab`）。
- 前端"导出"按钮**默认走 PDF**（Markdown 需工具才排版，PDF 可直接阅读与打印）。
- 同步 CPU 渲染放在线程池执行（几十毫秒级），不阻塞事件循环。

---

## 11. 系统与运维（`system`）

### 11.1 健康检查 `GET /system/health`　*免鉴权*

**响应 `data`**

```json
{
  "status": "healthy",
  "llm": { "configured": true, "impl": "aliyun-maas", "model_main": "…", "model_light": "…",
           "provider": "…", "protocol": "openai-compatible", "key_source": "LLM_API_KEY" },
  "databases": { "reports": "ok", "power_agent": "ok" }
}
```

- `status`：`healthy` / `degraded`（任一库不可达时为 `degraded`，**不抛异常**，只如实回报）。
- `llm.configured=false` 时 `impl=scripted`：分析链路走**离线脚本模型**，仍可端到端跑通，
  但结果**不是真实模型产出**（评测报告如是标注）。
- 该接口**不校验 token**，便于探活与部署自检。

### 11.2 注入用 Schema 文本 `GET /system/schema-text?conversation_code=`

取值规则：`conversation_code` 优先（会话级活跃报表是唯一权威），缺省回落系统默认活跃报表
（`uploaded_reports.is_active = 1`）。响应回显 `report_code` 与 `source`（`conversation` / `system_default`），
含 `text`（注入文本）、`text_source`、`est_tokens`、`truncated`。

---

## 附录 A. 端点总表（自动生成）

> 由 `01.code/tools/api_doc.py` 从 OpenAPI 同步；**不要手工编辑本节**，改接口后重跑工具即可。

<!-- BEGIN:ROUTES -->

#### 认证与个人中心（auth）

| 方法 | 路径 | 说明 | 鉴权 | 路径参数 | 查询参数 | 请求体 |
| --- | --- | --- | --- | --- | --- | --- |
| `POST` | `/auth/clear-records` | 清空历史记录（records=只清记录 / all=连会话对话） | 是 | — | — | ClearRecordsIn |
| `POST` | `/auth/login` | 用户登录（连续 5 次失败锁定 5 分钟） | **否** | — | — | LoginIn |
| `POST` | `/auth/logout` | 退出登录（前端清 token，服务端落审计） | 是 | — | — | — |
| `GET` | `/auth/me` | 当前登录用户信息 | 是 | — | — | — |
| `PATCH` | `/auth/me` | 编辑资料（显示名 / 邮箱） | 是 | — | — | ProfileUpdateIn |
| `DELETE` | `/auth/me` | 删除账号（密码 + 用户名双确认，不可撤销） | 是 | — | — | DeleteAccountIn |
| `POST` | `/auth/password` | 修改密码（需校验当前密码） | 是 | — | — | PasswordChangeIn |
| `POST` | `/auth/register` | 用户注册（成功即自动登录并签发 JWT） | **否** | — | — | RegisterIn |

#### 报表管理（reports）

| 方法 | 路径 | 说明 | 鉴权 | 路径参数 | 查询参数 | 请求体 |
| --- | --- | --- | --- | --- | --- | --- |
| `GET` | `/reports` | 报表列表（预置共享 + 本人上传） | 是 | — | — | — |
| `POST` | `/reports/upload` | 上传报表（解析 + 建表，待确认） | 是 | — | — | — |
| `GET` | `/reports/{report_code}` | 报表详情（元数据 + 质量摘要） | 是 | `report_code` | — | — |
| `DELETE` | `/reports/{report_code}` | 删除报表（含物理表与落盘文件） | 是 | `report_code` | — | — |
| `POST` | `/reports/{report_code}/activate` | 设为会话当前数据源 | 是 | `report_code` | — | ActivateReportIn |
| `POST` | `/reports/{report_code}/confirm` | 确认上传入库（提交写锁） | 是 | `report_code` | — | — |
| `GET` | `/reports/{report_code}/preview` | 数据预览（前 N 行 + 排序依据） | 是 | `report_code` | `limit` | — |
| `GET` | `/reports/{report_code}/quality` | 数据质量报告（数据集级 + 本表口径校验） | 是 | `report_code` | — | — |
| `GET` | `/reports/{report_code}/schema` | 报表 Schema（结构化 + 注入文本原文） | 是 | `report_code` | — | — |

#### 会话（conversations）

| 方法 | 路径 | 说明 | 鉴权 | 路径参数 | 查询参数 | 请求体 |
| --- | --- | --- | --- | --- | --- | --- |
| `GET` | `/conversations` | 会话列表（关键词 + 时间筛选） | 是 | — | `q`、`date_from`、`date_to` | — |
| `POST` | `/conversations` | 新建会话（可指定初始数据源） | 是 | — | — | ConversationCreateIn |
| `GET` | `/conversations/{conversation_code}` | 会话详情 | 是 | `conversation_code` | — | — |
| `PATCH` | `/conversations/{conversation_code}` | 重命名 / 置顶会话 | 是 | `conversation_code` | — | ConversationPatchIn |
| `DELETE` | `/conversations/{conversation_code}` | 删除会话（级联对话与分析记录） | 是 | `conversation_code` | — | — |
| `GET` | `/conversations/{conversation_code}/messages` | 消息历史（游标分页） | 是 | `conversation_code` | `limit`、`before_seq` | — |

#### 对话与分析（含 SSE 流式）（analysis）

| 方法 | 路径 | 说明 | 鉴权 | 路径参数 | 查询参数 | 请求体 |
| --- | --- | --- | --- | --- | --- | --- |
| `POST` | `/analysis/chat` | 对话分析（SSE 流式返回） | 是 | — | — | ChatIn |
| `POST` | `/analysis/clarify` | 澄清回填后就地继续执行（SSE） | 是 | — | — | ClarifyIn |
| `GET` | `/analysis/records` | 分析记录列表（多条件筛选 + 分页） | 是 | — | `conversation_code`、`report_code`、`status`、`q`、`date_from`、`date_to`、`page`、`page_size` | — |
| `GET` | `/analysis/records/{record_code}` | 分析记录详情（含逐阶段模型调用明细） | 是 | `record_code` | — | — |
| `DELETE` | `/analysis/records/{record_code}` | 删除分析记录 | 是 | `record_code` | — | — |
| `POST` | `/analysis/records/{record_code}/replay` | 重跑（锁定原报表，另存新记录） | 是 | `record_code` | — | — |

#### 图表（charts）

| 方法 | 路径 | 说明 | 鉴权 | 路径参数 | 查询参数 | 请求体 |
| --- | --- | --- | --- | --- | --- | --- |
| `GET` | `/analysis/records/{record_code}/chart.png` | 导出图表 PNG（预留能力，当前返回 501） | 是 | `record_code` | — | — |
| `POST` | `/charts/rerender` | 图表重绘（用已返回数据，不调用模型） | 是 | — | — | RerenderIn |

#### 评测中心（eval）

| 方法 | 路径 | 说明 | 鉴权 | 路径参数 | 查询参数 | 请求体 |
| --- | --- | --- | --- | --- | --- | --- |
| `GET` | `/eval/questions` | 题库列表（120 题全局资产） | 是 | — | `qtype`、`difficulty`、`is_trap`、`page`、`page_size` | — |
| `GET` | `/eval/runs` | 评测批次列表（仅本人） | 是 | — | `page`、`page_size` | — |
| `POST` | `/eval/runs` | 发起评测批次（异步，立即返回 run_code） | 是 | — | — | — |
| `GET` | `/eval/runs/{run_code}` | 批次详情（指标汇总 + 逐题明细） | 是 | `run_code` | `include_questions` | — |
| `GET` | `/eval/runs/{run_code}/report` | 下载评测报告（Markdown） | 是 | `run_code` | — | — |
| `GET` | `/eval/runs/{run_code}/report.pdf` | 下载评测报告（PDF） | 是 | `run_code` | — | — |

#### 系统与运维（system）

| 方法 | 路径 | 说明 | 鉴权 | 路径参数 | 查询参数 | 请求体 |
| --- | --- | --- | --- | --- | --- | --- |
| `GET` | `/system/health` | 健康检查（进程 + 两库 + 模型配置） | **否** | — | — | — |
| `GET` | `/system/schema-text` | 注入用 Schema 文本（按会话取当前数据源） | 是 | — | `conversation_code` | — |

> 本表由 `01.code/tools/api_doc.py` 从 **OpenAPI schema 自动生成**（共 39 条端点，其中免鉴权 3 条），与代码同源。改接口后重跑 `python 01.code/tools/api_doc.py --write` 即可同步，避免手工清单过时。

<!-- END:ROUTES -->

---

## 附录 B. 错误码速查（按字母序）

| 错误码 | HTTP | 一句话说明 |
| --- | --- | --- |
| `E_ACCOUNT_DISABLED` | 403 | 账号已停用 |
| `E_AUTH` | 401 | 未登录 / token 失效 |
| `E_AUTH_LOCKED` | 429 | 登录失败次数过多，账号锁定中（含 `locked_seconds`） |
| `E_CHART_UNSUPPORTED` | 400 | 指定的图表类型与数据形状不符 |
| `E_CODE_EXEC` | 500 | pandas 沙箱内代码运行报错（含 `error_type` / `line_no`） |
| `E_CODE_FORBIDDEN` | 400 | pandas 代码未通过 AST 安全预检（`rule_id`） |
| `E_CREDENTIALS` | 401 | 用户名或密码错误 |
| `E_ENCODING` | 400 | 文件编码无法识别 |
| `E_FILE_TOO_LARGE` | 413 | 文件超过 200MB |
| `E_FILE_TYPE` | 400 | 文件类型不支持 |
| `E_FORBIDDEN` | 403 | 越权 / 预置报表不可删 |
| `E_GRANULARITY_UNAVAILABLE` | 400 | 无法从快照重聚合到目标粒度 |
| `E_HEADER_INVALID` | 400 | 表头不合法（空列名 / 重复列名） |
| `E_INGEST_FAILED` | 500 | 入库失败（含 `rolled_back`） |
| `E_INTERNAL` | 500 | 服务内部错误 |
| `E_LLM` | 500/504 | 模型调用失败 / 阶段超时 |
| `E_NOT_FOUND` | 404 | 资源不存在（或不属于当前用户） |
| `E_NOT_IMPLEMENTED` | 501 | 能力未启用（当前：图表 PNG 导出） |
| `E_NO_NUMERIC_COLUMN` | 400 | 未找到可分析的数值列 |
| `E_NO_TIME_COLUMN` | 400 | 未识别到时间列 |
| `E_OUT_OF_SCOPE` | 400 | 问题超出报表分析范围 |
| `E_PARSE` | 400 | 文件解析失败（含 `line_no`） |
| `E_SANDBOX_TIMEOUT` | 500 | 沙箱执行超时 |
| `E_SQL_EXEC` | 500 | SQL 执行失败 |
| `E_SQL_FORBIDDEN` | 400 | SQL 未通过安全守卫（`rule_id`） |
| `E_USERNAME_TAKEN` | 409 | 用户名已被占用 |
| `E_VALIDATION` | 400 / 422 | 参数不合法（400 = 业务校验，422 = 结构/类型） |

---

## 附录 C. 变更记录

| 版本 | 日期 | 变更 |
| --- | --- | --- |
| v1.0.0 | 2026-09-19 | 首版：39 条端点契约成文（含 4 条个人中心端点、SSE 事件契约、错误码总表）；新增同步工具 `01.code/tools/api_doc.py`（端点表与 OpenAPI 同源）；同时给全部 39 条路由补中文 `summary`（Swagger UI 一并转中文）；对应后端 `prompt-v3.7` / 用例 295 条状态（**契约未变**：prompt v3.6/v3.7 只改提示词规则表述，接口字段与错误码一字未动） |

> **契约稳定性说明（2026-09-19）**：prompt-v3.6 / v3.7 的两次修订**只改提示词内的规则表述**，
> 因此本文档的端点、字段、错误码、SSE 事件契约**全部不变**；仅"当前提示词版本"与用例条数随之更新。

---

> **维护约定**：改动接口时，请同步做三件事 —— ① 路由装饰器写中文 `summary`；
> ② 跑 `api_doc.py --write` 同步附录 A；③ 在附录 C 记一行（以及《PRD》9.1 接口清单如涉及）。
