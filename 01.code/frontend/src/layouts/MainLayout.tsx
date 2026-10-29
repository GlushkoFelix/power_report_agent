/**
 * 登录后布局（UIUX 3：顶部栏 + 内容区）。
 *
 * 左栏（会话列表）只在对话工作台出现（它承载"会话列表"语义），
 * 因此放在 `Workbench` 页面内，而不是这里。
 */

import { useEffect } from 'react';
import { Outlet } from 'react-router-dom';

import TopBar from './TopBar';
import { useChatStore } from '@/stores/chat';
import { useReportStore } from '@/stores/reports';

export default function MainLayout() {
  const loadReports = useReportStore((state) => state.load);
  const loadConversations = useChatStore((state) => state.loadConversations);
  const conversations = useChatStore((state) => state.conversations);
  const ensureInitialSelection = useChatStore((state) => state.ensureInitialSelection);

  useEffect(() => {
    void loadReports();
    void loadConversations();
  }, [loadReports, loadConversations]);

  // 默认进入最近一次会话——**只做一次**（列表已按"置顶组在前 + 更新时间倒序"排序）。
  // 注意不能用"activeCode 为空就选第一条"：删除当前会话后 activeCode 也是空，
  // 那样会把用户强行跳进另一个会话（用户实测反馈过）。
  useEffect(() => {
    void ensureInitialSelection();
  }, [conversations, ensureInitialSelection]);

  return (
    // `overflow-hidden` + 内部 <main> 滚动：**滚动只发生在外壳里**，文档层不滚
    // （Tooltip 浮层因此不会撑出文档滚动条，见 `styles/index.css` 的说明）
    <div
      className="flex h-screen flex-col overflow-hidden"
      style={{ background: 'var(--c-bg-page)' }}
    >
      <TopBar />
      <main className="zd-scroll min-h-0 flex-1">
        <Outlet />
      </main>
    </div>
  );
}
