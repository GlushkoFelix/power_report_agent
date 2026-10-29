/**
 * 统一 HTTP 客户端（UIUX 13：API 层统一 axios 实例）。
 *
 * 三件事：
 * 1. **Token 注入**：每个请求自动带 `Authorization: Bearer <token>`；
 * 2. **包络拆解**：成功时把 `{code:0, message, data, request_id}` 直接解成 `data`，
 *    业务代码不必每次 `.data.data`；
 * 3. **错误归一**：失败时统一抛 `ApiError`（含 `error_code` / `data.hint` / `request_id`），
 *    401 交给注册进来的处理器（清 token + 跳登录 + 保留输入）。
 */

import axios, { type AxiosRequestConfig } from 'axios';

import type { Envelope, ErrorData } from './types';
import { clearToken, getToken } from './token';

export class ApiError extends Error {
  /** 业务错误码（如 `E_SQL_EXEC`）；网络层错误为 `E_NETWORK` */
  readonly code: string;
  /** HTTP 状态码（网络层错误为 0） */
  readonly httpStatus: number;
  readonly data: ErrorData;
  readonly requestId: string;

  constructor(opts: {
    code: string;
    httpStatus: number;
    message: string;
    data?: ErrorData;
    requestId?: string;
  }) {
    super(opts.message);
    this.name = 'ApiError';
    this.code = opts.code;
    this.httpStatus = opts.httpStatus;
    this.data = opts.data ?? { error_code: opts.code };
    this.requestId = opts.requestId ?? '-';
  }

  /** 后端给出的可执行下一步（UIUX 9.5：第二行文案） */
  get hint(): string | undefined {
    return this.data?.hint;
  }
}

interface UnauthorizedHandler {
  (): void;
}

let unauthorizedHandler: UnauthorizedHandler | null = null;

/** 注册 401 处理器（由 authStore 在初始化时注入，避免 api ← store 的循环依赖） */
export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  unauthorizedHandler = handler;
}

export const http = axios.create({
  baseURL: '/api/v1',
  // 分析链路最慢可能几十秒（沙箱/重试），给足超时；SSE 走 fetch，不受此限制
  timeout: 180_000,
  // **不设默认 Content-Type**：axios 会按 data 类型自动选（对象 → application/json，
  // FormData → multipart/form-data 且由浏览器补 boundary）。写死 json 会让上传接口报错。
});

http.interceptors.request.use((config) => {
  const token = getToken();
  if (token) {
    config.headers.set('Authorization', `Bearer ${token}`);
  }
  return config;
});

http.interceptors.response.use(
  (response) => {
    const body = response.data as Envelope<unknown> | undefined;
    // 非包络响应（例如 Markdown 报告下载）原样返回，交给调用方处理
    if (!body || typeof body !== 'object' || !('code' in body)) {
      return response;
    }
    if (body.code === 0) {
      // 拆包络：把 data 塞回 response.data，调用方拿到的就是业务数据
      response.data = body.data;
      return response;
    }
    throw new ApiError({
      code: (body.data as ErrorData)?.error_code ?? 'E_INTERNAL',
      httpStatus: body.code,
      message: body.message || '请求失败',
      data: body.data as ErrorData,
      requestId: body.request_id,
    });
  },
  (error) => {
    const response = error?.response;
    const body = response?.data as Envelope<ErrorData> | undefined;

    // 业务错误（后端统一包络）
    if (body && typeof body === 'object' && 'code' in body) {
      const apiError = new ApiError({
        code: body.data?.error_code ?? 'E_INTERNAL',
        httpStatus: body.code,
        message: body.message || '请求失败',
        data: body.data,
        requestId: body.request_id,
      });
      if (apiError.httpStatus === 401) {
        clearToken();
        unauthorizedHandler?.();
      }
      return Promise.reject(apiError);
    }

    // 框架级 422（Pydantic 校验失败，本项目已尽量避免）与网络层错误
    const status = response?.status ?? 0;
    if (status === 401) {
      clearToken();
      unauthorizedHandler?.();
    }
    const detail =
      status === 0
        ? '无法连接后端服务，请确认服务已启动（默认 127.0.0.1:8000）'
        : `请求失败（HTTP ${status}）`;
    return Promise.reject(
      new ApiError({
        code: status === 0 ? 'E_NETWORK' : 'E_INTERNAL',
        httpStatus: status,
        message: detail,
        data: { error_code: status === 0 ? 'E_NETWORK' : 'E_INTERNAL' },
        requestId: response?.headers?.['x-request-id'] ?? '-',
      }),
    );
  },
);

/** 发起请求并返回**已拆包络**的业务数据 */
export async function request<T>(config: AxiosRequestConfig): Promise<T> {
  const response = await http.request<T>(config);
  return response.data as T;
}

export const api = {
  get: <T>(url: string, config?: AxiosRequestConfig) => request<T>({ ...config, url, method: 'GET' }),
  post: <T>(url: string, data?: unknown, config?: AxiosRequestConfig) =>
    request<T>({ ...config, url, method: 'POST', data }),
  patch: <T>(url: string, data?: unknown, config?: AxiosRequestConfig) =>
    request<T>({ ...config, url, method: 'PATCH', data }),
  delete: <T>(url: string, config?: AxiosRequestConfig) =>
    request<T>({ ...config, url, method: 'DELETE' }),
};
