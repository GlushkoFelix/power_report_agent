/** 系统接口（健康检查用：页面据此提示"当前为离线脚本模型"）。 */

import { api } from './client';
import type { HealthOut } from './types';

export const systemApi = {
  health: () => api.get<HealthOut>('/system/health'),
};
