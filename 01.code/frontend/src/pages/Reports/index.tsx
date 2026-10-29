/**
 * 报表管理页（UIUX 5.3）。
 *
 * 区域构成：① 上传区（两步向导，见 `UploadWizard`）② 报表列表 ③ 质量报告抽屉 ④ Schema 字典抽屉。
 *
 * 两条与后端契约绑定的规则：
 * - **活跃报表高亮**：会话内取 `conversations.active_report_code`，会话外回落**系统默认活跃报表**
 *   （`is_active = 1`，当前为 `r_daily`）——与对话工作台顶部的选择器同一个真源；
 * - **删除按钮由 `is_deletable` 驱动**（预置报表为 false → 置灰并说明原因），前端**不自行拼
 *   `is_preset` 规则**（UIUX 5.3 决策 1）。
 */

import { useEffect, useState } from 'react';
import { Alert, Button, Popconfirm, Table, Tag, Tooltip } from 'antd';
import {
  CloudUploadOutlined,
  DatabaseOutlined,
  ReloadOutlined,
} from '@ant-design/icons';

import { ApiError } from '@/api/client';
import { reportApi } from '@/api/reports';
import type { Report } from '@/api/types';
import PageHeader from '@/layouts/PageHeader';
import { useChatStore } from '@/stores/chat';
import { useReportStore } from '@/stores/reports';
import { formatDateTime } from '@/utils/format';
import { toast } from '@/utils/toast';
import SchemaDrawer from '@/pages/Workbench/SchemaDrawer';
import PreviewDrawer from './PreviewDrawer';
import QualityPanel from './QualityPanel';
import UploadWizard from './UploadWizard';
import { Drawer } from 'antd';
import type { QualityOut } from '@/api/types';

const REPORT_TYPE_LABELS: Record<string, string> = {
  daily: '日报',
  monthly: '月报',
  hourly: '小时表',
  minute: '分钟表',
  structure: '结构表',
  uploaded: '上传',
};

