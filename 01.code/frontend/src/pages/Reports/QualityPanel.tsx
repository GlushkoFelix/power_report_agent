/**
 * 数据质量面板（UIUX 5.3 区域 3）。
 *
 * 数据来自 `GET /reports/{code}/quality`：形状随数据集而变（预置报表含 A1–A5 口径校验，
 * 上传报表只有通用体检），因此采用**通用渲染**：关键项上卡 → 其余键值表 → checks 勾选表，
 * 而不是硬编码某一套字段（否则上传报表会整块空白）。
 *
 * 口径提示：预置报表的质量事实来自**分钟级源数据**，派生报表与源数据同源（quality.note）。
 */

import { Alert, Descriptions, Tag, Tooltip } from 'antd';
import { CheckCircleFilled, CloseCircleFilled } from '@ant-design/icons';

import type { QualityDetail } from '@/api/types';
import { formatNumber } from '@/utils/format';

/** 口径校验项的中文说明（键来自 PRD 8.11 的 A1–A5 锚点） */
const CHECK_LABELS: Record<string, string> = {
  A1_levels_equal: 'A1 三级总量一致（小时 / 日 / 月）',
  A2_structure_closed: 'A2 用途四项闭合（厨房+洗衣房+热水空调+未计量 = 总电量）',
  A3_time_closed: 'A3 时段三项闭合（峰+平+谷 = 总电量）',
  A4_unmetered_share: 'A4 未计量用电占比落在预期区间',
  A5_row_counts: 'A5 行数一致（派生表与源数据同源）',
};

/** 上指标卡的关键项 */
const HEADLINE_FIELDS: { key: string; label: string; kind: 'count' | 'ratio' }[] = [
  { key: 'missing_rows', label: '缺失行数', kind: 'count' },
  { key: 'missing_ratio', label: '缺失占比', kind: 'ratio' },
  { key: 'time_axis_gap', label: '时间轴缺口', kind: 'count' },
  { key: 'duplicate_timestamps', label: '重复时间戳', kind: 'count' },
  { key: 'voltage_out_of_range_minutes', label: '电压越界分钟', kind: 'count' },
  { key: 'active_minutes_ratio', label: '有效分钟占比', kind: 'ratio' },
];

function renderValue(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (typeof value === 'number') return formatNumber(value, 4);
  if (Array.isArray(value)) return value.length ? value.join('、') : '—';
  if (typeof value === 'object') return JSON.stringify(value, null, 0);
  return String(value);
}

export default function QualityPanel({
  quality,
  caliberVersion,
}: {
  quality: QualityDetail | null | undefined;
  caliberVersion?: string | null;
}) {
  if (!quality) {
    return <Alert type="info" showIcon message="该报表暂无质量报告" />;
  }

  const dataset = quality.dataset ?? {};
  const checks = quality.checks ?? {};
  const checkKeys = Object.keys(checks);
  const headline = HEADLINE_FIELDS.filter((field) => field.key in dataset);
  const headlineKeys = new Set(headline.map((field) => field.key));
  const rest = Object.entries(dataset).filter(([key]) => !headlineKeys.has(key));

  return (
    <div className="space-y-3">
      {quality.note ? (
        <Alert type="info" showIcon message={quality.note} />
      ) : null}

      {headline.length ? (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
          {headline.map((field) => {
            const raw = dataset[field.key];
            const display =
              field.kind === 'ratio' && typeof raw === 'number'
                ? `${(raw * 100).toFixed(4)}%`
                : renderValue(raw);
            const warn =
              (field.key === 'missing_ratio' && typeof raw === 'number' && raw > 0.01) ||
              (field.key === 'time_axis_gap' && Number(raw) > 0) ||
              (field.key === 'duplicate_timestamps' && Number(raw) > 0);
            return (
              <div
                key={field.key}
                className="rounded-card border p-3"
                style={{
                  borderColor: warn ? 'var(--c-peak)' : 'var(--c-line-soft)',
                  background: 'var(--c-bg-weak2)',
                }}
              >
                <div className="text-[11px] text-text-sub">{field.label}</div>
                <div className="zd-num text-h2" style={{ color: warn ? 'var(--c-peak)' : undefined }}>
                  {display}
                </div>
              </div>
            );
          })}
        </div>
      ) : null}

      {checkKeys.length ? (
        <div className="zd-card p-3">
          <div className="mb-2 font-semibold">口径校验（A1–A5 锚点）</div>
          <div className="space-y-1">
            {checkKeys.map((key) => (
              <div key={key} className="flex items-start gap-2 text-body">
                {checks[key] ? (
                  <CheckCircleFilled style={{ color: 'var(--c-valley)', marginTop: 3 }} />
                ) : (
                  <CloseCircleFilled style={{ color: 'var(--c-anomaly)', marginTop: 3 }} />
                )}
                <span>{CHECK_LABELS[key] ?? key}</span>
                {!checks[key] ? <Tag color="red">未通过</Tag> : null}
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {rest.length ? (
        <div className="zd-card p-3">
          <div className="mb-2 font-semibold">体检明细</div>
          <Descriptions size="small" column={2} bordered items={rest.map(([key, value]) => ({
            key,
            label: (
              <Tooltip title={key}>
                <span className="font-mono text-[11px]">{key}</span>
              </Tooltip>
            ),
            children: <span className="zd-num text-[11px]">{renderValue(value)}</span>,
          }))} />
        </div>
      ) : null}

      <div className="text-[11px] text-text-sub">
        口径版本：{quality.caliber_version ?? caliberVersion ?? '—'} · 体检时间：
        {quality.checked_at ?? '—'}
      </div>
    </div>
  );
}
