-- ============================================================================
-- 智电析报 · 系统数据库 DDL（唯一事实源之一）
-- 文件：01.code/scripts/sql/power_agent.sql
-- 目标库：02.data/power_agent.db
-- 对应 PRD v1.3.0 第 7.4 节（系统数据库需求）
--
-- 职责（见 PRD 7.4 / 6.3 S8）：
--   - 本库承载【用户、会话、消息、分析记录、报表元数据、评测】
--   - 读写库；所有写入必须使用参数化 SQL（ORM 或绑定变量），禁止字符串拼接
--   - 每个连接都必须执行 PRAGMA foreign_keys = ON（SQLite 默认关闭）
--   - 删除行为全链路 ON DELETE CASCADE，与 PRD 7.4 约束说明一致
--
-- 通用约定：
--   - 物理主键 INTEGER PRIMARY KEY AUTOINCREMENT
--   - 对外业务编号列（*_code）TEXT UNIQUE，用于 URL 与前端展示
--   - 时间统一 TEXT ISO8601 'YYYY-MM-DD HH:MM:SS'
--   - JSON 类字段统一以 TEXT 存储，字段名以 _json 结尾，写入前校验可解析
--   - 布尔统一 INTEGER 0/1
--
-- 表定义顺序说明：
--   本文件按【依赖关系】排列（先被引用的表在前，便于阅读外键指向）；
--   《智电析报_数据库设计文档》第 5 节按 PRD 7.4 的业务顺序排列。
--   两者顺序不同，内容完全一致，均以本文件为字段级唯一事实源。
-- ============================================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- ----------------------------------------------------------------------------
-- 1. users —— 用户账号
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    username         TEXT    NOT NULL UNIQUE,          -- 4-20 位
    password_hash    TEXT    NOT NULL,                 -- bcrypt 哈希（禁止明文/可逆加密）
    display_name     TEXT,
    email            TEXT,
    is_active        INTEGER NOT NULL DEFAULT 1,
    login_fail_count INTEGER NOT NULL DEFAULT 0,       -- 连续失败 5 次锁定 5 分钟
    locked_until     TEXT,
    last_login_at    TEXT,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at       TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    CHECK (is_active IN (0, 1))
);

CREATE INDEX IF NOT EXISTS ix_users_created ON users (created_at);

-- ----------------------------------------------------------------------------
-- 2. uploaded_reports —— 报表元数据（预置 5 张 + 用户上传的动态表）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS uploaded_reports (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    report_code      TEXT    NOT NULL UNIQUE,          -- 如 'r_daily' / 'r_up_000003'
    user_id          INTEGER REFERENCES users (id) ON DELETE CASCADE,  -- 预置报表为 NULL
    filename         TEXT    NOT NULL,                 -- 原始文件名
    table_name       TEXT    NOT NULL,                 -- reports.db 中的物理表名
    report_type      TEXT    NOT NULL,                 -- minute/hourly/daily/monthly/structure/uploaded
    row_count        INTEGER,
    column_count     INTEGER,
    time_start       TEXT,                             -- 数据起始时间
    time_end         TEXT,                             -- 数据结束时间
    caliber_version  TEXT,                             -- 口径版本，如 'caliber-v1.0'
    is_preset        INTEGER NOT NULL DEFAULT 0,       -- 1=系统预置报表
    is_active        INTEGER NOT NULL DEFAULT 0,       -- 当前会话活跃标记
    status           TEXT    NOT NULL DEFAULT 'ready', -- pending/parsing/ready/failed/deleted
    schema_json      TEXT,                             -- Schema 结构化描述（注入用）
    quality_json     TEXT,                             -- 数据质量报告
    error_message    TEXT,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at       TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    CHECK (report_type IN ('minute', 'hourly', 'daily', 'monthly', 'structure', 'uploaded')),
    CHECK (is_preset IN (0, 1)),
    CHECK (is_active IN (0, 1)),
    CHECK (status IN ('pending', 'parsing', 'ready', 'failed', 'deleted')),
    UNIQUE (user_id, table_name)
);

CREATE INDEX IF NOT EXISTS ix_reports_user      ON uploaded_reports (user_id, created_at);
CREATE INDEX IF NOT EXISTS ix_reports_type      ON uploaded_reports (report_type);
CREATE INDEX IF NOT EXISTS ix_reports_active    ON uploaded_reports (is_active);
CREATE INDEX IF NOT EXISTS ix_reports_table     ON uploaded_reports (table_name);

