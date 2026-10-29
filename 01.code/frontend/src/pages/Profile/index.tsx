/**
 * 个人中心页（UIUX 5.5）。
 *
 * **本页四件事全部闭环（v1.19.12）**：账号信息（可编辑 / 改密）、我的数据统计、
 * 我的分析记录（状态页签 / 搜索 / 时间筛选 / 重跑 / 删除）、数据与隐私危险区
 * （清空历史记录 / 删除账号）。此前后两者是"按钮置灰 + 后端暂无该接口"的交付缺口
 * （PRD F6.4），现已补齐后端接口与二次确认弹窗（见 `AccountDialogs.tsx`）。
 *
 * **重跑的两个去处**：普通会话的记录 → 写回原会话；**评测批次会话（`c_eval_*`，只读留痕）**
 * 的记录 → 后端改写到新会话并在 `replay_note` 里说明，前端把这句话原样提示给用户
 * （避免"重跑污染评测批次留痕"）。
 *
 * **不在本页放模型 / 密钥相关内容**（决策，见 PRD 6.5）：模型 API Key 是**部署级**配置
 * （服务端 `.env`），本页既不提供填写入口，也不做只读展示——模型口径改由评测中心与
 * `/system/health` 呈现，个人中心只承载"我的账号与我的数据"。
 *
 * 布局（v1.14.3）：`Row align="stretch"` 让左右两列等高，右栏表格卡用 `.zd-fill-card` 撑满
 * （分页贴卡片底部），左栏危险区用 `flex-1` 间隔贴底——两栏底部因此始终对齐，
 * 不依赖"每页条数刚好凑够高度"（换窗口尺寸或记录变少都不会错位）。每页条数 15 见 `PAGE_SIZE`。
 *
 * 统计取数（不新增后端接口）：
 * - 会话数 / 上传报表数：各取一次列表的 `total`（报表里数 `is_preset = 0` 的）；
 * - 分析记录数与平均耗时：`GET /analysis/records`（列表项自带 `elapsed_ms`）；
 * - 近 7 天分析次数：7 次 `date_from = date_to = 当天 & page_size = 1` 读 `total`
 *   （比拉几百条记录更省，且不依赖分页完整性）。
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Popconfirm,
  Row,
  Segmented,
  Space,
  Table,
  Tag,
  Tooltip,
} from 'antd';
import {
  DeleteOutlined,
  LockOutlined,
  ReloadOutlined,
  SearchOutlined,
  UserOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';

import { ApiError } from '@/api/client';
import { analysisApi } from '@/api/analysis';
import { conversationApi } from '@/api/conversations';
import { reportApi } from '@/api/reports';
import type { RecordItem } from '@/api/types';
import PageHeader from '@/layouts/PageHeader';
import { useAuthStore } from '@/stores/auth';
import { formatDateTime, formatDuration } from '@/utils/format';
import { toast } from '@/utils/toast';
import {
  ClearRecordsDialog,
  DeleteAccountDialog,
  PasswordDialog,
  ProfileDialog,
} from './AccountDialogs';
import RecordDetailDrawer from './RecordDetailDrawer';

type StatusTab = 'all' | 'success' | 'failed';

/**
 * 每页条数（v1.14.3 调整：10 → 15）。
 *
 * 取值依据（1440×900 实测）：左栏（账号信息 321 + 统计 249 + 危险区 117 + 两个 16px 间距）自然高
 * **719px**，表格行高 **37px**、3 行时右卡 303px → 15 行约 747px，与左栏基本齐平。
 * 注意这只是"接近"，**真正的底部对齐由布局保证**（`Row align="stretch"` + 左栏危险区贴底 +
 * 右卡 `.zd-fill-card` 撑满），换窗口尺寸或数据量变少都不会错位。
 */
const PAGE_SIZE = 15;

