/**
 * 主题（UIUX 11：默认跟随系统，切换后立即生效并记忆；导出图片固定亮色）。
 *
 * 落地方式：把 `data-theme="dark"` 写到 `<html>`，CSS 变量随之切换；
 * AntD 侧由 `ThemeProvider` 依据同一个 store 选用 `darkAlgorithm`。
 */

import { create } from 'zustand';

import type { ThemeMode } from '@/theme/tokens';

const STORAGE_KEY = 'zdxb.theme';
type Preference = ThemeMode | 'system';

interface ThemeState {
  /** 用户偏好（可跟随系统） */
  preference: Preference;
  /** 实际生效的主题 */
  mode: ThemeMode;
  setPreference: (preference: Preference) => void;
  /** 在亮/暗之间切换（写入 localStorage） */
  toggle: () => void;
}

function systemMode(): ThemeMode {
  if (typeof window === 'undefined' || !window.matchMedia) return 'light';
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function readPreference(): Preference {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === 'light' || saved === 'dark' || saved === 'system') return saved;
  } catch {
    /* ignore */
  }
  return 'system';
}

function applyMode(mode: ThemeMode): void {
  const root = document.documentElement;
  root.dataset.theme = mode;
  root.style.colorScheme = mode;
}

const initialPreference = readPreference();
const initialMode: ThemeMode = initialPreference === 'system' ? systemMode() : initialPreference;
applyMode(initialMode);

export const useThemeStore = create<ThemeState>((set, get) => ({
  preference: initialPreference,
  mode: initialMode,

  setPreference: (preference) => {
    const mode = preference === 'system' ? systemMode() : preference;
    applyMode(mode);
    try {
      localStorage.setItem(STORAGE_KEY, preference);
    } catch {
      /* ignore */
    }
    set({ preference, mode });
  },

  toggle: () => {
    const next: ThemeMode = get().mode === 'dark' ? 'light' : 'dark';
    get().setPreference(next);
  },
}));

/** 跟随系统时监听系统主题变化（只在偏好为 system 时生效） */
if (typeof window !== 'undefined' && window.matchMedia) {
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    const { preference, setPreference } = useThemeStore.getState();
    if (preference === 'system') setPreference('system');
  });
}