-- ----------------------------------------------------------------------------
-- 3. upload_files —— 上传文件落盘元数据（与 uploaded_reports 一对一）
--    落盘目录：02.data/uploads/{user_id}/{report_code}/{原始文件名}
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS upload_files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id       INTEGER NOT NULL REFERENCES uploaded_reports (id) ON DELETE CASCADE,
    origin_filename TEXT    NOT NULL,
    stored_path     TEXT    NOT NULL,                 -- 相对 02.data/ 的路径（不存绝对路径）
    file_size       INTEGER,                          -- 字节
    sha256          TEXT,                             -- 指纹，用于去重与完整性校验
    encoding        TEXT,                             -- 探测结果：utf-8 / utf-8-sig / gbk
    delimiter       TEXT,                             -- ',' / ';' / '\t'
    uploaded_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    status          TEXT    NOT NULL DEFAULT 'stored',-- stored/parsed/failed
    error_message   TEXT,
    CHECK (status IN ('stored', 'parsed', 'failed'))
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_upload_files_report ON upload_files (report_id);
CREATE INDEX        IF NOT EXISTS ix_upload_files_sha256 ON upload_files (sha256);

-- ----------------------------------------------------------------------------
-- 4. conversations —— 会话（slot 持久化于此，供多轮追问继承）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS conversations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_code TEXT   NOT NULL UNIQUE,         -- 如 'c_20260915_0001'
    user_id          INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    title            TEXT,                            -- 默认取首问摘要
    active_report_id INTEGER REFERENCES uploaded_reports (id) ON DELETE SET NULL,
    slot_json        TEXT,                            -- 结构化查询条件（时间/粒度/指标/过滤/对比）
    message_count    INTEGER NOT NULL DEFAULT 0,
    last_message_at  TEXT,
    is_pinned        INTEGER NOT NULL DEFAULT 0,      -- 1=置顶（每用户最多 5 条，见 UIUX 5.1）
    pinned_at        TEXT,                            -- 置顶时间（置顶组按此倒序）
    created_at       TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at       TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    CHECK (is_pinned IN (0, 1))
);

CREATE INDEX IF NOT EXISTS ix_conv_user      ON conversations (user_id, updated_at);
CREATE INDEX IF NOT EXISTS ix_conv_report    ON conversations (active_report_id);
CREATE INDEX IF NOT EXISTS ix_conv_pinned    ON conversations (user_id, is_pinned, pinned_at);

-- ----------------------------------------------------------------------------
-- 5. analyses —— 分析记录（一次提问一行；结果快照与图表 JSON 存此）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS analyses (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    record_code           TEXT    NOT NULL UNIQUE,    -- 如 'a_000123'（对外业务编号，PRD 9.1 URL 用 {record_code}）
    user_id               INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    conversation_id       INTEGER REFERENCES conversations (id) ON DELETE CASCADE,
    report_id             INTEGER REFERENCES uploaded_reports (id) ON DELETE CASCADE,
    question              TEXT    NOT NULL,           -- 用户原始问题
    slot_json             TEXT,                       -- 本次生效的查询条件
    route                 TEXT,                       -- sql / pandas
    sql_text              TEXT,                       -- 生成的 SQL
    code_text             TEXT,                       -- 生成的 pandas 代码
    result_columns_json   TEXT,                       -- 结果列名与类型
    result_rows_json      TEXT,                       -- 结果快照（≤200KB，超出截断并置 result_truncated=1）
    result_row_count      INTEGER,
    result_truncated      INTEGER NOT NULL DEFAULT 0,
    chart_json            TEXT,                       -- Plotly figure JSON
    narrative             TEXT,                       -- 文字解读
    key_numbers_json      TEXT,                       -- 解读中的关键数值（供结论准确性判分）
    anomalies_json        TEXT,                       -- 识别到的异常项
    retries               INTEGER NOT NULL DEFAULT 0,
    path_switched         INTEGER NOT NULL DEFAULT 0, -- 1=发生过换路径重试
    status                TEXT    NOT NULL,           -- success / failed
    error_code            TEXT,
    error_message         TEXT,
    llm_model             TEXT,
    elapsed_ms            INTEGER,
    prompt_version        TEXT,
    schema_version        TEXT,
    created_at            TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    CHECK (route IS NULL OR route IN ('sql', 'pandas')),
    CHECK (status IN ('success', 'failed')),
    CHECK (result_truncated IN (0, 1)),
    CHECK (path_switched IN (0, 1))
);

