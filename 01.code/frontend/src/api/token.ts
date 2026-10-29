/** JWT 本地存储（决策：localStorage，刷新不丢；401 时由拦截器统一清理）。 */

const TOKEN_KEY = 'zdxb.token';
const USER_KEY = 'zdxb.user';

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string): void {
  try {
    localStorage.setItem(TOKEN_KEY, token);
  } catch {
    /* 隐私模式等场景忽略：内存态仍可用（本会话内有效） */
  }
}

export function clearToken(): void {
  try {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
  } catch {
    /* 同上 */
  }
}

/** 401 后需要跳回登录页时，前端把"未发送的输入"暂存于此（UIUX 9.4「保留用户输入」） */
const DRAFT_KEY = 'zdxb.draft';

export function saveDraft(text: string, next?: string): void {
  try {
    localStorage.setItem(DRAFT_KEY, JSON.stringify({ text, next: next ?? null, at: Date.now() }));
  } catch {
    /* ignore */
  }
}

export function takeDraft(): { text: string; next: string | null } | null {
  const draft = peekDraft();
  try {
    localStorage.removeItem(DRAFT_KEY);
  } catch {
    /* ignore */
  }
  return draft;
}

/** 只读草稿（登录页用它提示"输入已保留"，**不消费**——消费留给输入区） */
export function peekDraft(): { text: string; next: string | null } | null {
  try {
    const raw = localStorage.getItem(DRAFT_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { text?: string; next?: string | null };
    return { text: parsed.text ?? '', next: parsed.next ?? null };
  } catch {
    return null;
  }
}
