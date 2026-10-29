/**
 * 结果数据表（UIUX 7.3 表格规范 + 7.1 图表联动）。
 *
 * 规范落地：列顺序沿用后端（维度 → 度量 → 占比 → 标记）；
 * 数值右对齐 + 等宽数字；`is_anomaly=1` 加红色「异常」标签（悬浮显示 `anomaly_reason`）、
 * `is_partial_period=1` 加灰色「不完整」标签；默认分页 20 行、可切 50/100；
 * `truncated=true` 时顶部提示并提供「按月汇总」快捷重聚合。
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Table, Tag, Tooltip } from 'antd';
import type { ColumnsType } from 'antd/es/table';

import type { TablePayload } from '@/api/types';
import { columnLabel, formatNumber } from '@/utils/format';

interface Props {
  table: TablePayload;
  /** 图表点击带来的定位点（全局行号）；变化时自动翻页并高亮 */
  focusIndex: number | null;
  onHoverRow: (index: number | null) => void;
  onAggregateByMonth?: () => void;
  /**
   * 表体最大高度（默认 `260` 固定，工作台右栏沿用）。
   *
   * - 传 `null` → **不设上限、不套内部纵向滚动条**：表高完全由当前每页条数决定
   *   （数据预览用，与「历史批次」同一套做法）；
   * - 传数值 / CSS 长度（如 `'calc(100vh - 260px)'`）→ 表体到达该高度后内部滚动。
   */
  maxBodyHeight?: number | string | null;
  /** 每页条数可选项（默认 20 / 50 / 100） */
  pageSizeOptions?: number[];
  /** 初始每页条数（默认 20） */
  defaultPageSize?: number;
}

function isRatioColumn(name: string): boolean {
  // 后端列契约只有 name/type，比率列用命名约定识别（与 PRD 8.8 的比率列命名一致）
  return /_ratio$|coverage|_rate$|avg_power|avg_voltage|avg_current|_intensity/.test(name);
}

export default function ResultTable({
  table,
  focusIndex,
  onHoverRow,
  onAggregateByMonth,
  maxBodyHeight = 260,
  pageSizeOptions = [20, 50, 100],
  defaultPageSize = 20,
}: Props) {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(defaultPageSize);
  const containerRef = useRef<HTMLDivElement>(null);

  const columns: ColumnsType<Record<string, unknown>> = useMemo(() => {
    const list: ColumnsType<Record<string, unknown>> = table.columns.map((column) => ({
      title: (
        <Tooltip
          title={isRatioColumn(column.name) ? '比率列不可相加：跨周期汇总需分子分母分别求和' : column.type}
        >
          <span>
            {columnLabel(column.name)}
            {isRatioColumn(column.name) ? (
              <span className="text-[10px] text-text-sub">（比率）</span>
            ) : null}
          </span>
        </Tooltip>
      ),
      dataIndex: column.name,
      key: column.name,
      ellipsis: true,
      align: /int|real|numeric|float|double/i.test(column.type) ? 'right' : 'left',
      render: (value: unknown) => {
        if (value === null || value === undefined) {
          return <span className="text-text-disabled">—</span>;
        }
        if (typeof value === 'number') {
          return <span className="zd-num">{formatNumber(value)}</span>;
        }
        return <span className="zd-num">{String(value)}</span>;
      },
    }));

    const hasMarker = table.rows.some(
      (row) => row.is_anomaly === 1 || row.is_partial_period === 1,
    );
    if (hasMarker) {
      list.push({
        title: '标记',
        key: '__marker__',
        width: 96,
        render: (_value, row) => (
          <span className="flex gap-1">
            {row.is_anomaly === 1 ? (
              <Tooltip title={String(row.anomaly_reason ?? '异常数据点')}>
                <Tag color="red" style={{ marginInlineEnd: 0 }}>
                  异常
                </Tag>
              </Tooltip>
            ) : null}
            {row.is_partial_period === 1 ? (
              <Tooltip title="不完整周期（该周期数据不齐，跨周期对比需谨慎）">
                <Tag style={{ marginInlineEnd: 0 }}>不完整</Tag>
              </Tooltip>
            ) : null}
          </span>
        ),
      });
    }
    return list;
  }, [table]);

  // 图表点击 → 翻到对应页并滚到可见处（UIUX 7.1：自动滚动到对应行并高亮，2s 后淡出）
  useEffect(() => {
    if (focusIndex === null) return;
    const targetPage = Math.floor(focusIndex / pageSize) + 1;
    setPage(targetPage);
    const timer = window.setTimeout(() => {
      containerRef.current
        ?.querySelector('.zd-row-active')
        ?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }, 60);
    return () => window.clearTimeout(timer);
  }, [focusIndex, pageSize]);

  const localFocus =
    focusIndex === null ? null : focusIndex - (page - 1) * pageSize >= 0
      ? focusIndex - (page - 1) * pageSize
      : null;

  return (
    <div ref={containerRef}>
      {table.truncated ? (
        <Alert
          className="mb-2"
          type="warning"
          showIcon
          message="结果已截断（上限 1000 行），请细化条件"
          action={
            onAggregateByMonth ? (
              <Button size="small" onClick={onAggregateByMonth}>
                按月汇总
              </Button>
            ) : null
          }
        />
      ) : null}

      <Table<Record<string, unknown>>
        size="small"
        rowKey={(_row, index) => String((page - 1) * pageSize + (index ?? 0))}
        columns={columns}
        dataSource={table.rows}
        // `y: null` → 不设表体高度上限（表高由每页条数决定，见 Props 注释）
        scroll={{ y: maxBodyHeight ?? undefined, x: 'max-content' }}
        pagination={{
          current: page,
          pageSize,
          size: 'small',
          showSizeChanger: true,
          pageSizeOptions: pageSizeOptions.map(String),
          total: table.row_count,
          showTotal: (total) => `共 ${total} 行`,
          onChange: (nextPage, nextSize) => {
            setPage(nextPage);
            setPageSize(nextSize);
          },
        }}
        rowClassName={(_row, index) =>
          localFocus !== null && index === localFocus ? 'zd-row-active' : ''
        }
        onRow={(_row, index) => ({
          onMouseEnter: () =>
            onHoverRow(index === undefined ? null : (page - 1) * pageSize + index),
          onMouseLeave: () => onHoverRow(null),
        })}
      />
    </div>
  );
}
