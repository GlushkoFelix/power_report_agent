/**
 * 展示层格式化工具（UIUX 4.3 数字等宽 / 7.3 表格规范 / 6.2 芯片文本格式）。
 */

import type { Slot, SlotFilter } from '@/api/types';

/** 列名 → 中文展示名（与后端 `chart_service.COLUMN_LABELS` 保持同源语义） */
export const COLUMN_LABELS: Record<string, string> = {
  consumption_kwh: '总用电量',
  total_kwh: '总电量',
  gap_kwh: '分钟电量',
  peak_kwh: '峰段电量',
  flat_kwh: '平段电量',
  valley_kwh: '谷段电量',
  sm1_kwh: '厨房',
  sm2_kwh: '洗衣房',
  sm3_kwh: '热水器与空调',
  kitchen_kwh: '厨房',
  laundry_kwh: '洗衣房',
  water_heating_ac_kwh: '热水器与空调',
  unmetered_kwh: '未计量用电',
  avg_power_kw: '平均功率',
  max_power_kw: '最大功率',
  min_power_kw: '最小功率',
  avg_voltage: '平均电压',
  max_voltage: '最大电压',
  avg_current: '平均电流',
  avg_intensity: '平均电流强度',
  avg_reactive_power_kw: '平均无功功率',
  is_anomaly: '异常标记',
  is_partial_period: '不完整周期',
  is_weekend: '周末',
  year_month: '年月',
  date: '日期',
  dt: '时间',
  dt_hour: '小时',
  year: '年',
  month: '月',
  day: '日',
  hour: '小时',
  weekday: '星期',
};

export function columnLabel(name: string): string {
  return COLUMN_LABELS[name] ?? name;
}

/** 数值格式化：保留 2–3 位有效小数、千分位（UIUX 7.3：界面按精度格式化展示为 2–3 位） */
export function formatNumber(value: unknown, digits = 2): string {
  if (value === null || value === undefined || value === '') return '—';
  const num = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(num)) return String(value);
  const abs = Math.abs(num);
  const fraction = abs >= 1000 ? 0 : abs >= 1 ? digits : 4;
  return num.toLocaleString('zh-CN', {
    minimumFractionDigits: 0,
    maximumFractionDigits: fraction,
  });
}

/** 大数值：用于指标卡（9,842.6 → 9,842.6；1234567 → 123.5 万） */
export function formatMetric(value: unknown, digits = 1): string {
  if (value === null || value === undefined || value === '') return '—';
  const num = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(num)) return String(value);
  if (Math.abs(num) >= 100_000_000) return `${(num / 100_000_000).toFixed(digits)} 亿`;
  if (Math.abs(num) >= 10_000) return `${(num / 10_000).toFixed(digits)} 万`;
  return num.toLocaleString('zh-CN', { maximumFractionDigits: digits });
}

/** 毫秒 → 秒（保留 1 位） */
export function formatSeconds(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—';
  return `${(ms / 1000).toFixed(1)}s`;
}

/**
 * 毫秒 → 自适应时长：<1s 显示毫秒（如 `34ms`），否则显示秒。
 *
 * 用途：记录列表 / 统计里既有几十毫秒的自检记录、也有几十秒的重活，
 * 一律显示秒会出现"0.0s"这种误导性读数。
 */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

/** ISO 文本 → `MM-DD HH:mm` */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value.replace(' ', 'T'));
  if (Number.isNaN(date.getTime())) return value;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return '—';
  return value.slice(0, 10);
}

// ── 条件芯片（UIUX 6.2 / 6.3）──────────────────────────────────────────────

export interface ChipItem {
  /** 字段名（`time_range` / `granularity` / `metric` / `filters` / `compare`） */
  key: string;
  label: string;
  /** 显示值（超 10 个中文字符时中间省略） */
  value: string;
  /** 完整值（tooltip 用） */
  fullValue: string;
  /** `metric` 不可清除（必填，UIUX 6.3） */
  removable: boolean;
}

const CHIP_FIELD_LABELS: [keyof Slot, string][] = [
  ['time_range', '时间'],
  ['granularity', '粒度'],
  ['metric', '指标'],
  ['filters', '筛选'],
  ['compare', '对比'],
];

function ellipsis(text: string, max = 10): string {
  if (text.length <= max) return text;
  return `${text.slice(0, max)}…`;
}

/** 单个过滤器 → 中文文案（`is_weekend = true` → "仅周末"） */
export function filterText(filter: SlotFilter): string {
  const { field, op, value } = filter;
  const label = columnLabel(field);
  if (field === 'is_weekend') {
    if (value === true) return '仅周末';
    if (value === false) return '仅工作日';
  }
  if (field === 'is_partial_period' && value === false) return '排除不完整周期';
  if (field === 'is_anomaly' && value === true) return '仅异常';
  const opText = op === '=' ? '' : op === '!=' ? '≠' : op;
  if (op === 'in' && Array.isArray(value)) return `${label} ∈ [${value.join('/')}]`;
  if (op === 'between' && Array.isArray(value)) return `${label} ${value[0]}~${value[1]}`;
  return `${label}${opText}${String(value)}`;
}

/** slot → 芯片列表（顺序固定：时间 → 粒度 → 指标 → 过滤 → 对比） */
export function slotChips(slot: Slot | null | undefined): ChipItem[] {
  if (!slot) return [];
  const chips: ChipItem[] = [];
  for (const [key, label] of CHIP_FIELD_LABELS) {
    const raw = slot[key as string];
    if (key === 'filters') {
      const filters = (raw as SlotFilter[] | undefined) ?? [];
      if (!filters.length) continue;
      const text = filters.map(filterText).join('、');
      chips.push({
        key: 'filters',
        label,
        value: ellipsis(text),
        fullValue: text,
        removable: true,
      });
      continue;
    }
    if (raw === null || raw === undefined || raw === '') continue;
    if (key === 'compare' && raw === '无') continue;
    let text: string;
    if (key === 'time_range' && typeof raw === 'object') {
      text = Object.values(raw as Record<string, unknown>).filter(Boolean).join(' → ') || '全期';
    } else {
      text = String(raw);
    }
    chips.push({
      key: key as string,
      label,
      value: ellipsis(text),
      fullValue: text,
      // metric 必填不可清除；「全期」是默认值，清掉没有意义
      removable: key !== 'metric',
    });
  }
  return chips;
}

/** 追问时"已继承"的提示文案（UIUX 5.1：回答顶部标注"已继承：时间=全期，粒度=月"） */
export function inheritedText(slot: Slot | null | undefined): string | null {
  const chips = slotChips(slot).filter((chip) => chip.removable);
  if (!chips.length) return null;
  return chips.map((chip) => `${chip.label}=${chip.fullValue}`).join('，');
}
