/**
 * 左栏 · 会话列表（UIUX 5.1 区域 2）。
 *
 * 数据与落地行为（v1.8.0 契约）：
 * - 列表取 `GET /conversations`：**置顶组在前**，其余按更新时间倒序；置顶最多 5 条，
 *   超出由后端自动挤出最早的置顶并在响应 `evicted_pinned_code` 回显 → 这里只弹 Toast 提示；
 * - 「新建会话」**只开草稿、不落库**（`startNewConversation`）：侧栏出现记录要等用户真正
 *   发出第一条消息（那时 `send` 才建会话）——此前"点了就建"，侧栏会堆出一串空会话；
 * - 删除走 `DELETE /conversations/{code}`，二次确认文案说明"将同时删除消息与分析记录，
 *   安全日志保留"（PRD F6.3）；
 * - 悬停（或选中）时在行右侧显示「⋮」下拉：重命名 / 置顶 / 删除，重命名就地编辑。
 *   用下拉而非并排三个图标按钮的原因：长标题会与并排按钮互相遮挡（用户实测反馈）；
 *   标题行因此预留右侧空间，并在超长时用 Tooltip 显示全名。
 * - 顶部「多选」图标进入多选模式：顶部条切换为「选择对话 + ×」（×/Esc 退出并清空选择），
 *   行首出现复选框、整行点击=选中（不再切换会话），行尾「⋮」让位；
 *   列表下方出现批量操作条（全选 / 置顶 / 删除）。
 *   注：不设"批量取消置顶"——它与顶部「×」退出多选在语义上重叠（用户实测反馈后移除）。
 * - 顶部第一行是「会话」标题 + 搜索 / 多选 / 收起三个图标，第二行是整宽胶囊「新建会话」；
 *   点放大镜后该行变为搜索框（**服务端按标题模糊搜索** `GET /conversations?q=`，防抖 300ms），
 *   退出搜索或离开页面即撤销筛选。
 * - 侧栏底部是**账号卡片**（头像 + 账号名 + ⋯，向上弹出账号菜单）：
 *   原顶栏右上角的用户菜单、以及侧栏底部原「报表管理 / 评测中心 / 个人中心」三个按钮
 *   都已并入这一份菜单（设计稿形态）；收起态只留头像按钮，菜单同一份。
 */

import { useEffect, useState } from 'react';
import { App as AntdApp, Button, Checkbox, Dropdown, Empty, Input, Spin, Tooltip, Typography } from 'antd';
import {
  BarChartOutlined,
  CloseOutlined,
  DeleteOutlined,
  LogoutOutlined,
  MoonOutlined,
  SunOutlined,
  EditOutlined,
  FileTextOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  MoreOutlined,
  PlusOutlined,
  PushpinFilled,
  PushpinOutlined,
  SearchOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';

import { useAuthStore } from '@/stores/auth';
import { useChatStore } from '@/stores/chat';
import { useThemeStore } from '@/stores/theme';
import { formatDateTime } from '@/utils/format';

/**
 * 侧栏底部的页面入口（**已并入底部账号卡片的下拉菜单**，侧栏不再单列一排按钮：
 * 它们与顶栏全局导航重复，且把侧栏底部的垂直空间吃掉了）。
 */
const NAV_MENU_ITEMS = [
  { key: '/reports', label: '报表管理', icon: <FileTextOutlined /> },
  { key: '/eval', label: '评测中心', icon: <BarChartOutlined /> },
  { key: '/profile', label: '个人中心', icon: <UserOutlined /> },
];

/**
 * 多选图标：照设计稿重绘（两行「圆环 + 横线」）。
 * @ant-design/icons 里没有等价字形（`UnorderedListOutlined` 是圆点、`OrderedListOutlined` 是序号），
 * 所以内联 SVG 以保证与设计稿一致，并随按钮 currentColor 变色。
 */
function MultiSelectIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      width="1em"
      height="1em"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      aria-hidden="true"
    >
      <circle cx="5" cy="7.6" r="2.6" />
      <line x1="11.6" y1="7.6" x2="19.6" y2="7.6" />
      <circle cx="5" cy="16.4" r="2.6" />
      <line x1="11.6" y1="16.4" x2="19.6" y2="16.4" />
    </svg>
  );
}

