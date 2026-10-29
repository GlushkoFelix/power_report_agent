/**
 * 错误码 → 界面呈现映射（**逐行对齐 UIUX 9.2 的映射表**）。
 *
 * 这里只做"错误码 → 怎么显示"的翻译，不拼接具体定位信息
 * （`size_mb` / `line_no` 等由后端 `message` 与 `data.hint` 直接给出，UIUX 9.5「修复建议两行呈现」）。
 */

export type ErrorSeverity = 'inline' | 'card' | 'toast' | 'refuse' | 'clarify';

export interface ErrorPresentation {
  /** 呈现层次（UIUX 9.2 主色列） */
  severity: ErrorSeverity;
  /** 是否给「重试」按钮 */
  retryable: boolean;
  /** 标题（错误块首行；缺省用后端 message） */
  title?: string;
  /** 主色：红 / 橙 / 灰 / 主色 */
  tone: 'red' | 'orange' | 'gray' | 'primary';
}

const RED: ErrorPresentation = { severity: 'card', retryable: true, tone: 'red' };

export const ERROR_PRESENTATION: Record<string, ErrorPresentation> = {
  // 上传类（表单内联）
  E_NO_TIME_COLUMN: { severity: 'inline', retryable: false, tone: 'red' },
  E_ENCODING: { severity: 'inline', retryable: false, tone: 'red' },
  E_FILE_TOO_LARGE: { severity: 'inline', retryable: false, tone: 'red' },
  E_FILE_TYPE: { severity: 'inline', retryable: false, tone: 'red' },
  E_HEADER_INVALID: { severity: 'inline', retryable: false, tone: 'red' },
  E_NO_NUMERIC_COLUMN: { severity: 'inline', retryable: false, tone: 'red' },
  E_PARSE: { severity: 'inline', retryable: false, tone: 'red' },
  E_INGEST_FAILED: { severity: 'card', retryable: true, tone: 'red' },
  // 分析执行类（卡片错误块）
  E_SQL_FORBIDDEN: RED,
  E_SQL_EXEC: RED,
  E_SANDBOX_TIMEOUT: RED,
  E_CODE_FORBIDDEN: RED,
  E_CODE_EXEC: RED,
  E_OUT_OF_SCOPE: { severity: 'refuse', retryable: false, tone: 'gray', title: '超出可回答范围' },
  // 鉴权与权限
  E_AUTH: { severity: 'toast', retryable: false, tone: 'red' },
  E_FORBIDDEN: { severity: 'toast', retryable: false, tone: 'red' },
  E_CREDENTIALS: { severity: 'inline', retryable: false, tone: 'red', title: '用户名或密码错误' },
  E_AUTH_LOCKED: { severity: 'inline', retryable: false, tone: 'red' },
  E_USERNAME_TAKEN: { severity: 'inline', retryable: false, tone: 'red' },
  E_ACCOUNT_DISABLED: { severity: 'toast', retryable: false, tone: 'red' },
  E_VALIDATION: { severity: 'inline', retryable: false, tone: 'red' },
  // 其他
  E_LLM: { severity: 'toast', retryable: true, tone: 'orange' },
  E_NOT_FOUND: { severity: 'toast', retryable: false, tone: 'orange' },
  E_INTERNAL: { severity: 'toast', retryable: false, tone: 'red' },
};

export function presentationOf(code: string | null | undefined): ErrorPresentation {
  if (!code) return RED;
  return ERROR_PRESENTATION[code] ?? RED;
}

/**
 * 错误块的"已尝试路径"文案（UIUX 8.1：`error` 事件的 `retries` / `path_switched`）。
 */
export function attemptedPathsText(retries = 0, pathSwitched = false): string {
  const parts: string[] = [];
  if (retries > 0) parts.push(`已自动重写 ${retries} 次`);
  if (pathSwitched) parts.push('并已切换到 pandas 路径');
  return parts.length ? parts.join('，') : '未触发重试';
}
