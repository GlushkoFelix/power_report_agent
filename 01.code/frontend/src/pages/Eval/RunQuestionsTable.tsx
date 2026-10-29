/**
 * 逐题结果表（UIUX 5.4「逐题结果」）。
 *
 * 筛选：全部 / 错误 / 陷阱题 / 多轮（对齐 UIUX 5.4 的筛选项）；
 * 行展开给出"生成 SQL vs 标准 SQL"对照、差异说明、漏掉的关键数值与解读文本——
 * 这些字段都来自批次结束时的 `result_json.questions[]`。
 */

import { useMemo, useState } from 'react';
import { Segmented, Space, Table, Tag, Tooltip, Typography } from 'antd';
import { CheckCircleFilled, CloseCircleFilled, WarningFilled } from '@ant-design/icons';

import { EVAL_QTYPE_LABELS } from '@/api/eval';
import type { EvalQuestionResult } from '@/api/types';

type FilterKey = 'all' | 'wrong' | 'trap' | 'multi';

const FILTERS: { label: string; value: FilterKey }[] = [
  { label: '全部', value: 'all' },
  { label: '错误', value: 'wrong' },
  { label: '陷阱题', value: 'trap' },
  { label: '多轮', value: 'multi' },
];

function CodePair({ label, text, tone }: { label: string; text: string | null; tone: 'gold' | 'gen' }) {
  return (
    <div className="min-w-0 flex-1">
      <div className="mb-1 flex items-center gap-1 text-[11px] text-text-sub">
        <span
          className="inline-block h-2 w-2 rounded-sm"
          style={{ background: tone === 'gold' ? 'var(--c-valley)' : 'var(--c-primary)' }}
        />
        {label}
      </div>
      <pre
        className="zd-scroll m-0 max-h-40 overflow-auto rounded-input border p-2 font-mono text-[11px] leading-5"
        style={{ borderColor: 'var(--c-line-soft)', background: 'var(--c-bg-weak2)' }}
      >
        {text?.trim() || '—'}
      </pre>
    </div>
  );
}

export default function RunQuestionsTable({ questions }: { questions: EvalQuestionResult[] }) {
  const [filter, setFilter] = useState<FilterKey>('all');

  const rows = useMemo(() => {
    if (filter === 'wrong') return questions.filter((item) => !item.is_correct);
    if (filter === 'trap') return questions.filter((item) => item.is_trap);
    if (filter === 'multi') return questions.filter((item) => item.qtype === 'multi_turn');
    return questions;
  }, [questions, filter]);

  return (
    <div>
      <div className="mb-2 flex items-center gap-3">
        <span className="font-semibold">逐题结果</span>
        <Segmented
          size="small"
          value={filter}
          options={FILTERS}
          onChange={(value) => setFilter(value as FilterKey)}
        />
        <span className="text-[11px] text-text-sub">
          共 {rows.length} 题{filter !== 'all' ? `（全部 ${questions.length} 题）` : ''}
        </span>
      </div>

      <Table<EvalQuestionResult>
        size="small"
        rowKey="question_code"
        dataSource={rows}
        pagination={{ pageSize: 20, size: 'small', showSizeChanger: false }}
        columns={[
          { title: '题号', dataIndex: 'question_code', width: 76, fixed: 'left' },
          {
            title: '问题',
            dataIndex: 'question',
            ellipsis: true,
            render: (value: string, row) => (
              <Tooltip title={value.replace(/\n/g, ' → ')}>
                <span>
                  {row.is_trap ? (
                    <Tooltip title={`口径陷阱题${row.trap_rule ? `（${row.trap_rule}）` : ''}`}>
                      <WarningFilled style={{ color: 'var(--c-peak)', marginRight: 4 }} />
                    </Tooltip>
                  ) : null}
                  {value.split('\n')[0]}
                </span>
              </Tooltip>
            ),
          },
          {
            title: '题型',
            dataIndex: 'qtype',
            width: 90,
            render: (value: string) => EVAL_QTYPE_LABELS[value] ?? value,
          },
          {
            title: '路径',
            dataIndex: 'route',
            width: 80,
            render: (value: string | null) =>
              value ? (
                <Tag color={value === 'pandas' ? 'purple' : 'blue'} style={{ marginInlineEnd: 0 }}>
                  {value}
                </Tag>
              ) : (
                '—'
              ),
          },
          {
            title: '结果',
            width: 120,
            render: (_value, row) => (
              <Space size={4}>
                {row.is_correct ? (
                  <CheckCircleFilled style={{ color: 'var(--c-valley)' }} />
                ) : (
                  <CloseCircleFilled style={{ color: 'var(--c-anomaly)' }} />
                )}
                <span className="text-[11px] text-text-sub">
                  {row.is_correct ? `第 ${row.matched_tier ?? '—'} 档` : (row.error_code ?? row.diff_type ?? '—')}
                </span>
              </Space>
            ),
          },
          {
            title: '耗时',
            dataIndex: 'elapsed_ms',
            width: 90,
            render: (value: number, row) => (
              <span className="zd-num text-[11px]">
                {(value / 1000).toFixed(2)}s
                {row.retries > 0 ? <span style={{ color: 'var(--c-peak)' }}> ·重试{row.retries}</span> : null}
              </span>
            ),
          },
          {
            title: '错误类型',
            dataIndex: 'diff_type',
            width: 130,
            render: (value: string | null) => (value ? <Tag>{value}</Tag> : '—'),
          },
        ]}
        expandable={{
          expandedRowRender: (row) => (
            <div className="space-y-2">
              <div className="flex flex-col gap-3 xl:flex-row">
                <CodePair label="标准 SQL（题库 gold）" text={row.gold_sql} tone="gold" />
                <CodePair
                  label={`生成 ${row.generated === 'pandas' ? 'pandas 代码' : 'SQL'}`}
                  text={row.generated_sql_or_code}
                  tone="gen"
                />
              </div>
              <div className="grid grid-cols-1 gap-2 text-[11px] xl:grid-cols-3">
                <div>
                  <span className="text-text-sub">判分说明：</span>
                  {row.diff_detail ?? '—'}
                </div>
                <div>
                  <span className="text-text-sub">关键数值命中：</span>
                  {row.key_hit_rate === null ? '未判定' : `${(row.key_hit_rate * 100).toFixed(0)}%`}
                  {row.key_missed?.length
                    ? ` · 漏掉：${row.key_missed.map((item) => item.label ?? '—').join('、')}`
                    : ''}
                </div>
                <div>
                  <span className="text-text-sub">执行信息：</span>
                  轮次 {row.rounds} · 重试 {row.retries}
                  {row.path_switched ? ' · 已换路' : ''}
                  {row.error_code ? ` · ${row.error_code}` : ''}
                </div>
              </div>
              {row.narrative ? (
                <Typography.Paragraph type="secondary" className="!mb-0 !text-[11px]">
                  模型解读：{row.narrative}
                </Typography.Paragraph>
              ) : null}
            </div>
          ),
        }}
      />
    </div>
  );
}
