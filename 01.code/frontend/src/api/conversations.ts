/** 会话接口（PRD 9.1 会话模块 / F6.3 会话管理）。 */

import { api } from './client';
import type {
  ConversationCreateOut,
  ConversationDetail,
  ConversationList,
  ConversationPatchOut,
  MessageList,
} from './types';

export const conversationApi = {
  list: (params?: { q?: string; date_from?: string; date_to?: string }) =>
    api.get<ConversationList>('/conversations', { params }),

  create: (payload?: { title?: string | null; active_report_code?: string | null }) =>
    api.post<ConversationCreateOut>('/conversations', payload ?? {}),

  detail: (conversationCode: string) =>
    api.get<ConversationDetail>(`/conversations/${conversationCode}`),

  patch: (
    conversationCode: string,
    payload: { title?: string | null; active_report_code?: string | null; is_pinned?: boolean },
  ) => api.patch<ConversationPatchOut>(`/conversations/${conversationCode}`, payload),

  remove: (conversationCode: string) =>
    api.delete<{ conversation_code: string; deleted_messages?: number; deleted_analyses?: number }>(
      `/conversations/${conversationCode}`,
    ),

  messages: (conversationCode: string, params?: { limit?: number; before_seq?: number }) =>
    api.get<MessageList>(`/conversations/${conversationCode}/messages`, { params }),
};
