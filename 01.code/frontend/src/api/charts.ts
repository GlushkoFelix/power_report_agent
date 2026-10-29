/** 图表接口（UIUX 7.1：换类型 / 换粒度 / topN 均不调 LLM）。 */

import { api } from './client';
import type { ChartPayload, RerenderIn } from './types';

export const chartApi = {
  /**
   * 重绘：成功后后端**回写该记录的 `chart_json`**（详情页与右栏一致）。
   * 显式类型画不出来 → 400 `E_CHART_UNSUPPORTED`（`data.supported` 列出可用类型）；
   * 不可重聚合 → 400 `E_GRANULARITY_UNAVAILABLE`。
   */
  rerender: (payload: RerenderIn) => api.post<ChartPayload>('/charts/rerender', payload),

  /** 后端 PNG 导出入口（kaleido 未装时返回 501，前端静默走本地 `Plotly.toImage`） */
  pngUrl: (recordCode: string, params?: { width?: number; height?: number; scale?: number }) => {
    const query = new URLSearchParams();
    if (params?.width) query.set('width', String(params.width));
    if (params?.height) query.set('height', String(params.height));
    if (params?.scale) query.set('scale', String(params.scale));
    const suffix = query.toString();
    return `/api/v1/analysis/records/${recordCode}/chart.png${suffix ? `?${suffix}` : ''}`;
  },
};

/** 后端 PNG 接口是否已可用（kaleido 未装 → 501，前端不弹错、走本地导出） */
export async function probePngExport(recordCode: string): Promise<boolean> {
  try {
    const response = await fetch(chartApi.pngUrl(recordCode), { method: 'HEAD' });
    return response.ok;
  } catch {
    return false;
  }
}
