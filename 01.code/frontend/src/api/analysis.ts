/** 分析接口（SSE 对话/澄清 + 记录列表/详情/重跑/删除）。 */

import { api } from './client';
import { streamSSE, type SSEHandlers } from './sse';
import type { RecordDetail, RecordList, Slot } from './types';

export interface ChatIn {
  conversation_code: string;
  report_code?: string | null;
  message: string;
  /** 条件芯片编辑后的回填（UIUX 6.3） */
  slot_patch?: Slot | null;
}

export interface ClarifyIn {
  conversation_code: string;
  question: string;
  slot_patch: Slot;
}

export const analysisApi = {
  /** 对话分析（SSE）：提问走这条 */
  chat: (payload: ChatIn, handlers: SSEHandlers, signal?: AbortSignal) =>
    streamSSE('/analysis/chat', payload, handlers, signal),

  /** 澄清回填后就地继续执行（SSE） */
  clarify: (payload: ClarifyIn, handlers: SSEHandlers, signal?: AbortSignal) =>
    streamSSE('/analysis/clarify', payload, handlers, signal),

  records: (params: {
    conversation_code?: string;
    report_code?: string;
    status?: string;
    q?: string;
    date_from?: string;
    date_to?: string;
    page?: number;
    page_size?: number;
  }) => api.get<RecordList>('/analysis/records', { params }),

  record: (recordCode: string) => api.get<RecordDetail>(`/analysis/records/${recordCode}`),

  replay: (recordCode: string) =>
    api.post<RecordDetail>(`/analysis/records/${recordCode}/replay`),

  removeRecord: (recordCode: string) =>
    api.delete<{ record_code: string; deleted_messages: number; deleted_llm_calls: number }>(
      `/analysis/records/${recordCode}`,
    ),
};
