/**
 * 后端接口类型（**逐字段对齐** `01.code/backend/app/schemas/*.py`）。
 *
 * 为什么不自动生成？——后端端点返回 `dict`，OpenAPI 里响应体是 `additionalProperties: true`，
 * 没有可用的 schema（见 `_openapi.json`）。因此这里手写，字段名以后端 Pydantic 模型为准，
 * 改动后端响应模型时**必须同步本文件**。
 */

// ── 通用包络（PRD 9.1 / app/schemas/common.py）───────────────────────────────

/** 成功包络：`code = 0`；失败包络：`code = HTTP 状态码`，业务错误码在 `data.error_code` */
export interface Envelope<T> {
  code: number;
  message: string;
  data: T;
  request_id: string;
}

/** 失败包络的 `data`（含业务错误码与定位信息） */
export interface ErrorData {
  error_code: string;
  hint?: string;
  rule_id?: string;
  error_type?: string;
  line_no?: number;
  trace_tail?: string[];
  supported?: string[];
  [key: string]: unknown;
}

// ── 认证（app/schemas/auth.py）──────────────────────────────────────────────

export interface User {
  id: number;
  username: string;
  display_name: string | null;
  email: string | null;
  last_login_at: string | null;
  created_at: string;
}

export interface TokenOut {
  token: string;
  token_type: string;
  expires_at: string;
  user: User;
}

export interface LoginIn {
  username: string;
  password: string;
}

export interface RegisterIn {
  username: string;
  password: string;
  display_name?: string | null;
  email?: string | null;
}

// ── 会话（app/schemas/conversation.py）──────────────────────────────────────

