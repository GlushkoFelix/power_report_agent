/**
 * 条件芯片栏（UIUX 6.1–6.4；落地契约 v1.10.0）。
 *
 * 行为定案：
 * - 芯片顺序固定：时间 → 粒度 → 指标 → 筛选 → 对比 → 「+」；
 * - 点击芯片弹出编辑气泡，点 × 清除；**`metric` 不可清除**（必填，UIUX 6.3）；
 * - 编辑后以 `slot_patch` 随下一次提问提交（后端先合入会话条件再做增量抽取），
 *   同时提供「按新条件重算」直接重跑上一次问题（"编辑后重算即刻可用"）；
 * - 执行中芯片禁用（灰底；悬浮提示"生成中，请稍候"）。
 */

import { useMemo, useState } from 'react';
import { Button, Checkbox, DatePicker, Popover, Select, Space, Tooltip } from 'antd';
import { CloseOutlined, InfoCircleOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons';
import dayjs from 'dayjs';

import type { SlotFilter } from '@/api/types';
import { useChatStore } from '@/stores/chat';
import { slotChips } from '@/utils/format';

const GRANULARITY_OPTIONS = ['小时', '日', '月', '年'].map((value) => ({ value, label: value }));
const TIME_OPTIONS = ['全期', '今年', '近 30 天', '自定义'].map((value) => ({ value, label: value }));
const COMPARE_OPTIONS = ['无', '同比', '环比'].map((value) => ({ value, label: value }));
const METRIC_OPTIONS = [
  '总用电量',
  '峰段电量',
  '平段电量',
  '谷段电量',
  '未计量用电',
  '厨房',
  '洗衣房',
  '热水器与空调',
].map((value) => ({ value, label: value }));

/** 常用过滤器（UIUX 6.3：多选标签） */
const FILTER_PRESETS: { label: string; filter: SlotFilter }[] = [
  { label: '仅周末', filter: { field: 'is_weekend', op: '=', value: true } },
  { label: '仅工作日', filter: { field: 'is_weekend', op: '=', value: false } },
  { label: '仅异常', filter: { field: 'is_anomaly', op: '=', value: true } },
  { label: '排除不完整周期', filter: { field: 'is_partial_period', op: '=', value: false } },
];

export default function SlotBar({
  onRecalculate,
  streaming,
}: {
  onRecalculate: () => void;
  streaming: boolean;
}) {
  const slot = useChatStore((state) => state.slot);
  const pendingPatch = useChatStore((state) => state.pendingPatch);
  const editChip = useChatStore((state) => state.editChip);
  const clearChip = useChatStore((state) => state.clearChip);
  const clearPendingPatch = useChatStore((state) => state.clearPendingPatch);

  const [openField, setOpenField] = useState<string | null>(null);
  const chips = useMemo(() => slotChips(slot), [slot]);
  const filters = ((slot?.filters as SlotFilter[] | undefined) ?? []).filter(Boolean);

  const renderEditor = (field: string) => {
    if (field === 'time_range') {
      const current = typeof slot?.time_range === 'string' ? slot.time_range : '全期';
      const isCustom = current.includes('→');
      return (
        <Space direction="vertical" size={8} style={{ width: 250 }}>
          <Select
            size="small"
            style={{ width: '100%' }}
            value={TIME_OPTIONS.some((item) => item.value === current) ? current : '自定义'}
            options={TIME_OPTIONS}
            onChange={(value) => {
              if (value === '自定义') return;
              editChip('time_range', value);
              setOpenField(null);
            }}
          />
          <DatePicker.RangePicker
            size="small"
            style={{ width: '100%' }}
            value={
              isCustom
                ? [dayjs(current.split('→')[0].trim()), dayjs(current.split('→')[1].trim())]
                : null
            }
            onChange={(range) => {
              if (!range?.[0] || !range[1]) return;
              // 与提示词里的时间过滤写法一致（左闭右开由后端处理）
              editChip(
                'time_range',
                `${range[0].format('YYYY-MM-DD')} → ${range[1].format('YYYY-MM-DD')}`,
              );
              setOpenField(null);
            }}
          />
          <div className="text-[11px] text-text-sub">
            区间超出报表数据范围时，后端会按数据实际范围截断。
          </div>
        </Space>
      );
    }

    if (field === 'granularity') {
      return (
        <Select
          size="small"
          style={{ width: 160 }}
          value={(slot?.granularity as string | undefined) ?? undefined}
          options={GRANULARITY_OPTIONS}
          placeholder="由问题自动推断"
          onChange={(value) => {
            editChip('granularity', value);
            setOpenField(null);
          }}
        />
      );
    }

    if (field === 'metric') {
      return (
        <Space direction="vertical" size={4} style={{ width: 220 }}>
          <Select
            size="small"
            style={{ width: '100%' }}
            value={(slot?.metric as string | undefined) ?? undefined}
            options={METRIC_OPTIONS}
            showSearch
            placeholder="选择或输入指标"
            onSelect={(value: string) => {
              editChip('metric', value);
              setOpenField(null);
            }}
          />
          <div className="text-[11px] text-text-sub">指标为必填条件，不可清除。</div>
        </Space>
      );
    }

    if (field === 'compare') {
      return (
        <Select
          size="small"
          style={{ width: 140 }}
          value={(slot?.compare as string | undefined) ?? '无'}
          options={COMPARE_OPTIONS}
          onChange={(value) => {
            editChip('compare', value);
            setOpenField(null);
          }}
        />
      );
    }

    return (
      <Checkbox.Group
        style={{ display: 'flex', flexDirection: 'column', gap: 6 }}
        value={filters.map((item) => `${item.field}:${String(item.value)}`)}
        options={FILTER_PRESETS.map((preset) => ({
          label: preset.label,
          value: `${preset.filter.field}:${String(preset.filter.value)}`,
        }))}
        onChange={(values) => {
          const next = FILTER_PRESETS.filter((preset) =>
            (values as string[]).includes(`${preset.filter.field}:${String(preset.filter.value)}`),
          ).map((preset) => preset.filter);
          editChip('filters', next);
        }}
      />
    );
  };

  const chipStyle = (disabled: boolean) => ({
    background: disabled ? 'var(--c-bg-weak)' : 'var(--c-primary-light)',
    borderColor: disabled ? 'var(--c-line)' : 'var(--c-primary)',
    color: disabled ? 'var(--c-text-disabled)' : 'var(--c-primary)',
  });

  return (
    <div
      className="shrink-0 border-b px-4 py-2"
      style={{ background: 'var(--c-bg-card)', borderColor: 'var(--c-line)' }}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-text-sub">当前口径</span>

        {chips.map((chip) => (
          <Popover
            key={chip.key}
            trigger="click"
            placement="bottomLeft"
            open={openField === chip.key}
            onOpenChange={(open) => setOpenField(open ? chip.key : null)}
            content={renderEditor(chip.key)}
          >
            <Tooltip title={streaming ? '生成中，请稍候' : chip.fullValue}>
              <span
                className="flex h-[26px] items-center gap-1 rounded-chip border px-2 text-[12px]"
                style={{ ...chipStyle(streaming), cursor: streaming ? 'not-allowed' : 'pointer' }}
                aria-label={`编辑条件：${chip.label}`}
              >
                {chip.label}：{chip.value}
                {chip.removable && !streaming ? (
                  <CloseOutlined
                    style={{ fontSize: 10 }}
                    onClick={(event) => {
                      event.stopPropagation();
                      clearChip(chip.key);
                    }}
                  />
                ) : null}
              </span>
            </Tooltip>
          </Popover>
        ))}

        {chips.length === 0 ? (
          <span className="text-text-sub">暂无生效条件（提问后自动解析）</span>
        ) : null}

        <Popover
          trigger="click"
          placement="bottomLeft"
          content={
            <Space direction="vertical" size={2}>
              <Button type="text" size="small" disabled={streaming} onClick={() => setOpenField('filters')}>
                {filters.length ? '修改筛选条件' : '添加筛选条件'}
              </Button>
              <Button type="text" size="small" disabled={streaming} onClick={() => setOpenField('time_range')}>
                修改时间范围
              </Button>
              <Button type="text" size="small" disabled={streaming} onClick={() => setOpenField('granularity')}>
                修改聚合粒度
              </Button>
              <Button type="text" size="small" disabled={streaming} onClick={() => setOpenField('compare')}>
                设置同比 / 环比
              </Button>
            </Space>
          }
        >
          <span
            className="flex h-[26px] items-center gap-1 rounded-chip border px-2 text-[12px]"
            style={{
              borderColor: 'var(--c-line)',
              color: streaming ? 'var(--c-text-disabled)' : 'var(--c-text-sub)',
              cursor: streaming ? 'not-allowed' : 'pointer',
            }}
          >
            <PlusOutlined style={{ fontSize: 10 }} />
            添加
          </span>
        </Popover>
      </div>

      {pendingPatch && Object.keys(pendingPatch).length && !streaming ? (
        <div
          className="mt-2 flex flex-wrap items-center gap-2 rounded-input px-3 py-1.5 text-table"
          style={{ background: 'var(--c-primary-light)', color: 'var(--c-primary)' }}
        >
          <InfoCircleOutlined />
          <span>条件已修改，将在下次提问时生效，也可立即按新条件重算。</span>
          <Button size="small" type="primary" icon={<ReloadOutlined />} onClick={onRecalculate}>
            按新条件重算
          </Button>
          <Button size="small" type="text" onClick={clearPendingPatch}>
            撤销修改
          </Button>
        </div>
      ) : null}
    </div>
  );
}