CREATE INDEX IF NOT EXISTS ix_analyses_conv   ON analyses (conversation_id, created_at);
CREATE INDEX IF NOT EXISTS ix_analyses_user   ON analyses (user_id, created_at);
CREATE INDEX IF NOT EXISTS ix_analyses_report ON analyses (report_id);
CREATE INDEX IF NOT EXISTS ix_analyses_status ON analyses (status);

-- ----------------------------------------------------------------------------
-- 6. messages —— 对话消息流（用户提问 / 助手回答，执行明细见 analyses）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    user_id         INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    seq_no          INTEGER NOT NULL,                 -- 会话内顺序号，从 1 开始
    role            TEXT    NOT NULL,                 -- user / assistant / system
    content         TEXT,                             -- 文本内容（助手回答可为解读摘要）
    analysis_id     INTEGER REFERENCES analyses (id) ON DELETE SET NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    CHECK (role IN ('user', 'assistant', 'system')),
    UNIQUE (conversation_id, seq_no)
);

CREATE INDEX IF NOT EXISTS ix_messages_conv ON messages (conversation_id, seq_no);
CREATE INDEX IF NOT EXISTS ix_messages_user ON messages (user_id, created_at);

-- ----------------------------------------------------------------------------
-- 7. llm_calls —— 大模型调用流水（耗时/token/重试；支撑 N1/N2 与 B2 限流调优）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS llm_calls (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id       INTEGER REFERENCES analyses (id) ON DELETE CASCADE,
    eval_run_id       INTEGER REFERENCES eval_runs (id) ON DELETE CASCADE,
    stage             TEXT    NOT NULL,               -- slot / gen_sql / gen_pandas / retry / narrative / clarify
    model_name        TEXT    NOT NULL,
    temperature       REAL,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    total_tokens      INTEGER,
    elapsed_ms        INTEGER,
    first_token_ms    INTEGER,                        -- 流式首字延迟
    is_retry          INTEGER NOT NULL DEFAULT 0,
    http_status       INTEGER,
    status            TEXT    NOT NULL,               -- success / failed
    error_message     TEXT,
    request_id        TEXT,                           -- 与后端日志贯穿的请求 ID
    created_at        TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    CHECK (is_retry IN (0, 1)),
    CHECK (status IN ('success', 'failed'))
);

CREATE INDEX IF NOT EXISTS ix_llm_calls_analysis ON llm_calls (analysis_id);
CREATE INDEX IF NOT EXISTS ix_llm_calls_run      ON llm_calls (eval_run_id);
CREATE INDEX IF NOT EXISTS ix_llm_calls_stage    ON llm_calls (stage, created_at);

-- ----------------------------------------------------------------------------
-- 8. audit_logs —— 安全审计日志（支撑 B5 安全用例集拦截通过率统计）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER REFERENCES users (id) ON DELETE SET NULL,
    conversation_id INTEGER REFERENCES conversations (id) ON DELETE SET NULL,
    event_type      TEXT    NOT NULL,                 -- sql_blocked/code_blocked/injection_attempt/auth_forbidden/cascade_delete
                                                      -- + auth_register/auth_login/auth_login_failed/auth_locked/auth_logout
                                                      -- + narrative_number_warn（解读编造告警）
                                                      -- + auth_profile_updated/auth_password_changed/auth_password_change_failed
                                                      -- + profile_records_cleared/auth_account_deleted/auth_delete_account_failed
    severity        TEXT    NOT NULL DEFAULT 'medium',-- low / medium / high
    rule_id         TEXT,                             -- 命中的规则或校验项（如 'sqlglot:non_select'）
    action          TEXT    NOT NULL,                 -- blocked / warned / allowed
    payload         TEXT,                             -- 被拦截内容（截断至 2000 字符）
    ip              TEXT,
    user_agent      TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    -- 白名单随功能扩展（v1.19.12）：新增 7 类事件 —— 个人中心四件事（编辑资料 / 改密码 /
    -- 清空历史 / 删除账号）与解读"未授权数字"告警。**SQLite 无法 ALTER CHECK**，
    -- 老库用 `01.code/scripts/05_migrate_audit_events.py` 重建本表（幂等）。
    CHECK (event_type IN ('sql_blocked', 'code_blocked', 'injection_attempt', 'auth_forbidden',
                          'cascade_delete', 'auth_register', 'auth_login', 'auth_login_failed',
                          'auth_locked', 'auth_logout',
                          'narrative_number_warn', 'auth_profile_updated', 'auth_password_changed',
                          'auth_password_change_failed', 'profile_records_cleared',
                          'auth_account_deleted', 'auth_delete_account_failed')),
    CHECK (severity IN ('low', 'medium', 'high')),
    CHECK (action IN ('blocked', 'warned', 'allowed'))
);

