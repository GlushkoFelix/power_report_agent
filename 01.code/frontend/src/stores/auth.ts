/**
 * 登录态（决策：token 存 localStorage + axios 401 统一拦截）。
 *
 * 401 的落地行为（UIUX 9.4「保留用户输入」）：清 token → 记住当前路径 → Toast 提示 →
 * 跳登录页；未发送的输入由调用方（输入区）在收到 `E_AUTH` 时写入草稿，登录后回填。
 */

import { create } from 'zustand';

import { ApiError, setUnauthorizedHandler } from '@/api/client';
import { authApi } from '@/api/auth';
import { clearToken, getToken, setToken } from '@/api/token';
import { useChatStore } from '@/stores/chat';
import { toast } from '@/utils/toast';
import type { LoginIn, RegisterIn, User } from '@/api/types';

interface AuthState {
  user: User | null;
  /** `loading` = 正在用本地 token 换用户信息 */
  status: 'loading' | 'anonymous' | 'authenticated';
  /** 401 后要跳回的路径（登录成功后由 Login 页消费） */
  redirectTo: string | null;
  /**
   * 本次"未登录"是否由**用户主动退出**引起。
   *
   * 语义区别（登录页据此决定要不要认 `?next=`）：
   * - **登录态失效**（401）→ 保留 `next`，登录后回到原页（不丢用户正在做的事）；
   * - **主动退出** → 忽略 `next`，直接回首页（用户实测反馈"退出再登录又回到退出前那一页"）。
   */
  loggedOutByUser: boolean;
  init: () => Promise<void>;
  login: (payload: LoginIn) => Promise<void>;
  register: (payload: RegisterIn) => Promise<void>;
  logout: () => Promise<void>;
  /** 编辑资料成功后同步本页展示（后端已落库，这里只更新内存态） */
  setUser: (user: User) => void;
  /** 由 axios 拦截器触发：只清状态，提示与跳转交给路由守卫 */
  handleUnauthorized: () => void;
}

/** 供外部读取的默认导出类型（个人中心等页面用到 `User` 结构时走 api/types） */
export type { AuthState };

export const useAuthStore = create<AuthState>((set) => ({
  user: null,
  status: 'loading',
  redirectTo: null,
  loggedOutByUser: false,

  init: async () => {
    if (!getToken()) {
      set({ status: 'anonymous', user: null });
      return;
    }
    try {
      const user = await authApi.me();
      set({ user, status: 'authenticated' });
    } catch (error) {
      if (error instanceof ApiError && error.httpStatus === 401) {
        clearToken();
      }
      set({ user: null, status: 'anonymous' });
    }
  },

  login: async (payload) => {
    const result = await authApi.login(payload);
    setToken(result.token);
    // 上一位用户（或上一次登录会话）的会话/消息/条件必须清干净，并复位"自动选会话"标记
    useChatStore.getState().reset();
    set({ user: result.user, status: 'authenticated', redirectTo: null, loggedOutByUser: false });
  },

  register: async (payload) => {
    const result = await authApi.register(payload);
    setToken(result.token);
    useChatStore.getState().reset();
    set({ user: result.user, status: 'authenticated', redirectTo: null, loggedOutByUser: false });
  },

  setUser: (user) => {
    set({ user });
  },

  logout: async () => {
    try {
      await authApi.logout();
    } catch {
      // 退出失败不阻塞前端登出（token 本地清除即可，后端审计失败不影响用户）
    }
    // 先清业务状态（中断流 + 清空会话/结果），再清 token：
    // 否则重新登录时会看到上一位用户的数据，且"自动打开最近会话"不会再触发
    useChatStore.getState().reset();
    clearToken();
    set({ user: null, status: 'anonymous', redirectTo: null, loggedOutByUser: true });
  },

  handleUnauthorized: () => {
    set({
      user: null,
      status: 'anonymous',
      redirectTo: typeof window === 'undefined' ? null : window.location.pathname,
      // 登录态失效（非用户主动退出）→ 保留"回原页"的意图
      loggedOutByUser: false,
    });
    toast.error('登录状态已失效，请重新登录');
  },
}));

// 拦截器 → store 的反向注册（避免 api 层 import store 造成循环依赖）
setUnauthorizedHandler(() => {
  useAuthStore.getState().handleUnauthorized();
});
