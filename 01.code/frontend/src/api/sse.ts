/**
 * SSE 封装（UIUX 13：`fetchEventSource`）。
 *
 * 为什么不用原生 `EventSource`：它只支持 GET、不能自定义请求头——
 * 而 `POST /analysis/chat` 需要 **Bearer token + JSON 请求体**。
 *
 * 三条约定（对齐后端 `app/api/v1/endpoints/analysis.py`）：
 * 1. **不做自动重连**：出错即抛出，交给页面渲染错误块（避免"报错了界面还在转"）；
 * 2. 每个事件 `event:` 是事件名、`data:` 是 JSON 字符串；
 * 3. `AbortController` 支持用户中途「停止」（UIUX 8.3 的取消）。
 */

import { fetchEventSource, type EventSourceMessage } from '@microsoft/fetch-event-source';

import { ApiError } from './client';
import { getToken } from './token';
import type { Envelope, ErrorData } from './types';

export interface SSEHandlers {
  onEvent: (event: string, data: unknown) => void;
  onClose?: () => void;
}

function parseEnvelopeError(raw: string, status: number): ApiError {
  try {
    const body = JSON.parse(raw) as Envelope<ErrorData>;
    return new ApiError({
      code: body?.data?.error_code ?? 'E_INTERNAL',
      httpStatus: body?.code ?? status,
      message: body?.message ?? `请求失败（HTTP ${status}）`,
      data: body?.data,
      requestId: body?.request_id,
    });
  } catch {
    return new ApiError({
      code: 'E_INTERNAL',
      httpStatus: status,
      message: `分析请求失败（HTTP ${status}）`,
      data: { error_code: 'E_INTERNAL' },
    });
  }
}

/**
 * 建立一条 SSE 连接（Promise 在流结束或被中止时 settle）。
 *
 * @param path  相对 `/api/v1` 的路径，如 `/analysis/chat`
 * @param body  请求体（JSON）
 * @param handlers 事件回调
 * @param signal 中止信号（用户点「停止」）
 */
export async function streamSSE(
  path: string,
  body: unknown,
  handlers: SSEHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const token = getToken();
  let failure: ApiError | null = null;

  await fetchEventSource(`/api/v1${path}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(body),
    signal,
    // 页面切到后台时保持流（否则分析会被 SDK 主动挂断）
    openWhenHidden: true,
    async onopen(response) {
      if (response.ok) return;
      const text = await response.text();
      failure = parseEnvelopeError(text, response.status);
      throw failure;
    },
    onmessage(message: EventSourceMessage) {
      if (!message.event || message.event === 'message') return;
      let payload: unknown = message.data;
      try {
        payload = message.data ? JSON.parse(message.data) : null;
      } catch {
        // 非 JSON 视为纯文本（当前后端不会出现，保底不丢事件）
      }
      handlers.onEvent(message.event, payload);
    },
    onclose() {
      handlers.onClose?.();
    },
    onerror(error) {
      // 抛出让 SDK 停止重试：分析失败由页面以错误块呈现，不静默重连
      throw failure ?? error;
    },
  });
}