export default function ProfilePage() {
  const user = useAuthStore((state) => state.user);

  const [items, setItems] = useState<RecordItem[]>([]);
  const [total, setTotal] = useState(0);
  const [statusCount, setStatusCount] = useState<Record<string, number>>({});
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState<StatusTab>('all');
  const [keyword, setKeyword] = useState('');
  const [range, setRange] = useState<[string, string] | null>(null);
  /**
   * 日期输入框的**草稿值**（与已生效的 `range` 分开）。
   *
   * 用户反馈：此前选好左边就把右端自动填成同一天并**立刻刷新**，等于"每次选一端都查一次"。
   * 现在的规则：**只改草稿、不请求**；只有当两端都选好时才提交为筛选条件（触发一次查询）。
   */
  const [rangeDraft, setRangeDraft] = useState<[string, string]>(['', '']);
  /** 「报表」列的筛选值（走**服务端** `report_code` 过滤，与时间筛选同一套口径；null = 不筛） */
  const [reportFilter, setReportFilter] = useState<string | null>(null);
  /** 「报表」列的可选项：来自报表列表（含预置 + 已删但仍有记录的编码） */
  const [reportCodes, setReportCodes] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [replaying, setReplaying] = useState<string | null>(null);
  const [detailCode, setDetailCode] = useState<string | null>(null);
  // 个人中心四个弹窗（v1.19.12）：改密码 / 编辑资料 / 清空历史 / 删除账号
  const [passwordOpen, setPasswordOpen] = useState(false);
  const [profileOpen, setProfileOpen] = useState(false);
  const [clearOpen, setClearOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);

  const [stats, setStats] = useState<{
    conversations: number | null;
    uploaded: number | null;
    avgElapsed: number | null;
    daily: { date: string; count: number }[];
  }>({ conversations: null, uploaded: null, avgElapsed: null, daily: [] });

  const loadRecords = useCallback(async () => {
    setLoading(true);
    try {
      const data = await analysisApi.records({
        page,
        page_size: PAGE_SIZE,
        status: status === 'all' ? undefined : status,
        q: keyword.trim() || undefined,
        date_from: range?.[0],
        date_to: range?.[1],
        report_code: reportFilter ?? undefined,
      });
      setItems(data.items);
      setTotal(data.total);
      setStatusCount(data.status_count ?? {});
      const durations = data.items.map((item) => item.elapsed_ms).filter((value): value is number => value !== null);
      setStats((prev) => ({
        ...prev,
        avgElapsed: durations.length ? durations.reduce((a, b) => a + b, 0) / durations.length : null,
      }));
    } catch (error) {
      if (error instanceof ApiError) toast.error(error.message, error.requestId);
    } finally {
      setLoading(false);
    }
  }, [page, status, keyword, range, reportFilter]);

  useEffect(() => {
    void loadRecords();
  }, [loadRecords]);

  // 统计：并行取会话数 / 上传报表数 / 近 7 天每日计数
  useEffect(() => {
    let alive = true;
    const days = Array.from({ length: 7 }, (_value, index) =>
      dayjs().subtract(6 - index, 'day').format('YYYY-MM-DD'),
    );
    void Promise.all([
      conversationApi.list(),
      reportApi.list(),
      ...days.map((date) =>
        analysisApi.records({ date_from: date, date_to: date, page: 1, page_size: 1 }),
      ),
    ])
      .then(([conversations, reports, ...dailyResults]) => {
        if (!alive) return;
        setStats((prev) => ({
          ...prev,
          conversations: conversations.total,
          uploaded: reports.items.filter((item) => item.is_preset === 0).length,
          daily: days.map((date, index) => ({ date, count: dailyResults[index]?.total ?? 0 })),
        }));
        // 「报表」列筛选项：预置 + 上传报表的编码（排序稳定，便于在下拉里找）
        setReportCodes(reports.items.map((item) => item.report_code).sort());
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);

  const replay = async (recordCode: string) => {
    setReplaying(recordCode);
    try {
      const detail = await analysisApi.replay(recordCode);
      // `replay_note` 由后端给出：原记录若属**只读的评测批次会话**，重跑会另起新会话执行
      // （否则会给评测留痕会话塞进新的提问与回答）——这句话必须原样告诉用户
      const note = detail.replay_note ? `；${detail.replay_note}` : '';
      toast.success(`已由 ${recordCode} 重跑，生成新记录 ${detail.record_code}${note}`);
      await loadRecords();
    } catch (error) {
      // 重跑失败要把「原因 + 下一步」两行都给到（UIUX 9.4）：hint 来自后端（如"报表已删除"）
      if (error instanceof ApiError) {
        toast.error(
          [error.message, error.hint].filter(Boolean).join('；'),
          error.requestId,
        );
      }
    } finally {
      setReplaying(null);
    }
  };

  const remove = async (recordCode: string) => {
    try {
      const result = await analysisApi.removeRecord(recordCode);
      toast.success(`已删除记录（同时清理 ${result.deleted_llm_calls} 条模型调用记录，对话文本保留）`);
      await loadRecords();
    } catch (error) {
      if (error instanceof ApiError) {
        toast.error([error.message, error.hint].filter(Boolean).join('；'), error.requestId);
      }
    }
  };

  const dailyMax = useMemo(
    () => Math.max(1, ...stats.daily.map((item) => item.count)),
    [stats.daily],
  );

  /** 日期草稿 → 生效：两端齐全才提交查询；缺一端则回到"不筛"（不发起请求） */
  const changeRangeDraft = (next: [string, string]) => {
    const [from, to] = next;
    if (from && to && from > to) {
      // 兜底：原生 min/max 已在日期选择器里拦掉越界选择，这里防"手输/脚本绕过"
      toast.warning('开始日期不能晚于结束日期');
      return;
    }
    setRangeDraft(next);
    if (from && to) {
      setRange([from, to]);
      setPage(1);
      return;
    }
    setRange(null);
  };

  const clearRange = () => {
    setRangeDraft(['', '']);
    setRange(null);
  };

  /**
   * 「报表」列筛选项 = 报表列表里的编码 ∪ 当前页记录里出现过的编码。
   *
   * 为什么要并集：报表被删后**记录仍在**（`report_code` 只是不再出现在报表列表里），
   * 只取报表列表会让这类记录筛不出来。
   */
  const reportFilterOptions = useMemo(() => {
    const codes = new Set(reportCodes);
    items.forEach((item) => {
      if (item.report_code) codes.add(item.report_code);
    });
    return [...codes].sort().map((code) => ({ text: code, value: code }));
  }, [reportCodes, items]);

  return (
    <div className="mx-auto flex max-w-[1440px] flex-col gap-4 p-6">
      <PageHeader
        icon={<UserOutlined />}
        title="个人中心"
        subtitle="账号信息 · 数据统计 · 分析记录"
      />

      {/* align="stretch"：两列等高（高度由较高的一栏决定），左右两栏底部才能对齐 */}
      <Row gutter={[16, 16]} align="stretch">
        <Col xs={24} xl={10} className="flex flex-col">
          <Card className="zd-card" size="small" title="账号信息">
            <Descriptions size="small" column={1} bordered>
              <Descriptions.Item label="用户名">{user?.username ?? '—'}</Descriptions.Item>
              <Descriptions.Item label="显示名">{user?.display_name ?? '—'}</Descriptions.Item>
              <Descriptions.Item label="邮箱">{user?.email ?? '—'}</Descriptions.Item>
              <Descriptions.Item label="最近登录">
                {formatDateTime(user?.last_login_at)}
              </Descriptions.Item>
              <Descriptions.Item label="密码状态">
                <Tag color="green" style={{ marginInlineEnd: 0 }}>
                  已加密（bcrypt）
                </Tag>
                <span className="ml-2 text-[11px] text-text-sub">
                  安全日志不随记录清空而删除
                </span>
              </Descriptions.Item>
              <Descriptions.Item label="注册时间">{user?.created_at ?? '—'}</Descriptions.Item>
            </Descriptions>

            <Space className="mt-3" size={8} wrap>
              <Tooltip title="需输入当前密码确认身份；改密后其它设备上的登录不会自动退出">
                <Button
                  size="small"
                  icon={<LockOutlined />}
                  onClick={() => setPasswordOpen(true)}
                >
                  修改密码
                </Button>
              </Tooltip>
              <Button size="small" onClick={() => setProfileOpen(true)}>
                编辑资料
              </Button>
            </Space>
          </Card>

          <Card className="zd-card mt-4" size="small" title="我的数据统计">
            <Row gutter={[12, 12]}>
              <Col span={6}>
                <div className="text-[11px] text-text-sub">会话数</div>
                <div className="zd-num text-h1">{stats.conversations ?? '—'}</div>
              </Col>
              <Col span={6}>
                <div className="text-[11px] text-text-sub">分析记录</div>
                <div className="zd-num text-h1">{total}</div>
              </Col>
              <Col span={6}>
                <div className="text-[11px] text-text-sub">上传报表</div>
                <div className="zd-num text-h1">{stats.uploaded ?? '—'}</div>
              </Col>
              <Col span={6}>
                <div className="text-[11px] text-text-sub">本页平均耗时</div>
                <div className="zd-num text-h1">{formatDuration(stats.avgElapsed)}</div>
              </Col>
            </Row>

            <div className="mt-4">
              <div className="mb-2 text-[11px] text-text-sub">近 7 天分析次数</div>
              <div className="flex items-end gap-2">
                {stats.daily.map((item) => (
                  <Tooltip key={item.date} title={`${item.date}：${item.count} 次`}>
                    <div className="flex flex-1 flex-col items-center gap-1">
                      <span className="zd-num text-[11px] text-text-sub">{item.count}</span>
                      <div
                        className="w-full rounded-t"
                        style={{
                          height: `${Math.max(4, (item.count / dailyMax) * 56)}px`,
                          background: 'var(--c-primary)',
                          opacity: item.count ? 1 : 0.25,
                        }}
                      />
                      <span className="text-[10px] text-text-sub">{item.date.slice(5)}</span>
                    </div>
                  </Tooltip>
                ))}
              </div>
            </div>
          </Card>

          {/* 危险区贴左栏底部：flex-1 的间隔吸收右栏更高时多出来的空间（min-h-4 保住最小间距） */}
          <div className="min-h-4 flex-1" />

          <Card
            className="zd-card"
            size="small"
            title={<span style={{ color: 'var(--c-anomaly)' }}>数据与隐私（危险区）</span>}
            style={{ borderColor: 'var(--c-anomaly)' }}
          >
            <div className="space-y-2 text-body">
              <div className="flex items-center gap-2">
                <Tooltip title="可选「只清分析记录（保留会话）」或「清空全部会话与对话历史」">
                  <Button size="small" danger onClick={() => setClearOpen(true)}>
                    清空历史记录
                  </Button>
                </Tooltip>
                <span className="text-[11px] text-text-sub">
                  逐条删除请在右侧"我的分析记录"里操作
                </span>
              </div>
              <div className="flex items-center gap-2">
                <Tooltip title="密码 + 用户名双确认；上传报表（含物理表）、会话、记录与评测批次一并删除">
                  <Button
                    size="small"
                    danger
                    icon={<DeleteOutlined />}
                    onClick={() => setDeleteOpen(true)}
                  >
                    删除账号
                  </Button>
                </Tooltip>
                <span className="text-[11px] text-text-sub">
                  安全审计日志按合规保留（归属字段置空）
                </span>
              </div>
            </div>
          </Card>
        </Col>

        <Col xs={24} xl={14} className="flex flex-col">
          <Card
            className="zd-card zd-fill-card flex-1"
            size="small"
            title="我的分析记录"
            extra={
              <Space>
                <Segmented<StatusTab>
                  size="small"
                  value={status}
                  onChange={(value) => {
                    setStatus(value);
                    setPage(1);
                  }}
                  options={[
                    { label: `全部 ${statusCount.success !== undefined ? (statusCount.success ?? 0) + (statusCount.failed ?? 0) : ''}`, value: 'all' },
                    { label: `成功 ${statusCount.success ?? 0}`, value: 'success' },
                    { label: `失败 ${statusCount.failed ?? 0}`, value: 'failed' },
                  ]}
                />
              </Space>
            }
          >
            <Space className="mb-3" wrap>
              <input
                className="rounded-input border px-2 py-1 text-body"
                style={{ borderColor: 'var(--c-line)', background: 'var(--c-bg-card)' }}
                placeholder="搜索问题关键词"
                value={keyword}
                onChange={(event) => {
                  setKeyword(event.target.value);
                  setPage(1);
                }}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') void loadRecords();
                }}
              />
              <Button size="small" icon={<SearchOutlined />} onClick={() => void loadRecords()}>
                搜索
              </Button>
              {/* 时间筛选：原先外面套了个 Tooltip 说明口径，但鼠标移到日期框就弹气泡、
                  挡住下方内容（用户反馈）→ 已撤掉，改由「清除」按钮与占位文本自解释 */}
              <Space size={4}>
                <input
                  type="date"
                  className="rounded-input border px-2 py-1 text-[11px]"
                  style={{ borderColor: 'var(--c-line)', background: 'var(--c-bg-card)' }}
                  value={rangeDraft[0]}
                  // 右端选好后，左端不能再选它之后的日期
                  max={rangeDraft[1] || undefined}
                  onChange={(event) => changeRangeDraft([event.target.value, rangeDraft[1]])}
                />
                <span className="text-text-sub">~</span>
                <input
                  type="date"
                  className="rounded-input border px-2 py-1 text-[11px]"
                  style={{ borderColor: 'var(--c-line)', background: 'var(--c-bg-card)' }}
                  value={rangeDraft[1]}
                  // 左端选好后，右端不能再选它之前的日期（原生选择器会把越界日期置灰）
                  min={rangeDraft[0] || undefined}
                  onChange={(event) => changeRangeDraft([rangeDraft[0], event.target.value])}
                />
                {rangeDraft[0] || rangeDraft[1] ? (
                  <Button size="small" type="text" onClick={clearRange}>
                    清除
                  </Button>
                ) : null}
              </Space>
              <Button size="small" icon={<ReloadOutlined />} onClick={() => void loadRecords()}>
                刷新
              </Button>
            </Space>

            {items.length === 0 && !loading ? (
              <Alert
                type="info"
                showIcon
                className="mb-3"
                message="还没有分析记录"
                description="去「对话工作台」提一个问题，记录会出现在这里，并支持重跑与删除。"
              />
            ) : null}

            <Table<RecordItem>
              size="small"
              rowKey="record_code"
              loading={loading}
              dataSource={items}
              // 筛选与翻页都从这里走（分页自己的 onChange 已撤，避免一次操作触发两次）
              onChange={(pagination, filters, _sorter, extra) => {
                if (extra.action === 'filter') {
                  // 列筛选改变 → 改服务端过滤值并回到第一页（筛选后总条数会变，停在第 3 页会看到空表）
                  setReportFilter(((filters.report_code?.[0] as string | undefined) ?? null));
                  setPage(1);
                  return;
                }
                setPage(pagination.current ?? 1);
              }}
              pagination={{
                current: page,
                pageSize: PAGE_SIZE,
                total,
                size: 'small',
                showSizeChanger: false,
                showTotal: (value) => `共 ${value} 条`,
              }}
              columns={[
                {
                  title: '时间',
                  dataIndex: 'created_at',
                  width: 132,
                  render: (value: string) => <span className="zd-num text-[11px]">{formatDateTime(value)}</span>,
                },
                {
                  title: '问题',
                  dataIndex: 'question',
                  ellipsis: true,
                  render: (value: string, row) => (
                    <Tooltip title={value}>
                      <span>
                        {value}
                        <span className="ml-1 font-mono text-[10px] text-text-sub">{row.record_code}</span>
                      </span>
                    </Tooltip>
                  ),
                },
                {
                  title: '报表',
                  dataIndex: 'report_code',
                  width: 140,
                  // 列筛选：**服务端** `report_code` 过滤（列表是分页的，前端过滤只会筛到当前页）
                  filters: reportFilterOptions,
                  filteredValue: reportFilter ? [reportFilter] : null,
                  filterMultiple: false,
                  render: (value: string | null) => value ?? '—',
                },
                {
                  title: '状态',
                  width: 108,
                  render: (_value, row) => (
                    <span className="flex items-center gap-1">
                      <Tag
                        color={row.status === 'success' ? 'green' : 'red'}
                        style={{ marginInlineEnd: 0 }}
                      >
                        {row.status === 'success' ? '成功' : '失败'}
                      </Tag>
                      {row.route ? <span className="text-[10px] text-text-sub">{row.route}</span> : null}
                    </span>
                  ),
                },
                {
                  title: '耗时',
                  dataIndex: 'elapsed_ms',
                  width: 76,
                  render: (value: number | null) => (
                    <span className="zd-num text-[11px]">{formatDuration(value)}</span>
                  ),
                },
                {
                  title: '操作',
                  width: 132,
                  render: (_value, row) => (
                    <div className="flex items-center gap-1">
                      <Tooltip title="查看该记录的 SQL/代码、结果表、图表与解读">
                        <Button type="link" size="small" onClick={() => setDetailCode(row.record_code)}>
                          查看
                        </Button>
                      </Tooltip>
                      <Tooltip
                        title={
                          row.status === 'success'
                            ? '整轮重跑（新记录，便于对比 prompt 改动前后）'
                            : '仅成功记录可重跑'
                        }
                      >
                        <Button
                          type="link"
                          size="small"
                          disabled={row.status !== 'success'}
                          loading={replaying === row.record_code}
                          onClick={() => void replay(row.record_code)}
                        >
                          重跑
                        </Button>
                      </Tooltip>
                      <Popconfirm
                        title="删除记录"
                        description="将删除该记录及其模型调用流水，对话文本保留。"
                        okText="删除"
                        cancelText="取消"
                        okButtonProps={{ danger: true }}
                        onConfirm={() => void remove(row.record_code)}
                      >
                        <Button type="link" size="small" danger>
                          删除
                        </Button>
                      </Popconfirm>
                    </div>
                  ),
                },
              ]}
            />
          </Card>
        </Col>
      </Row>

      <RecordDetailDrawer
        recordCode={detailCode}
        open={Boolean(detailCode)}
        onClose={() => setDetailCode(null)}
      />

      {/* 账号信息两个弹窗 + 危险区两个弹窗（v1.19.12） */}
      <PasswordDialog open={passwordOpen} onClose={() => setPasswordOpen(false)} />
      <ProfileDialog open={profileOpen} user={user} onClose={() => setProfileOpen(false)} />
      <ClearRecordsDialog
        open={clearOpen}
        onClose={() => setClearOpen(false)}
        onCleared={() => void loadRecords()}
      />
      <DeleteAccountDialog open={deleteOpen} user={user} onClose={() => setDeleteOpen(false)} />
    </div>
  );
}
