/**
 * 认证接口（PRD F6.1 / F6.2 / F6.4）。注册成功即自动登录，与登录返回同一结构。
 *
 * 个人中心四件事（v1.19.12 补齐，此前是"按钮置灰 + 后端暂无该接口"的交付缺口）：
 * 编辑资料 / 修改密码 / 清空历史记录 / 删除账号。
 */

import { api } from './client';
import type { LoginIn, RegisterIn, TokenOut, User } from './types';

/** 编辑资料：**只提交要改的字段**（未提交的字段后端保持原值；显式传 null 表示清空） */
export interface ProfileUpdateIn {
  display_name?: string | null;
  email?: string | null;
}

export interface PasswordChangeIn {
  old_password: string;
  new_password: string;
}

/** 清空口径：`records` = 只清分析记录（对话文本保留）；`all` = 连会话与对话历史一起清 */
export type ClearScope = 'records' | 'all';

export interface ClearRecordsOut {
  scope: ClearScope;
  deleted_records: number;
  deleted_conversations: number;
  deleted_messages: number;
  deleted_llm_calls: number;
}

export interface DeleteAccountIn {
  password: string;
  confirm_username: string;
}

export interface DeleteAccountOut {
  username: string;
  deleted_reports: number;
  deleted_conversations: number;
  deleted_records: number;
  deleted_eval_runs: number;
  /** 按合规保留的安全日志条数（归属置空） */
  kept_audit_logs: number;
}

export const authApi = {
  login: (payload: LoginIn) => api.post<TokenOut>('/auth/login', payload),
  register: (payload: RegisterIn) => api.post<TokenOut>('/auth/register', payload),
  logout: () => api.post<Record<string, never>>('/auth/logout'),
  me: () => api.get<User>('/auth/me'),

  /** 编辑资料：只改显示名 / 邮箱（用户名是账号标识，不可改） */
  updateProfile: (payload: ProfileUpdateIn) => api.patch<User>('/auth/me', payload),

  /** 修改密码：需提供当前密码；改密后已签发的 token 仍有效（JWT 无服务端会话） */
  changePassword: (payload: PasswordChangeIn) => api.post<null>('/auth/password', payload),

  /** 清空历史记录（危险操作，调用前必须让用户二次确认） */
  clearRecords: (scope: ClearScope) => api.post<ClearRecordsOut>('/auth/clear-records', { scope }),

  /** 删除账号（不可撤销）：密码 + 用户名双确认；安全日志按合规保留 */
  deleteAccount: (payload: DeleteAccountIn) =>
    api.delete<DeleteAccountOut>('/auth/me', { data: payload }),
};
