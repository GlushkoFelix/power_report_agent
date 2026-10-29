/**
 * 三种"非成功"卡片（UIUX 9.1 五种呈现层次中的三种）。
 *
 * | 组件 | 语义 | 颜色 | 是否可重试 |
 * | --- | --- | --- | --- |
 * | `ErrorBlock` | 执行失败 / 安全拦截 | 红 | 执行失败可重试；**安全拦截不提供重试**（UIUX 9.3） |
 * | `RefuseCard` | 超出数据或能力范围 | 中性灰 | 否（改为推荐问法） |
 * | `ClarifyCard` | 信息不足，属正常交互 | 主色 | 点选选项即继续 |
 *
 * 三者都不使用"错误"语气的地方：拒答与澄清**都不算失败**，红色只留给执行失败与安全拦截。
 */

import { useState } from 'react';
import { Alert, Button, Input, Space, Tag, Tooltip } from 'antd';
import {
  CloseCircleFilled,
  CopyOutlined,
  ExclamationCircleOutlined,
  StopFilled,
} from '@ant-design/icons';

import type { ClarifyPayload, CodePayload, ErrorPayload } from '@/api/types';
import { attemptedPathsText, presentationOf } from '@/api/errors';

/** 安全拦截类错误码（UIUX 9.3：显著阻断 + 不提供重试） */
const SECURITY_CODES = new Set(['E_SQL_FORBIDDEN', 'E_CODE_FORBIDDEN']);

function detailText(detail: ErrorPayload['detail']): string {
  if (!detail) return '';
  if (typeof detail === 'string') return detail;
  return Object.entries(detail)
    .filter(([, value]) => value !== null && value !== undefined && value !== '')
    .map(([key, value]) => `${key}=${Array.isArray(value) ? value.join('/') : String(value)}`)
    .join(' · ');
}

