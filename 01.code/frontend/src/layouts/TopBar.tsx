/**
 * 顶部栏（UIUX 5.1 区域 1 / 10.2 响应式）。
 *
 * 组成：**可点击 Logo（回首页）** + 副标题（≥1366px 显示）· **全局导航** · 当前报表选择器。
 *
 * 导航是 v1.14.1 补的（用户实测反馈"进了报表管理/评测中心/个人中心后回不到首页"）：
 * 四个页面互为独立路由，此前只有用户下拉里两个入口（且没有评测中心），既进不去也回不来。
 * 报表选择器背后的规则见 `reportStore` 注释：**会话内以会话的活跃报表为准**，
 * 会话外回落系统默认活跃报表。
 *
 * 用户菜单（个人中心 / 主题 / 退出登录）**已移到侧栏底部**的账号卡片里（照设计稿的
 * "底部账号 + 向上弹出"形态）；顶栏原本另有主题开关，与菜单里那项重复，已按用户反馈删除
 * （主题数据仍由 `themeStore` 统一管理并持久化到 `zdxb.theme`）。
 */

import { useMemo, useState, type ReactNode } from 'react';
import { Select, Tooltip } from 'antd';
import {
  BarChartOutlined,
  FileTextOutlined,
  MessageOutlined,
  ThunderboltFilled,
  UserOutlined,
} from '@ant-design/icons';
import { useLocation, useNavigate } from 'react-router-dom';

import { useChatStore } from '@/stores/chat';
import { useReportStore } from '@/stores/reports';

const REPORT_TYPE_LABELS: Record<string, string> = {
  minute: '分钟表',
  daily: '日报',
  monthly: '月报',
  hourly: '小时表',
  structure: '结构表',
  uploaded: '上传',
};

function reportLabel(reportCode: string, reportType?: string): string {
  const type = reportType ? (REPORT_TYPE_LABELS[reportType] ?? reportType) : '';
  return type ? `${reportCode}（${type}）` : reportCode;
}

/** 全局导航（顺序 = 建议的使用顺序：先对话分析，再管数据、跑评测、看账号） */
const NAV_ITEMS: { path: string; label: string; icon: ReactNode }[] = [
  { path: '/', label: '对话工作台', icon: <MessageOutlined /> },
  { path: '/reports', label: '报表管理', icon: <FileTextOutlined /> },
  { path: '/eval', label: '评测中心', icon: <BarChartOutlined /> },
  { path: '/profile', label: '个人中心', icon: <UserOutlined /> },
];

