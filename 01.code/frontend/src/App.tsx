/**
 * 应用根：主题（ConfigProvider）→ Toast 出口 → 路由。
 *
 * AntD 主题与 CSS 变量**同源**：主色/圆角/字体在这里给 AntD，
 * 页面级颜色走 `styles/index.css` 的变量，两者随 `themeStore` 一起切换。
 */

import { useEffect } from 'react';
import { App as AntdApp, ConfigProvider, Spin, theme as antdTheme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom';

import MainLayout from '@/layouts/MainLayout';
import LoginPage from '@/pages/Login';
import WorkbenchPage from '@/pages/Workbench';
import ReportsPage from '@/pages/Reports';
import EvalPage from '@/pages/Eval';
import ProfilePage from '@/pages/Profile';
import { useAuthStore } from '@/stores/auth';
import { useThemeStore } from '@/stores/theme';
import { setToastInstance } from '@/utils/toast';

/** 把 `App.useApp()` 的 message 实例注册给 store / axios 使用（见 utils/toast.ts） */
function ToastBridge() {
  const { message } = AntdApp.useApp();
  useEffect(() => {
    setToastInstance(message);
    return () => setToastInstance(null);
  }, [message]);
  return null;
}

function RequireAuth({ children }: { children: React.ReactNode }) {
  const status = useAuthStore((state) => state.status);
  const location = useLocation();

  if (status === 'loading') {
    return (
      <div className="flex h-screen flex-col items-center justify-center gap-3">
        <Spin />
        <span className="text-text-sub">正在校验登录状态…</span>
      </div>
    );
  }
  if (status === 'anonymous') {
    const next = `${location.pathname}${location.search}`;
    return <Navigate to={`/login?next=${encodeURIComponent(next)}`} replace />;
  }
  return <>{children}</>;
}

export default function App() {
  const mode = useThemeStore((state) => state.mode);
  const init = useAuthStore((state) => state.init);

  useEffect(() => {
    void init();
  }, [init]);

  const isDark = mode === 'dark';

  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: isDark ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
        token: {
          colorPrimary: isDark ? '#4C74E6' : '#1D4ED8',
          colorInfo: isDark ? '#4C74E6' : '#1D4ED8',
          colorError: isDark ? '#FF5A5A' : '#E03131',
          colorWarning: isDark ? '#FF7A33' : '#E8590C',
          colorSuccess: isDark ? '#2FD4A0' : '#0CA678',
          colorTextBase: isDark ? '#E5E9F0' : '#1F2937',
          colorBgBase: isDark ? '#141821' : '#FFFFFF',
          colorBgContainer: isDark ? '#1C2230' : '#FFFFFF',
          colorBorder: isDark ? '#313A4D' : '#D9DEE7',
          colorBorderSecondary: isDark ? '#2A3242' : '#EEF1F5',
          borderRadius: 8,
          fontSize: 12,
          controlHeight: 32,
          fontFamily: '"Microsoft YaHei", "PingFang SC", system-ui, sans-serif',
        },
        components: {
          Table: { headerBg: isDark ? '#232A3A' : '#F1F3F5', cellPaddingBlockSM: 6 },
          Card: { paddingLG: 20 },
          Layout: { headerHeight: 56, headerPadding: '0 24px' },
        },
      }}
    >
      <AntdApp>
        <ToastBridge />
        <BrowserRouter>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route
              path="/"
              element={
                <RequireAuth>
                  <MainLayout />
                </RequireAuth>
              }
            >
              <Route index element={<WorkbenchPage />} />
              <Route path="reports" element={<ReportsPage />} />
              <Route path="eval" element={<EvalPage />} />
              <Route path="profile" element={<ProfilePage />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Routes>
        </BrowserRouter>
      </AntdApp>
    </ConfigProvider>
  );
}
