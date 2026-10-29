/**
 * 执行信息（UIUX 5.1 区域 8 + S7 免责声明）。
 *
 * 数据来源是 SSE 的 `meta` 事件：`elapsed_ms` / `retries` / `llm_tokens` / `model` / `llm_impl`。
 * 「报表口径版本」不在事件载荷里（只在记录详情 `GET /analysis/records/{code}` 的
 * `schema_version`），本批如实展示"口径版本见记录详情"，不编造。
 */

import type { ReactNode } from 'react';
import { Tag, Tooltip } from 'antd';

import type { AssistantTurn } from '@/stores/chat';
import { formatSeconds } from '@/utils/format';

function Row({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex items-start gap-2 text-table">
      <span className="w-[52px] shrink-0 text-text-sub">{label}</span>
      <span className="min-w-0 flex-1 break-all">{value}</span>
    </div>
  );
}

export default function ExecInfo({ turn }: { turn: AssistantTurn | null }) {
  const route = turn?.code?.route ?? (turn?.chart ? 'sql' : null);
  const isPandas = route === 'pandas';
  const model = turn?.meta?.model;
  const impl = turn?.meta?.llm_impl;

  return (
    <div
      className="rounded-card border p-3"
      style={{ borderColor: 'var(--c-line-soft)', background: 'var(--c-bg-weak2)' }}
    >
      <div className="mb-2 font-semibold">执行信息</div>
      <div className="space-y-1">
        <Row
          label="路径"
          value={
            <span className="flex items-center gap-2">
              <Tag color={route ? (isPandas ? 'purple' : 'blue') : 'default'} style={{ marginInlineEnd: 0 }}>
                {route ? (isPandas ? 'pandas 沙箱' : 'SQL 直查') : '—'}
              </Tag>
              <span className="text-text-sub">重试 {turn?.meta?.retries ?? 0} 次</span>
            </span>
          }
        />
        <Row label="耗时" value={formatSeconds(turn?.meta?.elapsed_ms)} />
        <Row
          label="模型"
          value={
            model ? (
              <Tooltip title={impl === 'scripted' ? '当前未配置模型 Key，链路走离线脚本模型' : impl}>
                <span>
                  {model}
                  {impl === 'scripted' ? (
                    <Tag className="ml-2" style={{ marginInlineEnd: 0 }}>
                      离线脚本模型
                    </Tag>
                  ) : null}
                </span>
              </Tooltip>
            ) : (
              '—'
            )
          }
        />
        <Row
          label="tokens"
          value={turn?.meta?.llm_tokens ? turn.meta.llm_tokens.toLocaleString('zh-CN') : '—'}
        />
        <Row
          label="口径"
          value={<span className="text-text-sub">报表口径版本见记录详情 · 结论仅供参考</span>}
        />
      </div>
      <div className="mt-2 border-t pt-2 text-[11px] text-text-sub" style={{ borderColor: 'var(--c-line-soft)' }}>
        免责声明：结论由大模型基于当前报表数据生成，仅供参考，不作为计量或结算依据。
      </div>
    </div>
  );
}