export interface Conversation {
  id: number;
  conversation_code: string;
  title: string | null;
  active_report_id: number | null;
  active_report_code: string | null;
  is_pinned: number;
  pinned_at: string | null;
  message_count: number;
  last_message_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ConversationDetail extends Conversation {
  /** 结构化查询条件（条件芯片的数据源） */
  slot: Record<string, unknown>;
}

export interface ConversationList {
  total: number;
  items: Conversation[];
}

export interface ConversationCreateOut extends Conversation {
  active_report_source: 'explicit' | 'last_conversation' | 'system_default';
}

export interface ConversationPatchOut extends Conversation {
  evicted_pinned_code: string | null;
}

export interface MessageOut {
  id: number;
  seq_no: number;
  role: 'user' | 'assistant';
  content: string | null;
  analysis_id: number | null;
  /** 助手消息对应的分析记录编号：刷新后据此回放表格 / 图表 / 执行信息 */
  record_code: string | null;
  created_at: string;
}

export interface MessageList {
  conversation_code: string;
  items: MessageOut[];
  limit: number;
  has_more: boolean;
  oldest_seq: number | null;
  newest_seq: number | null;
}

// ── 报表（app/schemas/report.py）────────────────────────────────────────────

export interface Report {
  id: number;
  report_code: string;
  filename: string;
  report_type: string;
  table_name: string;
  time_start: string | null;
  time_end: string | null;
  row_count: number | null;
  column_count: number | null;
  status: string;
  is_preset: number;
  is_active: number;
  created_at: string;
  table_exists: boolean;
  is_deletable: boolean;
}

export interface ReportList {
  total: number;
  items: Report[];
}

export interface QualitySummary {
  missing_rows: number | null;
  missing_ratio: number | null;
  time_axis_gap: number | null;
  duplicate_timestamps: number | null;
  voltage_out_of_range_minutes: number | null;
  checks: Record<string, boolean>;
}

export interface ReportDetail extends Report {
  caliber_version: string | null;
  quality_summary: QualitySummary;
}

export interface SchemaColumn {
  name: string;
  type: string;
  unit?: string;
  agg?: string;
  description?: string;
  derived?: boolean;
  origin_name?: string;
}

/** 上传解析后的列信息（「字段映射」与确认页展示） */
export interface UploadColumn {
  /** 安全列名（ASCII） */
  name: string;
  /** 原始列名（中文原名原样保留） */
  origin_name: string;
  type: string;
  unit: string;
  /** `sum` = 可求和；`none` = 不可求和（比率列 / 维度列 / 文本列） */
  agg: string;
  description: string;
  /** 系统自动生成的时间戳/维度列（源表没有该列） */
  derived: boolean;
}

export interface UploadPreview {
  columns: { name: string; type: string }[];
  rows: Record<string, unknown>[];
  returned_rows: number;
  preview_rows: number;
}

/** `POST /reports/upload` 响应（两步式第一步，状态为 `pending`） */
export interface UploadOut {
  report_code: string;
  filename: string;
  table_name: string;
  report_type: string;
  status: string;
  encoding: string;
  delimiter: string;
  row_count: number;
  column_count: number;
  raw_column_count: number;
  time_column: string;
  time_start: string | null;
  time_end: string | null;
  /** 命中 sha256 去重：本次未重复解析，直接复用已有报表 */
  is_duplicate: boolean;
  /** 原始列名 → 安全列名（仅列出发生改变的列） */
  column_mapping: Record<string, string>;
  columns: UploadColumn[];
  preview: UploadPreview;
  quality: QualityDetail;
  pending_expires_at: string;
}

/** `POST /reports/{code}/confirm` 响应（两步式第二步，幂等） */
export interface ConfirmOut {
  report_code: string;
  table_name: string;
  status: string;
  row_count: number;
  inserted_rows: number;
  indexes: string[];
  elapsed_ms: number;
  intermediate_removed: boolean;
  /** 重复确认（已是 ready）时为 true */
  already_confirmed: boolean;
}

export interface DeleteReportOut {
  report_code: string;
  filename: string;
  table_name: string | null;
  dropped_table: string | null;
  dropped_indexes: string[];
  affected_conversations: number;
  freed_pages: number;
  files_removed: boolean;
}

/** `GET /reports/{code}/preview`（默认 50 行，上限 200） */
export interface PreviewOut {
  report_code: string;
  table_name: string;
  columns: { name: string; type: string }[];
  rows: Record<string, unknown>[];
  total_rows: number | null;
  returned_rows: number;
  limit: number;
  truncated: boolean;
  order_by: string;
  /** 被主动过滤的列（自增主键等代理键） */
  excluded_columns: string[];
}

/**
 * 质量体检正文（`GET /reports/{code}/quality` 的 `quality` 字段）。
 *
 * 形状随数据集而变（预置报表含 A1–A5 口径校验，上传报表只有通用体检），
 * 因此 `dataset` 与 `checks` 都是**开放键**，页面按"键值表 + 勾选表"通用渲染。
 */
export interface QualityDetail {
  level?: string;
  note?: string;
  caliber_version?: string | null;
  checked_at?: string;
  dataset?: Record<string, unknown>;
  checks?: Record<string, boolean>;
}

export interface QualityOut {
  report_code: string;
  table_name: string;
  caliber_version: string | null;
  quality: QualityDetail;
}

export interface ReportSchema {
  report_code: string;
  table_name: string;
  caliber_version: string | null;
  structured: { columns?: SchemaColumn[]; [key: string]: unknown };
  /** 与注入给模型的说明**逐字一致**的完整文本 */
  text: string;
  text_source: string;
  est_tokens: number;
  truncated: boolean;
}

// ── 分析结果（SSE 载荷 + 记录快照）─────────────────────────────────────────

/** `table` 事件载荷（= `GET /analysis/records/{code}` 的 `result`） */
export interface TablePayload {
  columns: { name: string; type: string }[];
  rows: Record<string, unknown>[];
  row_count: number;
  truncated: boolean;
}

/** `chart` 事件载荷（`figure=null` + `fallback="table"` 表示不适合可视化，不是失败） */
export interface ChartPayload {
  figure: Record<string, unknown> | null;
  chart_type: string;
  fallback: string | null;
  reason: string | null;
  unit: string | null;
  x_axis?: string | null;
  /** 系列名列表（后端 `ChartOutcome.series: list[str]`，不是对象数组） */
  series: string[];
  r3_adjusted: boolean;
  r3_note: string | null;
  dropped_columns: string[];
  excluded_columns: string[];
  top_n: number | null;
  merged_count: number;
  downsampled_from: number | null;
  gap_count: number;
}

export interface NarrativePayload {
  text: string;
  key_numbers: Record<string, unknown>[];
  anomalies: string[];
}

export interface MetaPayload {
  elapsed_ms: number;
  retries: number;
  llm_tokens: number;
  model: string;
  llm_impl: string;
}

export interface ErrorPayload {
  code: string;
  message: string;
  /** 可能是字符串（`route=sql rule=...`）或对象（BizError.data），前端两者都要能接 */
  detail?: string | Record<string, unknown>;
  retries?: number;
  path_switched?: boolean;
}

export interface ClarifyOption {
  label: string;
  /** `null` 表示"自定义/不限"，前端弹输入框收集后再以 `slot_patch` 提交 */
  value: unknown;
}

export interface ClarifyPayload {
  question: string;
  options: ClarifyOption[];
  /** 缺失的字段名（**单个**，如 `metric`）——后端 `build_clarify_card` 的取值 */
  missing_slot: string;
  partial_slot: Record<string, unknown>;
}

export interface CodePayload {
  route: 'sql' | 'pandas';
  sql?: string;
  code?: string;
}

export interface DonePayload {
  /** 澄清轮为 `null`（不产生分析记录） */
  record_id: string | null;
}

export interface SlotPayload {
  [key: string]: unknown;
}

export interface StatusPayload {
  stage: 'parsing' | 'executing' | string;
}

// ── 分析记录（app/schemas/analysis_record.py）──────────────────────────────

export interface RecordItem {
  record_code: string;
  question: string;
  conversation_code: string | null;
  report_code: string | null;
  report_filename: string | null;
  route: string | null;
  status: string;
  error_code: string | null;
  elapsed_ms: number | null;
  llm_tokens: number | null;
  llm_model: string | null;
  created_at: string;
}

export interface RecordList {
  total: number;
  page: number;
  page_size: number;
  status_count: Record<string, number>;
  items: RecordItem[];
}

export interface LlmCall {
  stage: string;
  model_name: string;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  total_tokens: number | null;
  elapsed_ms: number | null;
  status: string;
}

export interface RecordDetail {
  record_code: string;
  question: string;
  conversation_code: string | null;
  report_code: string | null;
  report_filename: string | null;
  route: string | null;
  status: string;
  error_code: string | null;
  error_message: string | null;
  llm_model: string | null;
  elapsed_ms: number | null;
  prompt_version: string | null;
  schema_version: string | null;
  /** F2.4 自愈重试次数与是否换路（执行信息区块回放用） */
  retries: number;
  path_switched: boolean;
  created_at: string;
  slot: Record<string, unknown>;
  sql: string | null;
  code_text: string | null;
  result: TablePayload;
  narrative: string | null;
  key_numbers: Record<string, unknown>[];
  anomalies: string[];
  chart: Record<string, unknown> | null;
  llm_calls: LlmCall[];
  llm_tokens: number;
  can_replay: boolean;
  replayed_from: string | null;
  /** 重跑的落地说明（v1.19.12）：原记录属只读的评测批次会话时，重跑会另起新会话执行 */
  replay_note: string | null;
}

// ── 图表接口（app/schemas/chart.py）────────────────────────────────────────

export type ChartType = 'line' | 'bar' | 'area' | 'pie' | 'scatter' | 'table';

export interface RerenderIn {
  record_code: string;
  chart_type?: ChartType;
  granularity?: string;
  top_n?: number;
  sort?: 'natural' | 'value_desc' | 'value_asc';
}

// ── 系统（app/api/v1/endpoints/system.py）──────────────────────────────────

export interface HealthOut {
  status?: string;
  database?: Record<string, unknown>;
  llm?: {
    configured: boolean;
    /** `scripted` = 未配置 Key（离线脚本模型） */
    impl: string;
    provider?: string;
    protocol?: string;
    model_main?: string;
    model_light?: string;
    key_source?: string | null;
    base_url?: string;
  };
  [key: string]: unknown;
}

// ── 条件芯片（UIUX 6.5 与后端 slot 契约）──────────────────────────────────

export interface SlotFilter {
  field: string;
  op: string;
  value: unknown;
}

export interface Slot {
  time_range?: unknown;
  granularity?: string;
  metric?: string;
  dimension?: string;
  filters?: SlotFilter[];
  compare?: string;
  [key: string]: unknown;
}

// ── 评测（app/services/eval_service.py 的返回形状）──────────────────────────

/** 题库题目（`GET /eval/questions`） */
export interface EvalQuestion {
  question_code: string;
  question: string;
  /** stat / agg / compare / anomaly / structure / multi_turn */
  qtype: string;
  difficulty: string;
  report_table: string;
  is_trap: boolean;
  trap_rule: string | null;
  tags: string | null;
  gold_sql: string;
  key_numbers: { label?: string; value?: unknown; unit?: string; kind?: string }[];
}

export interface EvalQuestionList {
  total: number;
  page: number;
  page_size: number;
  items: EvalQuestion[];
}

/** 批次摘要（列表与详情共用的基础字段） */
export interface EvalRunSummary {
  run_code: string;
  name: string | null;
  model_name: string;
  temperature: number;
  prompt_version: string;
  schema_version: string;
  question_total: number;
  exec_correct_count: number | null;
  exec_correct_rate: number | null;
  narrative_hit_rate: number | null;
  avg_elapsed_ms: number | null;
  p95_elapsed_ms: number | null;
  first_token_avg_ms: number | null;
  retry_total: number | null;
  status: string;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  report_path: string | null;
}

export interface EvalRateGroup {
  total: number;
  correct: number;
  rate: number | null;
}

export interface EvalMetrics {
  attribution: Record<string, number>;
  question_total: number;
  exec_correct_count: number;
  exec_correct_rate: number | null;
  narrative_hit_rate: number | null;
  avg_elapsed_ms: number | null;
  p95_elapsed_ms: number | null;
  retry_total: number;
  by_type: Record<string, EvalRateGroup>;
  by_difficulty: Record<string, EvalRateGroup>;
  trap: EvalRateGroup & { rules: string[] };
  diff_types: Record<string, number>;
  errors: Record<string, number>;
  retry_distribution: Record<string, number>;
  /** 判分档命中分布（1 列位置 / 2 列名归一 / 3 单列数值集合） */
  tiers: Record<string, number>;
}

/** 逐题明细（`metrics` 与 `questions` 都来自批次结束时的 `result_json`） */
export interface EvalQuestionResult {
  question_code: string;
  question: string;
  qtype: string;
  difficulty: string;
  is_trap: boolean;
  trap_rule: string | null;
  gold_sql: string | null;
  rounds: number;
  status: string;
  route: string | null;
  generated: string;
  generated_sql_or_code: string | null;
  is_correct: boolean;
  matched_tier: number | null;
  diff_type: string | null;
  diff_detail: string | null;
  key_hit_rate: number | null;
  key_missed: { label?: string }[];
  elapsed_ms: number;
  retries: number;
  path_switched: boolean;
  error_code: string | null;
  analysis_ids: number[];
  narrative: string;
}

/** `GET /eval/runs/{run_code}`：批次详情（运行中时没有 `metrics`/`questions`） */
export interface EvalRunDetail extends EvalRunSummary {
  impl?: string;
  metrics?: EvalMetrics;
  questions?: EvalQuestionResult[];
}

export interface EvalRunCreated {
  run_code: string;
  status: string;
  impl: string;
  model_name: string;
  question_total: number;
}

export interface EvalRunList {
  total: number;
  page: number;
  page_size: number;
  items: EvalRunSummary[];
}

/** 评测执行模式（PRD F7：温度固定 0） */
export type EvalImpl = 'auto' | 'real' | 'selfcheck';