export function ErrorBlock({
  error,
  code,
  onRetry,
  onOpenSchema,
  onSuggest,
}: {
  error: ErrorPayload;
  code?: CodePayload | null;
  onRetry?: () => void;
  onOpenSchema?: () => void;
  onSuggest?: () => void;
}) {
  const presentation = presentationOf(error.code);
  const isSecurity = SECURITY_CODES.has(error.code);
  const attempts = attemptedPathsText(error.retries ?? 0, Boolean(error.path_switched));
  const detail = detailText(error.detail);

  const copyError = async () => {
    const text = [
      `[${error.code}] ${error.message}`,
      detail ? `detail: ${detail}` : '',
      `已尝试路径：${attempts}`,
      code ? `执行语句：${code.route === 'pandas' ? code.code : code.sql}` : '',
    ]
      .filter(Boolean)
      .join('\n');
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      /* 忽略剪贴板失败 */
    }
  };

  return (
    <div
      className="mt-3 rounded-card border-2 p-4"
      style={{
        borderColor: 'var(--c-anomaly)',
        background: 'var(--c-anomaly-bg)',
      }}
      role="alert"
    >
      <div className="flex items-start gap-2">
        {isSecurity ? (
          <StopFilled style={{ color: 'var(--c-anomaly)', fontSize: 16 }} />
        ) : (
          <CloseCircleFilled style={{ color: 'var(--c-anomaly)', fontSize: 16 }} />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-semibold" style={{ color: 'var(--c-anomaly)' }}>
              {isSecurity ? '请求已被安全拦截' : (presentation.title ?? '本次分析未生成结果')}
            </span>
            <Tag color="red" style={{ marginInlineEnd: 0 }}>
              {error.code}
            </Tag>
            {isSecurity ? (
              <Tooltip title="已写入安全审计日志（含用户、时间、命中规则与原始内容）">
                <Tag color="volcano" style={{ marginInlineEnd: 0 }}>
                  已记入审计
                </Tag>
              </Tooltip>
            ) : null}
          </div>

          <div className="mt-1 text-body" style={{ color: 'var(--c-text-main)' }}>
            {error.message}
          </div>

          {detail ? (
            <div className="mt-1 font-mono text-[11px] text-text-sub">{detail}</div>
          ) : null}

          <div className="mt-1 text-[11px] text-text-sub">已尝试路径：{attempts}</div>

          {isSecurity ? (
            <div className="mt-1 text-[11px] text-text-sub">
              系统仅提供**只读**分析能力：不执行写操作、不访问文件与网络。
            </div>
          ) : (
            <div className="mt-1 text-[11px] text-text-sub">
              本次未生成任何结果，系统不会给出推测数据。
            </div>
          )}

          <Space className="mt-3" size={8} wrap>
            {presentation.retryable && onRetry ? (
              <Button size="small" type="primary" onClick={onRetry}>
                重试
              </Button>
            ) : null}
            {isSecurity && onSuggest ? (
              <Button size="small" onClick={onSuggest}>
                换个问法
              </Button>
            ) : null}
            {onOpenSchema ? (
              <Button size="small" onClick={onOpenSchema}>
                查看字段字典
              </Button>
            ) : null}
            <Button size="small" icon={<CopyOutlined />} onClick={() => void copyError()}>
              复制错误
            </Button>
          </Space>
        </div>
      </div>
    </div>
  );
}

export function RefuseCard({
  message,
  suggestions,
  onPick,
}: {
  message: string;
  suggestions: string[];
  onPick: (question: string) => void;
}) {
  return (
    <div
      className="mt-3 rounded-card border p-4"
      style={{ borderColor: 'var(--c-line)', background: 'var(--c-neutral-bg)' }}
    >
      <div className="flex items-start gap-2">
        <ExclamationCircleOutlined style={{ color: 'var(--c-text-sub)', fontSize: 16 }} />
        <div>
          <div className="font-semibold">这个问题超出了当前报表能回答的范围</div>
          <div className="mt-1 text-body">{message}</div>
          <div className="mt-2 text-[11px] text-text-sub">
            当前报表为结构化用电数据（时间 / 用电量 / 峰平谷 / 用途拆分 / 电压电流），
            不支持预测、天气、电价等外部信息。
          </div>
          <div className="mt-2 flex flex-wrap gap-2">
            {suggestions.map((item) => (
              <Button key={item} size="small" onClick={() => onPick(item)}>
                {item}
              </Button>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

export function ClarifyCard({
  clarify,
  disabled,
  onSubmit,
}: {
  clarify: ClarifyPayload;
  disabled?: boolean;
  /** 回填后就地继续执行（`POST /analysis/clarify`） */
  onSubmit: (patch: Record<string, unknown>) => void;
}) {
  const [customMode, setCustomMode] = useState(false);
  const [customValue, setCustomValue] = useState('');

  const field = clarify.missing_slot || 'metric';
  const fieldLabels: Record<string, string> = {
    metric: '指标',
    time_range: '时间范围',
    granularity: '粒度',
    dimension: '分组维度',
    report: '报表',
  };

  const pick = (value: unknown) => {
    if (value === null) {
      // `value=null` 表示"自定义/不限" → 收集具体值后再提交（UIUX 8.1）
      setCustomMode(true);
      return;
    }
    onSubmit({ [field]: value });
  };

  return (
    <div
      className="mt-3 rounded-card border p-4"
      style={{ borderColor: 'var(--c-primary)', background: 'var(--c-primary-light)' }}
    >
      <div className="font-semibold" style={{ color: 'var(--c-primary)' }}>
        需要确认一下：{fieldLabels[field] ?? field}
      </div>
      <div className="mt-1 text-body">{clarify.question}</div>

      {!customMode ? (
        <div className="mt-3 flex flex-wrap gap-2">
          {clarify.options.map((option) => (
            <Button
              key={option.label}
              size="small"
              disabled={disabled}
              onClick={() => pick(option.value)}
            >
              {option.label}
            </Button>
          ))}
          <Button size="small" type="link" disabled={disabled} onClick={() => setCustomMode(true)}>
            都不是，我重新描述
          </Button>
        </div>
      ) : (
        <div className="mt-3 flex items-center gap-2">
          <Input
            size="small"
            autoFocus
            style={{ maxWidth: 240 }}
            placeholder={`请输入${fieldLabels[field] ?? field}`}
            value={customValue}
            disabled={disabled}
            onChange={(event) => setCustomValue(event.target.value)}
            onPressEnter={() => customValue.trim() && onSubmit({ [field]: customValue.trim() })}
          />
          <Button
            size="small"
            type="primary"
            disabled={disabled || !customValue.trim()}
            onClick={() => onSubmit({ [field]: customValue.trim() })}
          >
            继续
          </Button>
          <Button size="small" type="text" onClick={() => setCustomMode(false)}>
            返回选项
          </Button>
        </div>
      )}

      {clarify.partial_slot && Object.keys(clarify.partial_slot).length ? (
        <Alert
          className="mt-3"
          type="info"
          showIcon
          message="已解析的条件不会丢失，确认后会沿用上面的条件继续执行。"
        />
      ) : null}
    </div>
  );
}
