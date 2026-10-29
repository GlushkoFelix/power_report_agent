/**
 * 历史批次列表（UIUX 5.4 的批次切换入口 + 报告导出）。
 *
 * 说明：`GET /eval/runs` 只返回**批次摘要**（无逐题明细），因此列表上直接给关键指标，
 * 点某一行（或「查看」）会在**浮窗**里打开详情；导出在批次未完成时禁用（后端对未完成批次返回 400）。
 *
 * v1.19.12 / v1.19.13 调整（用户实测反馈）：
 * 1. **导出默认 PDF**：Markdown 需要工具才看得出排版，PDF 直接可读可打印（MD 仍保留为次要入口）；
 * 2. **分页取代内部滚动（v1.19.13）**：此前用"固定视口高度 + 内部滚动"压页面长度，滚轮与浮层
 *    提示叠在一起容易互相打架；现在改为**真分页**（每页 5/10/20 条，可切），
 *    **表高随每页条数自然变化**，内部不再套一层滚动条 —— 一次只渲染一页，页面也不会被撑长。
 */

import { Button, Table, Tag, Tooltip } from 'antd';

import { EVAL_STATUS_LABELS, evalApi } from '@/api/eval';
import type { EvalRunSummary } from '@/api/types';
import { formatDateTime } from '@/utils/format';
import { toast } from '@/utils/toast';

const STATUS_COLORS: Record<string, string> = {
  pending: 'default',
  running: 'processing',
  finished: 'green',
  failed: 'red',
};

/** 每页条数可选项（默认 10：一屏内看得完，且不用内部滚动） */
const PAGE_SIZE_OPTIONS = [5, 10, 20];
const DEFAULT_PAGE_SIZE = 10;

function pct(value: number | null): string {
  return value === null || value === undefined ? '—' : `${(value * 100).toFixed(2)}%`;
}

export default function RunsList({
  runs,
  activeCode,
  loading,
  onSelect,
}: {
  runs: EvalRunSummary[];
  activeCode: string | null;
  loading: boolean;
  onSelect: (runCode: string) => void;
}) {
  const exportReport = async (runCode: string, kind: 'pdf' | 'md') => {
    try {
      if (kind === 'pdf') {
        await evalApi.downloadReportPdf(runCode);
        toast.success('PDF 报告已开始下载');
      } else {
        await evalApi.downloadReport(runCode);
        toast.success('Markdown 报告已开始下载');
      }
    } catch {
      // blob 响应解析失败时（400/404 也是 blob）统一提示，避免暴露实现细节
      toast.error('报告导出失败：批次未完成或报告缺失');
    }
  };

  return (
    <Table<EvalRunSummary>
      size="small"
      rowKey="run_code"
      loading={loading}
      dataSource={runs}
      // 分页（每页条数决定表高）+ 横向滚动兜底；**不设 scroll.y**：不再套内部纵向滚动条
      pagination={{
        defaultPageSize: DEFAULT_PAGE_SIZE,
        pageSizeOptions: PAGE_SIZE_OPTIONS.map(String),
        showSizeChanger: true,
        size: 'small',
        showTotal: (total, [start, end]) => `第 ${start}–${end} 条 / 共 ${total} 个批次`,
      }}
      scroll={{ x: 'max-content' }}
      rowClassName={(row) => (row.run_code === activeCode ? 'zd-row-active' : '')}
      onRow={(row) => ({ onClick: () => onSelect(row.run_code), style: { cursor: 'pointer' } })}
      columns={[
        {
          title: '批次',
          dataIndex: 'run_code',
          render: (value: string, row) => (
            <div>
              <div className="flex items-center gap-2">
                <span className="font-mono text-[11px]">{value}</span>
                <Tag color={STATUS_COLORS[row.status] ?? 'default'} style={{ marginInlineEnd: 0 }}>
                  {EVAL_STATUS_LABELS[row.status] ?? row.status}
                </Tag>
                {row.model_name === 'scripted' ? <Tag style={{ marginInlineEnd: 0 }}>自检</Tag> : null}
              </div>
              <div className="text-[11px] text-text-sub">{row.name || '未命名批次'}</div>
            </div>
          ),
        },
        { title: '题量', dataIndex: 'question_total', width: 70 },
        {
          title: '执行正确率',
          width: 110,
          render: (_value, row) => (
            <span className="zd-num">{pct(row.exec_correct_rate)}</span>
          ),
        },
        {
          title: '命中率',
          width: 100,
          render: (_value, row) => <span className="zd-num">{pct(row.narrative_hit_rate)}</span>,
        },
        {
          title: '平均耗时',
          width: 100,
          render: (_value, row) => (
            <span className="zd-num">
              {row.avg_elapsed_ms === null ? '—' : `${(row.avg_elapsed_ms / 1000).toFixed(2)}s`}
            </span>
          ),
        },
        {
          title: '创建时间',
          dataIndex: 'created_at',
          width: 140,
          render: (value: string) => <span className="zd-num text-[11px]">{formatDateTime(value)}</span>,
        },
        {
          title: '操作',
          width: 220,
          render: (_value, row) => (
            <div className="flex items-center gap-1">
              <Button
                type="link"
                size="small"
                onClick={(event) => {
                  event.stopPropagation();
                  onSelect(row.run_code);
                }}
              >
                查看
              </Button>
              <Tooltip
                title={
                  row.status === 'finished'
                    ? '下载 PDF 报告（直接打开/打印，无需 Markdown 工具）'
                    : '批次未完成，暂无报告'
                }
              >
                <Button
                  type="link"
                  size="small"
                  disabled={row.status !== 'finished'}
                  onClick={(event) => {
                    event.stopPropagation();
                    void exportReport(row.run_code, 'pdf');
                  }}
                >
                  导出 PDF
                </Button>
              </Tooltip>
              <Tooltip title="下载 Markdown 原文（便于二次处理/归档）">
                <Button
                  type="link"
                  size="small"
                  disabled={row.status !== 'finished'}
                  onClick={(event) => {
                    event.stopPropagation();
                    void exportReport(row.run_code, 'md');
                  }}
                >
                  MD
                </Button>
              </Tooltip>
            </div>
          ),
        },
      ]}
    />
  );
}