export default function ConversationList({
  collapsed,
  onToggle,
}: {
  collapsed: boolean;
  onToggle: () => void;
}) {
  const navigate = useNavigate();
  const conversations = useChatStore((state) => state.conversations);
  const loading = useChatStore((state) => state.conversationsLoading);
  const activeCode = useChatStore((state) => state.activeCode);
  const selectConversation = useChatStore((state) => state.selectConversation);
  const startNewConversation = useChatStore((state) => state.startNewConversation);
  const renameConversation = useChatStore((state) => state.renameConversation);
  const togglePin = useChatStore((state) => state.togglePin);
  const deleteConversation = useChatStore((state) => state.deleteConversation);
  const streaming = useChatStore((state) => state.streaming);

  const [editingCode, setEditingCode] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState('');
  /** 删除确认用 `App.useApp().modal`（静态 `Modal.confirm` 消费不到动态主题，暗色下会变亮色） */
  const { modal } = AntdApp.useApp();

  // ── 多选模式 ──────────────────────────────────────────────────────────────
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const batchPin = useChatStore((state) => state.batchPin);
  const batchDelete = useChatStore((state) => state.batchDelete);

  const exitSelectMode = () => {
    setSelectMode(false);
    setSelected([]);
  };

  // ── 会话搜索（服务端按标题模糊筛选 `GET /conversations?q=`，非前端过滤）──────
  const [searchOpen, setSearchOpen] = useState(false);
  const [keyword, setKeyword] = useState('');
  const setConversationKeyword = useChatStore((state) => state.setConversationKeyword);
  const trimmedKeyword = keyword.trim();

  // 输入防抖 300ms：别每敲一个字打一次接口
  useEffect(() => {
    if (!searchOpen) return undefined;
    const timer = window.setTimeout(() => {
      void setConversationKeyword(trimmedKeyword);
    }, 300);
    return () => window.clearTimeout(timer);
  }, [trimmedKeyword, searchOpen, setConversationKeyword]);

  // 上次离开本页时若还挂着筛选，本次进入先撤掉并重取列表。
  // 放在"进入时"而不是"离开时"：① 离开时可能在退出登录，用失效 token 发请求会弹假的"登录已过期"；
  // ② 列表只在 MainLayout 挂载时拉一次，不重取的话回来会看到"被筛过的列表 + 空搜索框"。
  useEffect(() => {
    if (useChatStore.getState().conversationKeyword) {
      void setConversationKeyword('');
    }
  }, [setConversationKeyword]);

  const closeSearch = () => {
    setSearchOpen(false);
    setKeyword('');
    void setConversationKeyword('');
  };

  const handleCreate = () => {
    // 搜索中新建 → 先撤掉筛选：否则新会话标题多半不匹配关键词，侧栏里"看不见"
    if (searchOpen || trimmedKeyword) closeSearch();
    // 只进草稿态（不落库）：侧栏出现记录要等用户真正发出第一条消息
    startNewConversation();
  };
  const toggleSelected = (code: string) =>
    setSelected((prev) => (prev.includes(code) ? prev.filter((c) => c !== code) : [...prev, code]));

  // Esc 退出多选（键盘操作不必回去点 ×）
  useEffect(() => {
    if (!selectMode) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setSelectMode(false);
        setSelected([]);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [selectMode]);

  // 列表变化后清掉已不存在的选中项（批量删除 / 他处删除后不会残留幽灵选中）
  useEffect(() => {
    setSelected((prev) => {
      const alive = new Set(conversations.map((item) => item.conversation_code));
      const next = prev.filter((code) => alive.has(code));
      return next.length === prev.length ? prev : next;
    });
  }, [conversations]);

  const allSelected = conversations.length > 0 && selected.length === conversations.length;

  const confirmBatchDelete = () =>
    modal.confirm({
      title: `删除 ${selected.length} 个会话`,
      content: '将同时删除这些会话的消息与分析记录，安全日志保留。',
      okText: '删除',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: async () => {
        await batchDelete(selected);
        exitSelectMode();
      },
    });

  const pinned = conversations.filter((item) => item.is_pinned === 1);
  const rest = conversations.filter((item) => item.is_pinned !== 1);

  // ── 账号菜单（原顶栏用户菜单并入此处；侧栏在底部，故**向上弹出**）─────────────
  const user = useAuthStore((state) => state.user);
  const logout = useAuthStore((state) => state.logout);
  const { mode, toggle: toggleTheme } = useThemeStore();
  const accountName = user?.display_name || user?.username || '未登录';
  const accountTitle = [user?.username, user?.email].filter(Boolean).join(' · ') || accountName;

  const accountMenu = {
    items: [
      // 页面入口（原侧栏底部那排按钮，已并入此处）
      ...NAV_MENU_ITEMS,
      {
        key: 'theme',
        icon: mode === 'dark' ? <SunOutlined /> : <MoonOutlined />,
        label: mode === 'dark' ? '切换到亮色' : '切换到暗色',
      },
      { type: 'divider' as const },
      { key: 'logout', icon: <LogoutOutlined />, label: '退出登录', danger: true },
    ],
    onClick: ({ key }: { key: string }) => {
      if (key === 'theme') {
        toggleTheme();
        return;
      }
      if (key === 'logout') {
        // 主动退出 ≠ 登录态失效：先回登录页且**不带 next**，
        // 否则重新登录会被带回退出前那一页（401 失效仍走 RequireAuth 的 next 回跳）
        navigate('/login', { replace: true });
        void logout();
        return;
      }
      navigate(key); // 其余菜单项的 key 即目标路由
    },
  };

  if (collapsed) {
    return (
      <div
        className="flex w-[48px] shrink-0 flex-col items-center gap-3 border-r py-3"
        style={{ background: 'var(--c-bg-card)', borderColor: 'var(--c-line)' }}
      >
        <Tooltip title="展开会话列表" placement="right">
          <Button type="text" icon={<MenuUnfoldOutlined />} onClick={onToggle} />
        </Tooltip>
        <Tooltip title="新建会话" placement="right">
          <Button
            type="primary"
            shape="circle"
            size="small"
            icon={<PlusOutlined />}
            disabled={streaming}
            onClick={startNewConversation}
          />
        </Tooltip>
        {/* 收起态也保留账号入口（与展开态同一份菜单）；**点击**才弹出（用户反馈：不要悬浮触发） */}
        <div className="mt-auto">
          <Dropdown menu={accountMenu} placement="topRight" trigger={['click']}>
            <Tooltip title={accountName} placement="right">
              <Button type="text" shape="circle" aria-label="用户菜单" icon={<UserOutlined />} />
            </Tooltip>
          </Dropdown>
        </div>
      </div>
    );
  }

  const renderItem = (item: (typeof conversations)[number]) => {
    const active = item.conversation_code === activeCode;
    const isEditing = editingCode === item.conversation_code;
    const checked = selected.includes(item.conversation_code);
    return (
      <div
        key={item.conversation_code}
        className="group relative cursor-pointer rounded-btn px-2 py-2"
        style={{
          background: selectMode && checked ? 'var(--c-primary-light)' : active ? 'var(--c-primary-light)' : 'transparent',
        }}
        onClick={() => {
          if (selectMode) {
            toggleSelected(item.conversation_code);
            return;
          }
          if (!isEditing) void selectConversation(item.conversation_code);
        }}
      >
        {isEditing ? (
          <Input
            size="small"
            autoFocus
            value={editingTitle}
            maxLength={60}
            onChange={(event) => setEditingTitle(event.target.value)}
            onPressEnter={() => {
              void renameConversation(item.conversation_code, editingTitle.trim());
              setEditingCode(null);
            }}
            onBlur={() => setEditingCode(null)}
          />
        ) : (
          <>
            {/* 标题行右侧预留 24px（pr-6）给「⋮」，长标题在按钮左侧省略，不再互相遮挡；
                多选模式下「⋮」让位给行首复选框，因此不再需要预留 */}
            <div className={`flex items-center gap-2 ${selectMode ? '' : 'pr-6'}`}>
              {selectMode ? (
                <Checkbox
                  checked={checked}
                  onChange={() => toggleSelected(item.conversation_code)}
                  onClick={(event) => event.stopPropagation()}
                />
              ) : item.is_pinned === 1 ? (
                <PushpinFilled style={{ fontSize: 10, color: 'var(--c-primary)' }} />
              ) : null}
              <Typography.Text
                ellipsis={{ tooltip: item.title || '未命名会话' }}
                style={{ color: active ? 'var(--c-primary)' : 'var(--c-text-main)', fontWeight: active ? 600 : 400 }}
              >
                {item.title || '未命名会话'}
              </Typography.Text>
            </div>
            {/* 只留时间 + 报表：消息条数已按用户反馈去掉（列表里信息够用，越简越干净） */}
            <div className="mt-0.5 flex items-center gap-2 text-[11px] text-text-sub">
              <span>{formatDateTime(item.last_message_at ?? item.updated_at)}</span>
              {item.active_report_code ? <span>· {item.active_report_code}</span> : null}
            </div>

            {/* 悬停（或当前选中）时出现在行右侧的「⋮」：重命名 / 置顶 / 删除（多选模式下让位） */}
            {selectMode ? null : (
            <Dropdown
              trigger={['click']}
              placement="bottomRight"
              menu={{
                items: [
                  { key: 'rename', icon: <EditOutlined />, label: '重命名' },
                  {
                    key: 'pin',
                    icon: item.is_pinned === 1 ? <PushpinFilled /> : <PushpinOutlined />,
                    label: item.is_pinned === 1 ? '取消置顶' : '置顶',
                  },
                  { type: 'divider' as const },
                  { key: 'delete', icon: <DeleteOutlined />, label: '删除会话', danger: true },
                ],
                onClick: ({ key, domEvent }) => {
                  // 菜单点击不要透传给整行（否则会顺带切换会话）
                  domEvent.stopPropagation();
                  if (key === 'rename') {
                    setEditingCode(item.conversation_code);
                    setEditingTitle(item.title ?? '');
                    return;
                  }
                  if (key === 'pin') {
                    void togglePin(item.conversation_code, item.is_pinned !== 1);
                    return;
                  }
                  if (key === 'delete') {
                    modal.confirm({
                      title: '删除会话',
                      content: '将同时删除该会话的消息与分析记录，安全日志保留。',
                      okText: '删除',
                      cancelText: '取消',
                      okButtonProps: { danger: true },
                      onOk: () => deleteConversation(item.conversation_code),
                    });
                  }
                },
              }}
            >
              <Button
                type="text"
                size="small"
                aria-label="会话操作"
                icon={<MoreOutlined />}
                className={`absolute right-1 top-1 ${active ? 'flex' : 'hidden group-hover:flex'}`}
                onClick={(event) => event.stopPropagation()}
              />
            </Dropdown>
            )}
          </>
        )}
      </div>
    );
  };

  return (
    <aside
      className="flex w-[240px] shrink-0 flex-col border-r"
      style={{ background: 'var(--c-bg-card)', borderColor: 'var(--c-line)' }}
    >
      {selectMode ? (
        <div className="flex items-center justify-between p-3">
          <span className="text-[15px] font-semibold" style={{ color: 'var(--c-text-main)' }}>
            选择对话
          </span>
          <Tooltip title="退出多选">
            <Button
              type="text"
              shape="circle"
              aria-label="退出多选"
              icon={<CloseOutlined style={{ fontSize: 14 }} />}
              onClick={exitSelectMode}
              style={{ border: '1px solid var(--c-line)' }}
            />
          </Tooltip>
        </div>
      ) : searchOpen ? (
        /* 搜索态：整行换成输入框 + 退出（照设计稿"点放大镜→行内变搜索框"） */
        <div className="flex items-center gap-2 px-3 py-3">
          <Input
            autoFocus
            allowClear
            size="small"
            aria-label="搜索会话输入框"
            prefix={<SearchOutlined style={{ color: 'var(--c-text-sub)' }} />}
            placeholder="搜索会话名称"
            value={keyword}
            onChange={(event) => setKeyword(event.target.value)}
          />
          <Tooltip title="退出搜索">
            <Button
              type="text"
              size="small"
              aria-label="退出搜索"
              icon={<CloseOutlined />}
              onClick={closeSearch}
            />
          </Tooltip>
        </div>
      ) : (
        <>
          {/* 第一行：左标题 + 右侧图标组（搜索 / 多选 / 收起），照设计稿排版 */}
          <div className="flex items-center gap-1 px-3 pb-1 pt-3">
            <span className="mr-auto text-[15px] font-semibold" style={{ color: 'var(--c-text-main)' }}>
              会话
            </span>
            <Tooltip title="搜索会话">
              <Button
                type="text"
                aria-label="搜索会话"
                icon={<SearchOutlined />}
                onClick={() => setSearchOpen(true)}
              />
            </Tooltip>
            <Tooltip title="多选">
              <Button
                type="text"
                aria-label="多选"
                icon={<MultiSelectIcon />}
                onClick={() => setSelectMode(true)}
              />
            </Tooltip>
            <Tooltip title="收起会话列表">
              <Button type="text" icon={<MenuFoldOutlined />} onClick={onToggle} />
            </Tooltip>
          </div>
          {/* 第二行：整宽胶囊按钮（照设计稿） */}
          <div className="px-3 pb-2">
            <Button
              type="primary"
              block
              icon={<PlusOutlined />}
              disabled={streaming}
              className="!h-10 !rounded-full"
              onClick={handleCreate}
            >
              新建会话
            </Button>
          </div>
        </>
      )}

      <div className="zd-scroll min-h-0 flex-1 px-2 pb-2">
        {/* 只在**首次加载**（列表还是空的）显示加载态：否则每次发消息/删除后的重取
            都会把已有列表整块换成 Spin → 侧栏闪烁（用户观感为"闪一下全没了"） */}
        {loading && conversations.length === 0 ? (
          <div className="flex justify-center py-8">
            <Spin size="small" />
          </div>
        ) : conversations.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={trimmedKeyword ? `没有匹配「${trimmedKeyword}」的会话` : '还没有会话'}
            className="!mt-8"
          />
        ) : (
          <>
            {trimmedKeyword ? (
              <div className="px-2 py-1 text-[11px] text-text-sub">
                搜索结果 {conversations.length} 条
              </div>
            ) : null}
            {pinned.length ? (
              <>
                <div className="px-2 py-1 text-[11px] text-text-sub">置顶（最多 5 条）</div>
                <div className="space-y-1">{pinned.map(renderItem)}</div>
                <div className="px-2 pb-1 pt-3 text-[11px] text-text-sub">会话历史</div>
              </>
            ) : null}
            <div className="space-y-1">{rest.map(renderItem)}</div>
          </>
        )}
      </div>

      {selectMode ? (
        <div className="border-t px-3 py-2" style={{ borderColor: 'var(--c-line-soft)' }}>
          <div className="mb-2 flex items-center justify-between">
            <Checkbox
              checked={allSelected}
              indeterminate={!allSelected && selected.length > 0}
              disabled={!conversations.length}
              aria-label="全选"
              onChange={(event) =>
                setSelected(
                  event.target.checked ? conversations.map((item) => item.conversation_code) : [],
                )
              }
            >
              <span className="text-[12px]">全选</span>
            </Checkbox>
            <span className="text-[12px] text-text-sub">已选 {selected.length} 项</span>
          </div>
          <div className="flex items-center gap-1">
            <Button
              size="small"
              block
              icon={<PushpinOutlined />}
              disabled={!selected.length || streaming}
              onClick={() => void batchPin(selected, true).then(exitSelectMode)}
            >
              置顶
            </Button>
            <Button
              size="small"
              block
              danger
              icon={<DeleteOutlined />}
              disabled={!selected.length || streaming}
              onClick={confirmBatchDelete}
            >
              删除
            </Button>
          </div>
          {streaming ? (
            <div className="mt-1 text-[11px] text-text-sub">分析进行中，暂不可批量操作</div>
          ) : null}
        </div>
      ) : null}

      {/* 账号卡片（设计稿形态：底部整块账号 + 向上弹出菜单）
          原顶栏用户菜单 + 侧栏底部"报表管理/评测中心/个人中心"三个按钮**都已并入此菜单** */}
      <div className="border-t px-2 py-2" style={{ borderColor: 'var(--c-line-soft)' }}>
        <Dropdown menu={accountMenu} placement="topRight" trigger={['click']}>
          <button
            type="button"
            aria-label="用户菜单"
            title={accountTitle}
            className="flex w-full cursor-pointer items-center gap-2 rounded-input px-2 py-2"
            style={{ background: 'var(--c-bg-weak)', border: 'none' }}
          >
            <span
              className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full"
              style={{ background: 'var(--c-primary-light)', color: 'var(--c-primary)' }}
            >
              <UserOutlined style={{ fontSize: 13 }} />
            </span>
            <span className="min-w-0 flex-1 truncate text-left text-body" style={{ color: 'var(--c-text-main)' }}>
              {accountName}
            </span>
            <MoreOutlined style={{ color: 'var(--c-text-sub)' }} />
          </button>
        </Dropdown>
      </div>
    </aside>
  );
}