export default function ReportsPage() {
  const reports = useReportStore((state) => state.items);
  const loading = useReportStore((state) => state.loading);
  const load = useReportStore((state) => state.load);
  const systemActive = useReportStore((state) => state.systemActive);

  const conversationReportCode = useChatStore(
    (state) => state.activeDetail?.active_report_code ?? null,
  );
  const setActiveReport = useChatStore((state) => state.setActiveReport);

  const [uploadOpen, setUploadOpen] = useState(false);
  const [previewCode, setPreviewCode] = useState<string | null>(null);
  const [schemaCode, setSchemaCode] = useState<string | null>(null);
  const [quality, setQuality] = useState<{ code: string; data: QualityOut | null } | null>(null);
  const [highlightCode, setHighlightCode] = useState<string | null>(null);

  useEffect(() => {
    void load(true);
  }, [load]);

  const activeCode = conversationReportCode ?? systemActive()?.report_code ?? null;

  const openQuality = async (code: string) => {
    setQuality({ code, data: null });
    try {
      const data = await reportApi.quality(code);
      setQuality({ code, data });
    } catch (error) {
      if (error instanceof ApiError) toast.error(error.message, error.requestId);
      setQuality(null);
    }
  };

  const remove = async (report: Report) => {
    try {
      const result = await reportApi.remove(report.report_code);
      const extra = result.affected_conversations
        ? `（已同步重置 ${result.affected_conversations} 个会话的活跃报表）`
        : '';
      toast.success(`已删除 ${result.filename}${extra}`);
      await load(true);
    } catch (error) {
      if (error instanceof ApiError) toast.error(error.message, error.requestId);
    }
  };

  const activate = async (code: string) => {
    await setActiveReport(code);
    await load(true);
  };

  return (
    <div className="mx-auto max-w-[1440px] p-6">
      <PageHeader
        icon={<DatabaseOutlined />}
        title="报表管理"
        subtitle="预置 5 张报表全用户共享；你上传的报表仅自己可见"
        note="预置报表数据源：UCI #235（Individual household electric power consumption），由 03_init_db.py 导入并按 R1–R7 口径聚合为 5 张报表；上传报表仅本人可见，删除后会级联清理其分析记录。"
        extra={
          <>
            <Button icon={<ReloadOutlined />} onClick={() => void load(true)}>
              刷新
            </Button>
            <Button
              type="primary"
              icon={<CloudUploadOutlined />}
              onClick={() => setUploadOpen((value) => !value)}
            >
              {uploadOpen ? '收起上传' : '上传报表'}
            </Button>
          </>
        }
      />

      {uploadOpen ? (
        <div className="mb-4">
          <UploadWizard
            onFinished={(code) => {
              setHighlightCode(code);
              void load(true);
            }}
            onOpenSchema={(code) => setSchemaCode(code)}
            onActivate={(code) => void activate(code)}
          />
        </div>
      ) : null}

      <div className="zd-card p-4">
        <Table<Report>
          size="small"
          rowKey="report_code"
          loading={loading}
          dataSource={reports}
          rowClassName={(row) => (row.report_code === highlightCode ? 'zd-row-active' : '')}
          pagination={false}
          columns={[
            {
              title: '报表',
              dataIndex: 'filename',
              render: (value: string, row) => (
                <div>
                  <div className="flex items-center gap-2">
                    <span className="font-semibold">{value}</span>
                    <Tag color="blue" style={{ marginInlineEnd: 0 }}>
                      {REPORT_TYPE_LABELS[row.report_type] ?? row.report_type}
                    </Tag>
                    {row.is_preset === 1 ? <Tag style={{ marginInlineEnd: 0 }}>系统预置</Tag> : null}
                    {row.report_code === activeCode ? (
                      <Tag color="green" style={{ marginInlineEnd: 0 }}>
                        当前报表
                      </Tag>
                    ) : null}
                  </div>
                  <div className="font-mono text-[11px] text-text-sub">
                    {row.report_code} · {row.table_name}
                  </div>
                </div>
              ),
            },
            {
              title: '数据时间范围',
              width: 200,
              render: (_value, row) => (
                <span className="zd-num text-[11px]">
                  {row.time_start ?? '—'} ~ {row.time_end ?? '—'}
                </span>
              ),
            },
            {
              title: '行数 / 列数',
              width: 130,
              render: (_value, row) => (
                <span className="zd-num text-[11px]">
                  {row.row_count ?? '—'} / {row.column_count ?? '—'}
                </span>
              ),
            },
            {
              title: '上传时间',
              width: 150,
              render: (_value, row) =>
                row.is_preset === 1 ? (
                  <span className="text-text-sub">系统预置</span>
                ) : (
                  <span className="zd-num text-[11px]">{formatDateTime(row.created_at)}</span>
                ),
            },
            {
              title: '状态',
              width: 110,
              render: (_value, row) => (
                <span className="flex flex-col gap-1">
                  <Tag color={row.status === 'ready' ? 'green' : 'orange'} style={{ marginInlineEnd: 0 }}>
                    {row.status === 'ready' ? '可用' : row.status}
                  </Tag>
                  {!row.table_exists ? (
                    <Tooltip title="元数据存在但 reports.db 中找不到物理表（数据可能已被外部删除）">
                      <Tag color="red" style={{ marginInlineEnd: 0 }}>
                        物理表缺失
                      </Tag>
                    </Tooltip>
                  ) : null}
                </span>
              ),
            },
            {
              title: '操作',
              width: 300,
              render: (_value, row) => (
                <div className="flex flex-wrap items-center gap-1">
                  <Button type="link" size="small" onClick={() => setPreviewCode(row.report_code)}>
                    预览
                  </Button>
                  <Button type="link" size="small" onClick={() => void openQuality(row.report_code)}>
                    质量
                  </Button>
                  <Button type="link" size="small" onClick={() => setSchemaCode(row.report_code)}>
                    Schema
                  </Button>
                  <Tooltip title={row.report_code === activeCode ? '已是当前报表' : '设为当前分析报表'}>
                    <Button
                      type="link"
                      size="small"
                      disabled={row.report_code === activeCode}
                      onClick={() => void activate(row.report_code)}
                    >
                      切换
                    </Button>
                  </Tooltip>
                  <Popconfirm
                    title="删除报表"
                    description="将级联清理该报表的分析记录，安全日志保留。此操作不可撤销。"
                    okText="删除"
                    cancelText="取消"
                    okButtonProps={{ danger: true }}
                    onConfirm={() => void remove(row)}
                    disabled={!row.is_deletable}
                  >
                    <Tooltip title={row.is_deletable ? '' : '系统预置报表不可删除（全用户共享数据源）'}>
                      <Button type="link" size="small" danger disabled={!row.is_deletable}>
                        删除
                      </Button>
                    </Tooltip>
                  </Popconfirm>
                </div>
              ),
            },
          ]}
        />
      </div>

      {reports.length === 0 && !loading ? (
        <Alert
          className="mt-3"
          type="info"
          showIcon
          message="还没有可用报表"
          description="点右上角「上传报表」上传一份含时间列的 CSV，系统会自动建表并生成字段说明。"
        />
      ) : null}

      <PreviewDrawer
        reportCode={previewCode}
        open={Boolean(previewCode)}
        onClose={() => setPreviewCode(null)}
      />

      <SchemaDrawer
        reportCode={schemaCode}
        open={Boolean(schemaCode)}
        onClose={() => setSchemaCode(null)}
      />

      <Drawer
        title={`数据质量报告 · ${quality?.code ?? ''}`}
        width={760}
        open={Boolean(quality)}
        onClose={() => setQuality(null)}
        destroyOnClose
      >
        <QualityPanel
          quality={quality?.data?.quality}
          caliberVersion={quality?.data?.caliber_version}
        />
      </Drawer>
    </div>
  );
}
