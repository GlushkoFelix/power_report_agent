/**
 * 评测指标与归因（UIUX 5.4 区域 2–4）。
 *
 * 数据源全部是 `GET /eval/runs/{run_code}` 的 `metrics`（批次结束时落库）：
 * - 指标卡：`exec_correct_rate`（≥85%）/ `narrative_hit_rate`（≥90%）/ `avg_elapsed_ms` 与
 *   `p95_elapsed_ms`（≤8000ms）/ 陷阱题 `trap.rate`；
 *   **首字延迟本版本无数据**（客户端是单次 POST 非流式）→ 显示"—"并说明，**不显示 0**；
 * - 分类正确率：`by_type` / `by_difficulty`（低于 85% 用橙色标注）；
 * - 判分档：`tiers`（宽松档兜住多少题——正确率的"含金量"）；
 * - 归因：`attribution` 七类计数 + 每类"→ 下一步动作"（启发式，见后端 `_attribute` /
 *   `ATTRIBUTION_ORDER`；**顺序与全集以后端元组为准**，前端只负责文案）；
 * - 「与上一批次差值」按 UIUX 5.4 的说明由前端拉两次比对（后端无基线接口）。
 */

import { Alert, Card, Col, Collapse, Progress, Row, Statistic, Tag, Tooltip, Typography } from 'antd';

import { EVAL_ATTRIBUTION_LABELS, EVAL_DIFFICULTY_LABELS, EVAL_QTYPE_LABELS } from '@/api/eval';
import type { EvalMetrics, EvalRunDetail, EvalRunSummary } from '@/api/types';

const ATTRITION_ACTIONS: Record<string, string> = {
  澄清未执行: '槽位没解析出来、流程主动停下来问用户——看澄清卡缺哪个槽位，判断是规则还是提示词的问题',
  模型调用失败: '链路挂在模型调用层（E_LLM：超时/网络/空输出/不可解析）——先查端点健康与耗时，别当系统缺陷查',
  口径理解错误: '检查注入 Schema 的口径红线说明（R1/R3/R6/R7），必要时补同型陷阱题',
  'SQL 语法与函数': '看错题的错误码（E_SQL_FORBIDDEN / E_SQL_EXEC），补 SQLite 可用函数说明',
  'slot 继承错误': '检查多轮 slot 合并规则与提示词的增量抽取说明',
  执行失败未重试成功: '看错题的 retries / path_switched，评估重试矩阵是否覆盖该失败类型',
  结果口径偏差: '对比"生成 SQL vs 标准 SQL"的差异列与行，定位是列选择还是过滤条件问题',
};

function RateValue({
  rate,
  target,
  unit = '%',
}: {
  rate: number | null | undefined;
  target: number;
  unit?: string;
}) {
  if (rate === null || rate === undefined) return <span className="text-text-sub">—</span>;
  const percent = rate * 100;
  const ok = percent >= target;
  return (
    <span className="flex items-baseline gap-2">
      <span className="zd-num" style={{ color: ok ? 'var(--c-valley)' : 'var(--c-peak)' }}>
        {percent.toFixed(2)}
        {unit}
      </span>
      <Tag color={ok ? 'green' : 'orange'} style={{ marginInlineEnd: 0 }}>
        {ok ? '达标' : `目标 ≥${target}%`}
      </Tag>
    </span>
  );
}

function Delta({ current, previous }: { current: number | null; previous?: number | null }) {
  if (current === null || current === undefined) return null;
  if (previous === null || previous === undefined) {
    return <span className="text-[11px] text-text-sub">首次批次，无对比基线</span>;
  }
  const diff = (current - previous) * 100;
  const better = diff >= 0;
  return (
    <span className="text-[11px]" style={{ color: better ? 'var(--c-valley)' : 'var(--c-anomaly)' }}>
      较上一批次 {better ? '+' : ''}
      {diff.toFixed(2)} pt
    </span>
  );
}

