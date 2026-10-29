/**
 * 右栏 · 图表与表格（UIUX 5.1 区域 7、7.1 联动矩阵、7.2 图表规范）。
 *
 * 联动实现（本地状态，不调 LLM）：
 * - 图上点某点 → `focusIndex` → 表格翻页 + 滚动 + 高亮（主色浅底，2s 后淡出）；
 * - 表格行 hover → `hoverIndex` → 图上对应点加粗（通过 `Plotly.restyle` 之外的方案：
 *   本批以"图例/点高亮"最简实现，行 hover 时给容器加描边提示，避免与后端 figure 耦合）。
 *
 * 类型切换走 `POST /charts/rerender`（后端回写 `chart_json`）；其中「表格」是**纯前端切换**：
 * 结果表格本就在下方常驻，不必让后端重画一张 table 类型的图。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Empty, Segmented, Select, Tag, Tooltip } from 'antd';
import { DownloadOutlined, LineChartOutlined } from '@ant-design/icons';
import type { PlotlyHTMLElement } from 'plotly.js-dist-min';

import type { ChartPayload, ChartType } from '@/api/types';
import type { AssistantTurn } from '@/stores/chat';
import { LEGEND_ITEMS } from '@/theme/tokens';
import { useThemeStore } from '@/stores/theme';
import ExecInfo from './ExecInfo';
import ResultChart, { exportFigurePng } from './ResultChart';
import ResultTable from './ResultTable';

/** 图表类型切换（UIUX 5.1 区域 7）；「表格」是前端视图切换，不请求后端 */
const CHART_TYPES: { label: string; value: ChartType }[] = [
  { label: '折线', value: 'line' },
  { label: '柱状', value: 'bar' },
  { label: '饼图', value: 'pie' },
  { label: '表格', value: 'table' },
];

const GRANULARITY_OPTIONS = ['小时', '日', '月', '年', '全期'].map((value) => ({
  value,
  label: value,
}));

function Legend() {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-text-sub">
      {LEGEND_ITEMS.map((item) => (
        <span key={item.key} className="flex items-center gap-1">
          <span
            className="inline-block h-2 w-2 rounded-sm"
            style={{ background: `var(${item.cssVar})` }}
          />
          {item.label}
        </span>
      ))}
      <span className="ml-auto">口径：谷 00–08 / 平 08–18 / 峰 18–24</span>
    </div>
  );
}

function ChartNotes({ chart }: { chart: ChartPayload | null }) {
  if (!chart) return null;
  const notes: string[] = [];
  if (chart.unit) notes.push(`单位：${chart.unit}`);
  if (chart.r3_adjusted) notes.push(chart.r3_note ?? '已按 R3 只展示一套拆分');
  if (chart.merged_count > 0) notes.push(`其余 ${chart.merged_count} 项已合并为「其他」`);
  if (chart.downsampled_from) notes.push(`原始 ${chart.downsampled_from} 个点已抽稀展示`);
  if (chart.gap_count > 0) notes.push(`缺采 ${chart.gap_count} 处已断开（不插值）`);
  if (!notes.length) return null;
  return (
    <div className="mt-1 flex flex-wrap gap-1">
      {notes.map((note) => (
        <Tag key={note} style={{ marginInlineEnd: 0 }} color="default">
          {note}
        </Tag>
      ))}
    </div>
  );
}