export default function TopBar() {
  const navigate = useNavigate();
  const location = useLocation();

  const reports = useReportStore((state) => state.items);
  const activeDetail = useChatStore((state) => state.activeDetail);
  const preferredReportCode = useChatStore((state) => state.preferredReportCode);
  const setActiveReport = useChatStore((state) => state.setActiveReport);

  const currentCode = useMemo(() => {
    if (activeDetail?.active_report_code) return activeDetail.active_report_code;
    return preferredReportCode ?? reports.find((item) => item.is_active === 1)?.report_code ?? null;
  }, [activeDetail, preferredReportCode, reports]);

  const options = reports.map((report) => ({
    value: report.report_code,
    label: reportLabel(report.report_code, report.report_type),
  }));

  const current = reports.find((item) => item.report_code === currentCode);

  /** 下拉是否展开：展开时禁用外层 Tooltip（它会被挤在下拉两侧，显示成黑色小方块，用户实测反馈） */
  const [selectOpen, setSelectOpen] = useState(false);

  const reportByCode = useMemo(() => {
    const map = new Map<string, (typeof reports)[number]>();
    reports.forEach((item) => map.set(item.report_code, item));
    return map;
  }, [reports]);

  /** 报表的"人话"信息：表名 / 文件名 / 规模 / 数据范围 / 归属（预置 or 上传） */
  const reportFacts = (report: (typeof reports)[number]): string[] =>
    [
      `表名：${report.table_name}`,
      `文件：${report.filename}`,
      report.row_count != null
        ? `规模：${report.row_count.toLocaleString()} 行 × ${report.column_count ?? '—'} 列`
        : null,
      report.time_start && report.time_end
        ? `范围：${report.time_start.slice(0, 10)} ~ ${report.time_end.slice(0, 10)}`
        : null,
      report.is_preset === 1 ? '系统预置（全用户共享）' : '我上传的（仅自己可见）',
    ].filter((line): line is string => Boolean(line));

  /**
   * 下拉每项：只显示「编码（类型）」，信息全部放**悬浮气泡**里。
   *
   * 曾一度在右侧加过一列次要文案（文件名 / 表名 · 规模），但用户反馈它和悬浮气泡里的
   * 内容重复、看着互相打架 → 已移除，保持下拉干净。
   */
  const renderOption = (code: string) => {
    const report = reportByCode.get(code);
    if (!report) return <span>{code}</span>;
    return (
      <Tooltip
        placement="right"
        title={
          <div className="space-y-0.5">
            {reportFacts(report).map((line) => (
              <div key={line}>{line}</div>
            ))}
          </div>
        }
      >
        <span>{reportLabel(report.report_code, report.report_type)}</span>
      </Tooltip>
    );
  };

  return (
    <header
      // 窄屏收紧内边距与间距：手机实测 390px 下 px-6 + gap-4 会把品牌名挤成两行
      className="flex h-14 shrink-0 items-center gap-2 border-b px-3 sm:gap-4 sm:px-6"
      style={{ background: 'var(--c-bg-card)', borderColor: 'var(--c-line)' }}
    >
      {/* Logo 可点 → 回对话工作台（首页），这是最直觉的"回家"入口 */}
      <div
        className="flex shrink-0 cursor-pointer items-center gap-2 whitespace-nowrap"
        onClick={() => navigate('/')}
        role="link"
        tabIndex={0}
        onKeyDown={(event) => {
          if (event.key === 'Enter') navigate('/');
        }}
      >
        <ThunderboltFilled style={{ color: 'var(--c-primary)', fontSize: 18 }} />
        <span className="text-h2 font-semibold">智电析报</span>
        <span className="hidden text-text-sub 2xl:inline">
          基于LLM的能源电力报表智能分析Agent
        </span>
      </div>

      {/* 全局导航：四个页面互为独立路由，必须能互相跳。
          四个入口**不挂 Tooltip**（用户反馈：宽屏下文字标签已显示，悬浮气泡多余且遮挡内容）。 */}
      <nav className="flex items-center gap-1">
        {NAV_ITEMS.map((item) => {
          const active =
            item.path === '/'
              ? location.pathname === '/'
              : location.pathname.startsWith(item.path);
          return (
            <button
              key={item.path}
              type="button"
              onClick={() => navigate(item.path)}
              aria-current={active ? 'page' : undefined}
              className="flex h-8 items-center gap-1 rounded-input px-2 text-body transition-colors lg:px-3"
              style={{
                background: active ? 'var(--c-primary-light)' : 'transparent',
                color: active ? 'var(--c-primary)' : 'var(--c-text-main)',
                fontWeight: active ? 600 : 400,
                border: 'none',
                cursor: 'pointer',
              }}
            >
              {item.icon}
              <span className="hidden lg:inline">{item.label}</span>
            </button>
          );
        })}
      </nav>

      <div className="ml-auto flex items-center gap-4">
        <div className="flex items-center gap-2">
          {/* 窄屏隐藏文字标签（选择器自身有 placeholder），给报表选择器留出宽度 */}
          <span className="hidden text-text-sub lg:inline">当前报表：</span>
          <Tooltip
            title={
              current
                ? `${current.filename} · ${current.table_name} · ${current.row_count?.toLocaleString() ?? '—'} 行`
                : '选择分析使用的报表'
            }
            placement="bottom"
            // 下拉展开时强制关闭：否则这个气泡会被挤在下拉左右两侧（黑色小方块，用户实测反馈）
            open={selectOpen ? false : undefined}
          >
            <Select
              size="small"
              style={{ minWidth: 200 }}
              value={currentCode ?? undefined}
              options={options}
              onChange={(value) => void setActiveReport(value)}
              placeholder="选择报表"
              notFoundContent="暂无可用报表"
              open={selectOpen}
              onOpenChange={setSelectOpen}
              optionRender={(option) => renderOption(String(option.value))}
            />
          </Tooltip>
        </div>
      </div>
    </header>
  );
}
