/**
 * 发起评测（UIUX 5.4 区域 1）。
 *
 * 契约要点：
 * - **温度固定 0 只读**（PRD F7：同题可复现）；
 * - 模型只读展示：厂商与主力模型名来自 `/system/health`（**不支持在线换厂商**，换厂商改 `.env`）；
 * - 执行模式（决策 D5）：`real` 真实模型 / `selfcheck` 自检回放（gold 回放，不消耗 token）；
 *   选 real 但未配 Key → 后端 400 + `hint`，这里内联展示并建议改用自检；
 * - 题量：全量（题库总数）或抽样 `limit`。
 */

import { useEffect, useState } from 'react';
import { Alert, Button, Card, Input, InputNumber, Segmented, Space, Switch, Tag, Tooltip } from 'antd';
import { ExperimentOutlined, ThunderboltFilled } from '@ant-design/icons';

import { ApiError } from '@/api/client';
import { evalApi } from '@/api/eval';
import { systemApi } from '@/api/system';
import type { EvalImpl, HealthOut } from '@/api/types';
import { toast } from '@/utils/toast';

export default function StartPanel({ onStarted }: { onStarted: (runCode: string) => void }) {
  const [impl, setImpl] = useState<EvalImpl>('selfcheck');
  const [fullSet, setFullSet] = useState(true);
  const [limit, setLimit] = useState<number>(20);
  const [includeNarrative, setIncludeNarrative] = useState(true);
  const [name, setName] = useState('');
  const [questionTotal, setQuestionTotal] = useState<number | null>(null);
  const [health, setHealth] = useState<HealthOut | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<{ message: string; hint?: string } | null>(null);

  useEffect(() => {
    evalApi
      .questions({ page_size: 1 })
      .then((data) => setQuestionTotal(data.total))
      .catch(() => setQuestionTotal(null));
    systemApi.health().then(setHealth).catch(() => setHealth(null));
  }, []);

  const llm = health?.llm;
  const keyConfigured = Boolean(llm?.configured);

  const start = async () => {
    setError(null);
    setStarting(true);
    try {
      const created = await evalApi.createRun({
        impl,
        name: name.trim() || null,
        limit: fullSet ? null : limit,
        include_narrative: includeNarrative,
      });
      toast.success(`评测批次 ${created.run_code} 已创建（${created.question_total} 题）`);
      onStarted(created.run_code);
    } catch (err) {
      if (err instanceof ApiError) {
        setError({
          message: err.message,
          hint:
            err.hint ??
            (impl === 'real'
              ? '可先把「执行模式」改为自检回放（selfcheck）验证链路，配置 Key 后再跑真实评测。'
              : undefined),
        });
      } else {
        setError({ message: '发起评测失败，请稍后重试' });
      }
    } finally {
      setStarting(false);
    }
  };

  return (
    <Card
      className="zd-card"
      title={
        <span className="flex items-center gap-2">
          <ExperimentOutlined />
          发起评测
        </span>
      }
      styles={{ body: { paddingTop: 16 } }}
    >
      {error ? (
        <Alert
          className="mb-3"
          type="error"
          showIcon
          message={error.message}
          description={error.hint}
        />
      ) : null}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <div>
          <div className="mb-1 text-[11px] text-text-sub">执行模式</div>
          <Segmented<EvalImpl>
            value={impl}
            onChange={setImpl}
            options={[
              {
                label: '真实模型（real）',
                value: 'real',
              },
              {
                label: '自检回放（selfcheck）',
                value: 'selfcheck',
              },
            ]}
          />
          <div className="mt-1 text-[11px] text-text-sub">
            {impl === 'real'
              ? '逐题走完整 Agent 链路，消耗 API 额度；无 Key 时接口会返回 400'
              : '按题库 gold 回放 + 标准解读，不消耗 token，用于验证「题库→链路→判分→指标→报告」全通'}
          </div>
        </div>

        <div>
          <div className="mb-1 text-[11px] text-text-sub">题量</div>
          <Space>
            <Segmented
              value={fullSet ? 'all' : 'sample'}
              onChange={(value) => setFullSet(value === 'all')}
              options={[
                { label: `全量（${questionTotal ?? 120} 题）`, value: 'all' },
                { label: '抽样', value: 'sample' },
              ]}
            />
            <InputNumber
              size="small"
              min={1}
              max={questionTotal ?? 120}
              value={limit}
              disabled={fullSet}
              onChange={(value) => setLimit(Number(value ?? 20))}
              addonAfter="题"
            />
          </Space>
          <div className="mt-1 text-[11px] text-text-sub">
            题库含口径陷阱题（R1/R3/R6/R7），抽样时按题库顺序取前 N 题
          </div>
        </div>

        <div>
          <div className="mb-1 text-[11px] text-text-sub">其他</div>
          <Space direction="vertical" size={4}>
            <Space size={8}>
              <span className="text-body">生成文字解读</span>
              <Switch size="small" checked={includeNarrative} onChange={setIncludeNarrative} />
              <span className="text-[11px] text-text-sub">
                {includeNarrative ? '计入结论命中率' : '跳过解读（更快）'}
              </span>
            </Space>
            <Input
              size="small"
              placeholder="批次名称（可选，如：prompt-v3.0 首跑）"
              maxLength={60}
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </Space>
        </div>
      </div>

      <div
        className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-2 rounded-card border p-3 text-[11px]"
        style={{ borderColor: 'var(--c-line-soft)', background: 'var(--c-bg-weak2)' }}
      >
        <span className="flex items-center gap-1">
          <ThunderboltFilled style={{ color: 'var(--c-primary)' }} />
          模型：
          <Tag style={{ marginInlineEnd: 0 }}>
            {llm?.provider ?? '—'} / {llm?.model_main ?? 'qwen-max'}
          </Tag>
          {llm?.impl === 'scripted' ? (
            <Tag color="orange" style={{ marginInlineEnd: 0 }}>
              未配置 Key（当前离线脚本模型）
            </Tag>
          ) : null}
        </span>
        <span className="text-text-sub">温度：0（只读，保证同题可复现）</span>
        <span className="text-text-sub">协议：{llm?.protocol ?? 'openai-compatible'}</span>
        <span className="text-text-sub">系统只支持 OpenAI 兼容厂商，换厂商改后端 .env</span>

        <Tooltip
          title={
            keyConfigured
              ? '已配置 Key，可选真实模型'
              : '未配置 LLM_API_KEY：选真实模型会返回 400，建议先用自检回放验证链路'
          }
        >
          <Button className="ml-auto" type="primary" loading={starting} onClick={() => void start()}>
            开始评测
          </Button>
        </Tooltip>
      </div>
    </Card>
  );
}