function RateBars({
  title,
  data,
  labels,
}: {
  title: string;
  data: Record<string, { total: number; correct: number; rate: number | null }>;
  labels?: Record<string, string>;
}) {
  const entries = Object.entries(data ?? {});
  if (!entries.length) return null;
  return (
    <div>
      <div className="mb-2 font-semibold">{title}</div>
      <div className="space-y-2">
        {entries.map(([key, group]) => {
          const percent = (group.rate ?? 0) * 100;
          const low = percent < 85;
          const untested = group.total === 0;
          return (
            <div key={key} className="flex items-center gap-3">
              <span className="w-[76px] shrink-0 text-[11px] text-text-sub">
                {labels?.[key] ?? key}
              </span>
              {untested ? (
                // 本批次没出到这类题：显示"未出题"而不是 0% 的进度条（0% 会被读成"全错"）
                <>
                  <span className="flex-1 text-[11px] text-text-sub">未出题</span>
                  <span className="w-[76px] shrink-0 text-right text-[11px] text-text-sub">0/0</span>
                </>
              ) : (
                <>
                  <Progress
                    percent={Number(percent.toFixed(2))}
                    size="small"
                    strokeColor={low ? 'var(--c-peak)' : 'var(--c-valley)'}
                    format={(value) => `${value}%`}
                    className="!mb-0 flex-1"
                  />
                  <span className="zd-num w-[76px] shrink-0 text-right text-[11px] text-text-sub">
                    {group.correct}/{group.total}
                  </span>
                </>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function MetricsPanel({
  detail,
  previous,
}: {
  detail: EvalRunDetail;
  previous?: EvalRunSummary | null;
}) {
  const metrics: EvalMetrics | undefined = detail.metrics;
  if (!metrics) {
    return (
      <Alert
        type="info"
        showIcon
        message="批次尚未产出指标"
        description="逐题结果与指标会在批次结束后一次性下发（运行期后端不提供逐题进度）。"
      />
    );
  }

  const isSelfCheck = detail.impl === 'selfcheck';
  const tiers = metrics.tiers ?? {};
  const attributionEntries = Object.entries(metrics.attribution ?? {});

  return (
    <div className="space-y-3">
      {isSelfCheck ? (
        <Alert
          type="warning"
          showIcon
          message="自检模式（impl=selfcheck）：本批数据的正确率反映的是「评测链路自洽性」，不代表模型能力"
          description="自检按题库 gold_sql 回放 + 标准解读，正常应≈100%；任何偏差都说明链路有问题。真实评测请配置 LLM_API_KEY 后以 real 模式重跑。"
        />
      ) : null}

      <Row gutter={[12, 12]}>
        <Col xs={24} md={12} xl={6}>
          <Card className="zd-card" size="small">
            <Statistic
              title="执行正确率（目标 ≥85%）"
              valueRender={() => (
                <div className="flex flex-col gap-1">
                  <RateValue rate={metrics.exec_correct_rate} target={85} />
                  <Delta current={metrics.exec_correct_rate} previous={previous?.exec_correct_rate} />
                </div>
              )}
            />
          </Card>
        </Col>
        <Col xs={24} md={12} xl={6}>
          <Card className="zd-card" size="small">
            <Statistic
              title="结论关键数值命中率（≥90%）"
              valueRender={() => (
                <div className="flex flex-col gap-1">
                  <RateValue rate={metrics.narrative_hit_rate} target={90} />
                  <Delta
                    current={metrics.narrative_hit_rate}
                    previous={previous?.narrative_hit_rate}
                  />
                </div>
              )}
            />
          </Card>
        </Col>
        <Col xs={24} md={12} xl={6}>
          <Card className="zd-card" size="small">
            <Statistic
              title="平均响应时间（≤8s）"
              valueRender={() => (
                <div className="flex flex-col gap-1">
                  <span className="flex items-baseline gap-2">
                    <span
                      className="zd-num"
                      style={{
                        color:
                          (metrics.avg_elapsed_ms ?? 0) <= 8000
                            ? 'var(--c-valley)'
                            : 'var(--c-peak)',
                      }}
                    >
                      {metrics.avg_elapsed_ms === null ? '—' : `${(metrics.avg_elapsed_ms / 1000).toFixed(2)}s`}
                    </span>
                    <Tooltip title="P95：95% 的题目在该耗时内完成">
                      <Tag style={{ marginInlineEnd: 0 }}>
                        P95{' '}
                        {metrics.p95_elapsed_ms === null
                          ? '—'
                          : `${(metrics.p95_elapsed_ms / 1000).toFixed(2)}s`}
                      </Tag>
                    </Tooltip>
                  </span>
                  <span className="text-[11px] text-text-sub">
                    首字延迟：—（客户端为单次 POST、非流式，本版本未采集）
                  </span>
                </div>
              )}
            />
          </Card>
        </Col>
        <Col xs={24} md={12} xl={6}>
          <Card className="zd-card" size="small">
            <Statistic
              title="陷阱题正确率（口径红线）"
              valueRender={() => (
                <div className="flex flex-col gap-1">
                  <RateValue rate={metrics.trap?.rate ?? null} target={85} />
                  <span className="text-[11px] text-text-sub">
                    {metrics.trap?.correct ?? 0}/{metrics.trap?.total ?? 0} 题
                    {metrics.trap?.rules?.length ? ` · 覆盖 ${metrics.trap.rules.join('、')}` : ''}
                  </span>
                </div>
              )}
            />
          </Card>
        </Col>
      </Row>

      <Card className="zd-card" size="small" title="分类正确率">
        <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
          <RateBars title="按题型" data={metrics.by_type} labels={EVAL_QTYPE_LABELS} />
          <RateBars title="按难度" data={metrics.by_difficulty} labels={EVAL_DIFFICULTY_LABELS} />
        </div>
        <div className="mt-3 text-[11px] text-text-sub">
          低于 85% 的分组用橙色标注；判分档命中分布：第一档（列位置）{tiers['1'] ?? 0} 题、第二档（列名归一）
          {tiers['2'] ?? 0} 题、第三档（单列数值集合）{tiers['3'] ?? 0} 题——第三档越多，说明正确率里
          "靠宽松档兜住"的成分越大。
        </div>
      </Card>

      <Card className="zd-card" size="small" title={`bad case 归因（共 ${metrics.question_total - metrics.exec_correct_count} 道错题）`}>
        {attributionEntries.length === 0 ? (
          <Typography.Text type="secondary">本批次没有错题。</Typography.Text>
        ) : (
          <Row gutter={[12, 12]}>
            {attributionEntries.map(([label, count]) => (
              <Col xs={24} md={12} xl={8} key={label}>
                <div
                  className="rounded-card border p-3"
                  style={{ borderColor: 'var(--c-line-soft)', background: 'var(--c-bg-weak2)' }}
                >
                  <div className="flex items-center gap-2">
                    <span className="zd-num text-h2">{count}</span>
                    <span className="font-semibold">{EVAL_ATTRIBUTION_LABELS[label] ?? label}</span>
                  </div>
                  <div className="mt-1 text-[11px] text-text-sub">
                    → {ATTRITION_ACTIONS[label] ?? '查看错题的差异明细'}
                  </div>
                </div>
              </Col>
            ))}
          </Row>
        )}
      </Card>

      <Collapse
        items={[
          {
            key: 'more',
            label: '更多统计（差异类型 / 错误码 / 重试分布）',
            children: (
              <div className="grid grid-cols-1 gap-4 text-[11px] xl:grid-cols-3">
                <div>
                  <div className="mb-1 font-semibold">差异类型</div>
                  {Object.entries(metrics.diff_types ?? {}).map(([key, value]) => (
                    <div key={key} className="flex justify-between">
                      <span className="text-text-sub">{key}</span>
                      <span className="zd-num">{value}</span>
                    </div>
                  ))}
                  {!Object.keys(metrics.diff_types ?? {}).length ? <span className="text-text-sub">—</span> : null}
                </div>
                <div>
                  <div className="mb-1 font-semibold">错误码</div>
                  {Object.entries(metrics.errors ?? {}).map(([key, value]) => (
                    <div key={key} className="flex justify-between">
                      <span className="text-text-sub">{key}</span>
                      <span className="zd-num">{value}</span>
                    </div>
                  ))}
                  {!Object.keys(metrics.errors ?? {}).length ? <span className="text-text-sub">—</span> : null}
                </div>
                <div>
                  <div className="mb-1 font-semibold">重试分布（总重试 {metrics.retry_total} 次）</div>
                  {Object.entries(metrics.retry_distribution ?? {}).map(([key, value]) => (
                    <div key={key} className="flex justify-between">
                      <span className="text-text-sub">重试 {key} 次的题</span>
                      <span className="zd-num">{value}</span>
                    </div>
                  ))}
                </div>
              </div>
            ),
          },
        ]}
      />
    </div>
  );
}