export default function ResultPanel({
  turn,
  onRerender,
  onOpenSchema,
  exportSignal = 0,
  variant = 'rail',
}: {
  turn: AssistantTurn | null;
  onRerender: (payload: { chart_type?: ChartType; granularity?: string }) => void;
  onOpenSchema: () => void;
  /** 递增即触发一次导出（消息卡片上的「导出图片」按钮通过它请求右栏导出） */
  exportSignal?: number;
  /** `rail` = 桌面右栏固定 408px；`drawer` = 窄屏抽屉内**占满宽度**（UIUX 10.1） */
  variant?: 'rail' | 'drawer';
}) {
  const mode = useThemeStore((state) => state.mode);
  const [focusIndex, setFocusIndex] = useState<number | null>(null);
  const [, setHoverIndex] = useState<number | null>(null);
  const [viewMode, setViewMode] = useState<'chart' | 'table'>('chart');
  const [chartNode, setChartNode] = useState<PlotlyHTMLElement | null>(null);

  const handleReady = useCallback((node: PlotlyHTMLElement) => setChartNode(node), []);

  const chart = turn?.chart ?? null;
  const table = turn?.table ?? null;
  const currentType = chart?.chart_type && chart.chart_type !== 'table' ? chart.chart_type : 'line';
  const figure = chart?.figure ?? null;
  const degraded = Boolean(chart && !figure);

  const exportImage = useCallback(async () => {
    // 后端 PNG 导出入口为 `GET …/chart.png`（kaleido 未装时 501），
    // 本版前端 `Plotly.toImage` 是唯一可用路径（UIUX 7.1 落地行为），导出固定亮色底。
    if (!chartNode) return;
    await exportFigurePng(chartNode, `chart_${turn?.recordId ?? 'result'}`);
  }, [chartNode, turn?.recordId]);

  /**
   * 导出是**一次性动作**，必须只消费一次信号。
   *
   * 此前的写法是 `if (exportSignal > 0) exportImage()`，而依赖里带着 `exportImage`
   * ——它的身份随 `turn?.recordId` / `chartNode` 变化而重建，于是**切换会话（turn 变）时
   * 副作用会再次触发**，表现就是"导出一次之后，每点一个其它会话都自动下载一张图"。
   * 改为用 ref 记录已消费的信号值，只有信号**真正增大**时才导出一次。
   */
  const handledSignalRef = useRef(0);
  useEffect(() => {
    if (exportSignal === 0 || exportSignal === handledSignalRef.current) return;
    handledSignalRef.current = exportSignal;
    void exportImage();
  }, [exportSignal, exportImage]);

  return (
    <aside
      className={`zd-scroll flex shrink-0 flex-col gap-3 border-l p-3 ${
        variant === 'drawer' ? 'h-full w-full' : 'w-[408px]'
      }`}
      style={{ background: 'var(--c-bg-page)', borderColor: 'var(--c-line)' }}
    >
      <div className="zd-card p-3">
        <div className="mb-2 flex items-center gap-2">
          <span className="font-semibold">图表</span>
          <Segmented<ChartType>
            size="small"
            value={viewMode === 'table' ? 'table' : (currentType as ChartType)}
            options={CHART_TYPES}
            onChange={(value) => {
              if (value === 'table') {
                setViewMode('table');
                return;
              }
              setViewMode('chart');
              if (value !== currentType) onRerender({ chart_type: value });
            }}
          />
          <Tooltip title={turn?.recordId ? '导出 PNG（2 倍图，亮色底）' : '尚未生成结果'}>
            <Button
              size="small"
              icon={<DownloadOutlined />}
              disabled={!figure}
              onClick={() => void exportImage()}
            >
              导出图片
            </Button>
          </Tooltip>
        </div>

        <div className="mb-2 flex items-center gap-2">
          <span className="text-[11px] text-text-sub">聚合粒度</span>
          <Select
            size="small"
            style={{ width: 96 }}
            placeholder="原粒度"
            options={GRANULARITY_OPTIONS}
            disabled={!turn?.recordId}
            onChange={(value) => onRerender({ granularity: value })}
          />
          <span className="text-[11px] text-text-sub">只能由细变粗，重聚合不调模型</span>
        </div>

        {degraded ? (
          <Alert
            type="info"
            showIcon
            message="该结果不适合可视化，已降级为表格"
            description={chart?.reason ?? undefined}
          />
        ) : null}

        {figure && viewMode === 'chart' ? (
          <>
            <ResultChart
              figure={figure}
              mode={mode}
              height={280}
              onPointClick={setFocusIndex}
              onReady={handleReady}
            />
            <ChartNotes chart={chart} />
            <div className="mt-2">
              <Legend />
            </div>
          </>
        ) : null}

        {!figure && !degraded ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="提问后这里会显示图表"
            className="!my-6"
          />
        ) : null}
      </div>

      <div className="zd-card p-3">
        <div className="mb-2 flex items-center gap-2">
          <span className="font-semibold">数据表格</span>
          <span className="text-[11px] text-text-sub">（与图表联动）</span>
          <Button type="link" size="small" className="ml-auto !px-0" onClick={onOpenSchema}>
            字段字典
          </Button>
        </div>

        {table && table.rows.length ? (
          <ResultTable
            table={table}
            focusIndex={focusIndex}
            onHoverRow={setHoverIndex}
            onAggregateByMonth={() => onRerender({ granularity: '月' })}
          />
        ) : turn?.status === 'streaming' ? (
          <div className="py-6 text-center text-text-sub">
            <LineChartOutlined /> 正在执行查询…
          </div>
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无结果数据" className="!my-4" />
        )}
      </div>

      <ExecInfo turn={turn} />
    </aside>
  );
}
