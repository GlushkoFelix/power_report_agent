/**
 * 评测接口（PRD 9.1 评测组 / F5）。
 *
 * 三条与页面强相关的契约（v1.12.0 实测）：
 * 1. **批次是异步的**：`POST /eval/runs` 立即返回 `run_code`（`status=pending`），
 *    页面按 2–3s 轮询 `GET /eval/runs/{run_code}` 直到 `finished/failed`；
 *    **运行期没有逐题进度**（`metrics`/`questions` 要等批次结束才落库）→ 前端如实显示"运行中"；
 * 2. `impl=real` 且未配置 Key → **400**（不静默回落到脚本模型），页面把 `hint` 内联展示；
 * 3. Markdown 报告走 `GET /eval/runs/{run_code}/report`，需要 Bearer 头，
 *    因此用 axios 取 blob 再触发下载（不能直接 `window.open`）。
 */

import { api, http } from './client';
import type {
  EvalImpl,
  EvalQuestionList,
  EvalRunCreated,
  EvalRunDetail,
  EvalRunList,
} from './types';

export interface CreateEvalRunIn {
  impl?: EvalImpl;
  name?: string | null;
  question_codes?: string[] | null;
  /** 抽样题量（不传则全量 120 题） */
  limit?: number | null;
  include_narrative?: boolean;
}

export const evalApi = {
  /** 题库（全局资产，不做用户隔离） */
  questions: (params?: {
    qtype?: string;
    difficulty?: string;
    is_trap?: boolean;
    page?: number;
    page_size?: number;
  }) => api.get<EvalQuestionList>('/eval/questions', { params }),

  createRun: (payload: CreateEvalRunIn) => api.post<EvalRunCreated>('/eval/runs', payload),

  runs: (params?: { page?: number; page_size?: number }) =>
    api.get<EvalRunList>('/eval/runs', { params }),

  /** 批次详情：运行中时只有基础字段 + `question_total`（没有 metrics/questions） */
  run: (runCode: string, includeQuestions = true) =>
    api.get<EvalRunDetail>(`/eval/runs/${runCode}`, {
      params: { include_questions: includeQuestions },
    }),

  /**
   * 下载 **PDF** 报告（v1.19.12 新增，主推）。
   *
   * 后端把同一份 Markdown 报告渲染成 PDF（`reportlab`，A4 横向、CID 中文字体），
   * 浏览器 / WPS / Word 都能直接打开并打印 —— 用户诉求："md 还要工具才能识别排版"。
   */
  downloadReportPdf: async (runCode: string): Promise<void> => {
    const response = await http.request<Blob>({
      url: `/eval/runs/${runCode}/report.pdf`,
      method: 'GET',
      responseType: 'blob',
    });
    downloadBlob(response.data, `${runCode}.pdf`);
  },

  /** 下载 Markdown 报告（批次未完成时 400、报告缺失 404）；保留用于二次处理/归档 */
  downloadReport: async (runCode: string): Promise<void> => {
    const response = await http.request<Blob>({
      url: `/eval/runs/${runCode}/report`,
      method: 'GET',
      responseType: 'blob',
    });
    downloadBlob(response.data, `${runCode}.md`);
  },
};

/** 触发浏览器下载（带 Bearer 的请求拿不到直链，只能用 blob） */
function downloadBlob(data: Blob, filename: string): void {
  const url = URL.createObjectURL(data);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

/** 题型 / 难度 / 归因的中文标签（与后端题库、报告保持一致） */
export const EVAL_QTYPE_LABELS: Record<string, string> = {
  stat: '统计问数',
  agg: '分组聚合',
  compare: '对比分析',
  anomaly: '异常识别',
  structure: '结构拆分',
  multi_turn: '多轮追问',
};

export const EVAL_DIFFICULTY_LABELS: Record<string, string> = {
  easy: '简单',
  medium: '中等',
  hard: '困难',
};

export const EVAL_ATTRIBUTION_LABELS: Record<string, string> = {
  澄清未执行: '澄清未执行',
  模型调用失败: '模型调用失败',
  口径理解错误: '口径理解错误',
  'SQL 语法与函数': 'SQL 语法与函数',
  slot继承错误: 'slot 继承错误',
  'slot 继承错误': 'slot 继承错误',
  执行失败未重试成功: '执行失败未重试成功',
  结果口径偏差: '结果口径偏差',
};

export const EVAL_STATUS_LABELS: Record<string, string> = {
  pending: '排队中',
  running: '运行中',
  finished: '已完成',
  failed: '失败',
};
