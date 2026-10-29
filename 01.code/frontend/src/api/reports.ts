/** 报表接口（PRD 9.1 报表模块；本批只用到列表 / 详情 / 字典 / 切换活跃报表）。 */

import { api, http } from './client';
import type {
  ConfirmOut,
  DeleteReportOut,
  PreviewOut,
  QualityOut,
  ReportDetail,
  ReportList,
  ReportSchema,
  UploadOut,
} from './types';

export const reportApi = {
  list: () => api.get<ReportList>('/reports'),

  detail: (reportCode: string) => api.get<ReportDetail>(`/reports/${reportCode}`),

  /** Schema 数据字典：`text` 与注入给模型的说明**逐字一致**（UIUX 5.3 区域 4） */
  schema: (reportCode: string) => api.get<ReportSchema>(`/reports/${reportCode}/schema`),

  /** 数据质量报告（预处理入库时算好，接口只读库） */
  quality: (reportCode: string) => api.get<QualityOut>(`/reports/${reportCode}/quality`),

  /** 数据预览（真表结构，≤200 行；`excluded_columns` 回显被过滤的代理键） */
  preview: (reportCode: string, limit = 50) =>
    api.get<PreviewOut>(`/reports/${reportCode}/preview`, { params: { limit } }),

  /**
   * 上传第一步：解析 + 落 pending（**multipart**，字段名 `file`）。
   *
   * 大文件解析是长等待（207 万行约 30s），因此：
   * - `onProgress` 回传上传进度百分比（浏览器侧）；
   * - 调用方自己跑"已耗时"计时器，>5s 追加说明（UIUX 8.2）。
   */
  upload: (file: File, onProgress?: (percent: number) => void) => {
    const form = new FormData();
    form.append('file', file);
    return http
      .request<UploadOut>({
        url: '/reports/upload',
        method: 'POST',
        data: form,
        // 大文件解析：给足 10 分钟（客户端默认 3 分钟不够）
        timeout: 600_000,
        onUploadProgress: (event) => {
          if (!onProgress || !event.total) return;
          onProgress(Math.round((event.loaded / event.total) * 100));
        },
      })
      .then((response) => response.data);
  },

  /** 上传第二步：确认入库（幂等，重复确认返回 `already_confirmed=true`） */
  confirm: (reportCode: string) => api.post<ConfirmOut>(`/reports/${reportCode}/confirm`),

  /** 删除报表（预置报表返回 403；未确认的 pending 也可直接删=取消上传） */
  remove: (reportCode: string) => api.delete<DeleteReportOut>(`/reports/${reportCode}`),

  /** 切换会话的活跃报表（报表视角语法糖，权限与会话校验由后端保证） */
  activate: (reportCode: string, conversationCode: string) =>
    api.post<{ conversation_code: string; active_report_code: string }>(
      `/reports/${reportCode}/activate`,
      { conversation_code: conversationCode },
    ),
};
