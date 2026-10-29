/**
 * Toast 出口（UIUX 9.1：Toast 用于鉴权失效 / 限流 / 网络异常 / 成功提示）。
 *
 * store 与 axios 拦截器都需要弹提示，但它们不在 React 组件树内、拿不到 `App.useApp()` 的
 * 上下文实例，因此由根组件把实例注册进来，其余模块统一走这里（避免用 antd 的静态方法：
 * 静态方法消费不到 ConfigProvider 的动态主题，暗色下会保持亮色样式）。
 */

import type { MessageInstance } from 'antd/es/message/interface';

let instance: MessageInstance | null = null;

export function setToastInstance(next: MessageInstance | null): void {
  instance = next;
}

function withRequestId(content: string, requestId?: string): string {
  return requestId && requestId !== '-' ? `${content}（request_id: ${requestId}）` : content;
}

export const toast = {
  success: (content: string) => instance?.success(content),
  info: (content: string) => instance?.info(content),
  warning: (content: string) => instance?.warning(content),
  error: (content: string, requestId?: string) => instance?.error(withRequestId(content, requestId)),
};
