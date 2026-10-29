/**
 * 数据预览抽屉（UIUX 5.3 区域 2「预览」）。
 *
 * 复用对话工作台的 `ResultTable`（同一套数值对齐 / 标记列 / 分页），
 * 把头 50 行（可切到 200）当一张普通结果表渲染；表头另附排序依据与被过滤的代理键说明。
 *
 * v1.19.12 / v1.19.13 / v1.19.14 调整（用户实测反馈）：
 * 1. **关闭方式与其它抽屉统一**：撤掉底部多余的「关闭」按钮，用抽屉右上角自带的 ×
 *    （此前只有预览抽屉有底部按钮，其它抽屉没有，"别扭"）；
 * 2. **默认只显示日期列（v1.19.13）**：宽表里 `year / month / day / hour / weekday / is_weekend`
 *    都是从时间列**派生**出来的维度（供查询分组用），人眼看预览时纯属重复（用户实测反馈
 *    「小时报预览里 年/月/日/小时/星期/周末 跟首列的时间重复了」）→ 默认折叠，
 *    需要核对字段时勾一个开关即可展开；
 * 3. **撤掉「冻结列」**：上一轮加的冻结列在预览场景里多余（用户反馈），已移除；
 * 4. **表高由「每页条数」决定（v1.19.14）**：此前是"按视口撑开 + 内部滚动条"，
 *    与"前 20 / 50 / 200 行"这个**取数**开关混在一起，一屏里两套行数语义；现在
 *    **一次取满 API 上限（200 行）**，行数只由表格自带的分页器（10 / 20 / 50 / 100 条每页）
 *    决定，**表体不再套内部纵向滚动条** —— 换每页条数，表就多高（与「历史批次」同一套做法）。
 *
 * > 说明：**派生列不会从数据表里删掉** —— 题库 gold SQL 大量 `GROUP BY year / month / hour /
 * > weekday`，删列会让 120 题基准作废。这里只是**预览展示口径**的收敛。
 */

import { useEffect, useState } from 'react';
import { Alert, Checkbox, Drawer, Spin, Tooltip } from 'antd';

import { ApiError } from '@/api/client';
import { reportApi } from '@/api/reports';
import type { PreviewOut } from '@/api/types';
import { toast } from '@/utils/toast';
import ResultTable from '@/pages/Workbench/ResultTable';

/** 一次取满接口上限（`PREVIEW_MAX_LIMIT = 200`）；页内行数交给表格分页器控制 */
const PREVIEW_ROWS = 200;

/**
 * 时间派生维度列（预览默认折叠）。
 *
 * 这些列在各报表里都是**由时间列派生**出来的（`01_build_reports.py` 统一生成），
 * 用途是让模型与用户能直接 `GROUP BY 月 / 星期 / 小时`；预览时人眼只需看时间列。
 */
const DERIVED_TIME_COLUMNS = new Set([
  'year',
  'month',
  'day',
  'hour',
  'weekday',
  'is_weekend',
]);

/** 每页条数可选项（表高随之变化，不再有内部纵向滚动条） */
const PAGE_SIZE_OPTIONS = [10, 20, 50, 100];
const DEFAULT_PAGE_SIZE = 20;

export default function PreviewDrawer({
  reportCode,
  open,
  onClose,
}: {
  reportCode: string | null;
  open: boolean;
  onClose: () => void;
}) {
  const [preview, setPreview] = useState<PreviewOut | null>(null);
  const [showDerived, setShowDerived] = useState(false); // 默认折叠时间派生维度列
  const [loading, setLoading] = useState(false);

  // 展示列 = 全量列（或）剔除时间派生维度列
  const columns = showDerived
    ? preview?.columns ?? []
    : (preview?.columns ?? []).filter((column) => !DERIVED_TIME_COLUMNS.has(column.name));
  const hiddenCount = (preview?.columns.length ?? 0) - columns.length;

  useEffect(() => {
    setPreview(null);
    if (!open || !reportCode) return;
    let alive = true;
    setLoading(true);
    reportApi
      .preview(reportCode, PREVIEW_ROWS)
      .then((data) => {
        if (alive) setPreview(data);
      })
      .catch((error: unknown) => {
        if (error instanceof ApiError) toast.error(error.message, error.requestId);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [open, reportCode]);

  return (
    <Drawer
      title={`数据预览 · ${reportCode ?? ''}`}
      width={980}
      open={open}
      onClose={onClose}
      destroyOnClose
    >
      {loading ? (
        <div className="flex justify-center py-10">
          <Spin />
        </div>
      ) : !preview ? (
        <Alert type="warning" showIcon message="未加载到预览数据" />
      ) : (
        <>
          <div className="mb-3 flex flex-wrap items-center gap-2 text-[11px] text-text-sub">
            <span>
              表 {preview.table_name} · 共 {preview.total_rows ?? '—'} 行 · 本次取回{' '}
              {preview.returned_rows} 行 · 排序依据 {preview.order_by}
              （每页条数在表格底部分页器切换，表高随之变化）
            </span>
            <Tooltip
              title={
                '年 / 月 / 日 / 小时 / 星期 / 周末 都是由时间列派生的维度（供分组查询用），' +
                '人眼预览时与首列重复，故默认折叠；勾选可展开核对。' +
                '这些列在数据表里**保留**（题库分组口径依赖它们）。'
              }
            >
              <Checkbox
                checked={showDerived}
                onChange={(event) => setShowDerived(event.target.checked)}
              >
                <span className="text-[11px]">
                  显示派生列
                  {hiddenCount && !showDerived ? `（已折叠 ${hiddenCount} 列）` : ''}
                </span>
              </Checkbox>
            </Tooltip>
            {preview.excluded_columns.length ? (
              <span>已过滤代理键：{preview.excluded_columns.join('、')}</span>
            ) : null}
          </div>

          {/* `maxBodyHeight={null}`：不设表体高度上限、不套内部纵向滚动条 —— 表高由当前
              每页条数决定（用户诉求），超出抽屉时由抽屉自身滚动 */}
          <ResultTable
            table={{
              columns,
              rows: preview.rows,
              row_count: preview.returned_rows,
              truncated: preview.truncated,
            }}
            focusIndex={null}
            onHoverRow={() => undefined}
            maxBodyHeight={null}
            defaultPageSize={DEFAULT_PAGE_SIZE}
            pageSizeOptions={PAGE_SIZE_OPTIONS}
          />
        </>
      )}
    </Drawer>
  );
}