CREATE INDEX IF NOT EXISTS ix_audit_event  ON audit_logs (event_type, created_at);
CREATE INDEX IF NOT EXISTS ix_audit_user   ON audit_logs (user_id, created_at);
CREATE INDEX IF NOT EXISTS ix_audit_action ON audit_logs (action);

-- ----------------------------------------------------------------------------
-- 9. eval_questions —— 评测问题集（120 题，含 gold 答案与关键数值）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS eval_questions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    question_code    TEXT    NOT NULL UNIQUE,         -- 如 'q001'
    question         TEXT    NOT NULL,
    qtype            TEXT    NOT NULL,                -- stat/agg/compare/anomaly/structure/multi_turn
    difficulty       TEXT    NOT NULL DEFAULT 'medium',-- easy / medium / hard
    report_table     TEXT    NOT NULL,                -- 目标报表表名
    gold_sql         TEXT,                            -- 标准 SQL（与 gold_code 至少一项）
    gold_code        TEXT,                            -- 标准 pandas 代码（可选）
    gold_answer_json TEXT,                            -- 真实执行得到的标准结果
    key_numbers_json TEXT,                            -- 用于结论判定的关键数值
    is_trap          INTEGER NOT NULL DEFAULT 0,      -- 1=口径陷阱题
    trap_rule        TEXT,                            -- 命中的红线规则：R1量纲/R3正交/R6比率/R7全期行
    tags             TEXT,
    sort_no          INTEGER,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    CHECK (qtype IN ('stat', 'agg', 'compare', 'anomaly', 'structure', 'multi_turn')),
    CHECK (difficulty IN ('easy', 'medium', 'hard')),
    CHECK (is_trap IN (0, 1))
);

CREATE INDEX IF NOT EXISTS ix_eval_q_type ON eval_questions (qtype, difficulty);
CREATE INDEX IF NOT EXISTS ix_eval_q_trap ON eval_questions (is_trap);

-- ----------------------------------------------------------------------------
-- 10. eval_runs —— 评测批次与指标汇总
--     逐题明细存 result_json（如需按题型/难度做 SQL 级统计，后续可拆出 eval_run_items 表）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS eval_runs (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_code              TEXT    NOT NULL UNIQUE,    -- 如 'run_20260920_01'
    user_id               INTEGER REFERENCES users (id) ON DELETE SET NULL,
    name                  TEXT,
    model_name            TEXT,
    temperature           REAL    DEFAULT 0,          -- 评测固定 0，保证可复现
    prompt_version        TEXT,
    schema_version        TEXT,
    question_total        INTEGER NOT NULL DEFAULT 0,
    exec_correct_count    INTEGER NOT NULL DEFAULT 0,
    exec_correct_rate     REAL,                       -- 执行正确率（目标 ≥0.85）
    narrative_hit_rate    REAL,                       -- 结论关键数值命中率（目标 ≥0.90）
    avg_elapsed_ms        INTEGER,
    p95_elapsed_ms        INTEGER,
    first_token_avg_ms    INTEGER,
    retry_total           INTEGER DEFAULT 0,
    result_json           TEXT,                       -- 逐题明细（生成 SQL/代码/答案/对错/耗时）
    report_path           TEXT,                       -- 评测报告文件相对路径
    status                TEXT    NOT NULL DEFAULT 'pending', -- pending/running/finished/failed
    error_message         TEXT,
    started_at            TEXT,
    finished_at           TEXT,
    created_at            TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    CHECK (status IN ('pending', 'running', 'finished', 'failed'))
);

CREATE INDEX IF NOT EXISTS ix_eval_runs_status ON eval_runs (status, created_at);
CREATE INDEX IF NOT EXISTS ix_eval_runs_user   ON eval_runs (user_id, created_at);
