/**
 * 报表与活跃报表（UIUX 5.1 区域 1：顶部选择器；5.3：列表与高亮来源）。
 *
 * 高亮来源规则（UIUX 5.3）：**会话内取 `conversations.active_report_id`，会话外回落系统默认
 * 活跃报表（`is_active = 1`）**——因此本 store 只负责"列表 + 系统默认"，
 * 会话级活跃报表由 chatStore 持有（切换会话时同步）。
 */

import { create } from 'zustand';

import { reportApi } from '@/api/reports';
import type { Report } from '@/api/types';

interface ReportState {
  items: Report[];
  loading: boolean;
  loaded: boolean;
  load: (force?: boolean) => Promise<void>;
  /** 系统默认活跃报表（会话外回落用） */
  systemActive: () => Report | null;
  byCode: (reportCode: string | null | undefined) => Report | null;
}

export const useReportStore = create<ReportState>((set, get) => ({
  items: [],
  loading: false,
  loaded: false,

  load: async (force = false) => {
    if (get().loading) return;
    if (get().loaded && !force) return;
    set({ loading: true });
    try {
      const result = await reportApi.list();
      set({ items: result.items, loaded: true });
    } finally {
      set({ loading: false });
    }
  },

  systemActive: () => get().items.find((item) => item.is_active === 1) ?? null,

  byCode: (reportCode) =>
    reportCode ? (get().items.find((item) => item.report_code === reportCode) ?? null) : null,
}));
