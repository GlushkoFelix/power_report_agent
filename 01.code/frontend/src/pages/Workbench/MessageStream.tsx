/**
 * 对话流（UIUX 5.1 区域 3–6、8.1 事件映射、8.3 等待体验）。
 *
 * 助手卡片按事件"逐项填充"：阶段时间线 → 代码块 → 表格（右栏）→ 图表（右栏）→ 解读 → 关键数值；
 * 失败 → 错误块、拒答 → 灰卡、缺条件 → 澄清卡（三种"非成功"卡片见 `StateCards.tsx`）。
 */

import { Alert, Button, Space, Tooltip } from 'antd';
import { CopyOutlined, ExportOutlined, ReloadOutlined, TagsOutlined } from '@ant-design/icons';

import type { CodePayload, Slot } from '@/api/types';
import type { AssistantTurn, Turn, UserTurn } from '@/stores/chat';
import { formatMetric } from '@/utils/format';
import { toast } from '@/utils/toast';
import CodeBlock from './CodeBlock';
import StageTimeline from './StageTimeline';
import Typewriter from './Typewriter';
import { ClarifyCard, ErrorBlock, RefuseCard } from './StateCards';

const REFUSE_SUGGESTIONS = ['全期总用电量是多少', '按月看用电趋势'];

function KeyNumbers({ turn }: { turn: AssistantTurn }) {
  const numbers = turn.narrative?.key_numbers ?? [];
  if (!numbers.length) return null;
  return (
    <div className="mt-3 flex flex-wrap gap-2">
      {numbers.map((item, index) => {
        const label = String(item.label ?? item.name ?? '指标');
        const value = item.value ?? item.amount;
        const unit = item.unit ? String(item.unit) : '';
        return (
          <div
            key={`${label}-${index}`}
            className="rounded-btn border px-3 py-1.5"
            style={{ borderColor: 'var(--c-line-soft)', background: 'var(--c-bg-weak2)' }}
          >
            <div className="text-[11px] text-text-sub">{label}</div>
            <div className="zd-num text-h3">
              {formatMetric(value)}
              {unit ? <span className="ml-1 text-[11px] text-text-sub">{unit}</span> : null}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function UserBubble({ turn }: { turn: UserTurn }) {
  return (
    <div className="flex justify-end">
      <div
        className="max-w-[72%] whitespace-pre-wrap rounded-card px-3 py-2 text-body"
        style={{ background: 'var(--c-primary)', color: '#fff' }}
      >
        {turn.text}
        {turn.reportCode ? (
          <span className="ml-2 text-[10px] opacity-75">{turn.reportCode}</span>
        ) : null}
      </div>
    </div>
  );
}

function AssistantCard({
  turn,
  onOpenSchema,
  onPickQuestion,
  onRetry,
  onReuseSlot,
  onClarify,
  onExport,
}: {
  turn: AssistantTurn;
  onOpenSchema: () => void;
  onPickQuestion: (question: string) => void;
  onRetry: (question: string) => void;
  onReuseSlot: (slot: Slot) => void;
  /** 澄清回填：走 `POST /analysis/clarify` 就地继续执行 */
  onClarify: (patch: Slot) => void;
  onExport: () => void;
}) {
  const streaming = turn.status === 'streaming';
  const success = turn.status === 'success';
  const code: CodePayload | null = turn.code;
  const inherited = turn.slot && !turn.fromHistory ? turn.slot : null;

  const copySql = async () => {
    const text = (code?.route === 'pandas' ? code.code : code?.sql) ?? '';
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      toast.success('已复制执行语句');
    } catch {
      toast.warning('剪贴板不可用，请手动选择复制');
    }
  };

  return (
    <div className="zd-card p-4">
      <StageTimeline stages={turn.stages} retries={turn.meta?.retries ?? 0} />

      {inherited && Object.keys(inherited).length ? (
        <div className="mt-2 text-[11px] text-text-sub">
          已继承会话条件（可在上方芯片栏修改或清除）。
        </div>
      ) : null}

      {code ? <CodeBlock code={code} onOpenSchema={onOpenSchema} /> : null}

      {streaming && !turn.narrative ? (
        <div className="mt-3 text-body text-text-sub">
          {turn.stages.find((stage) => stage.status === 'running')?.key === 'executing'
            ? '正在执行沙箱查询…'
            : '正在解析问题…'}
        </div>
      ) : null}

      {turn.narrative?.text ? (
        <div className="mt-3">
          <Typewriter text={turn.narrative.text} animate={!turn.fromHistory} />
        </div>
      ) : null}

      <KeyNumbers turn={turn} />

      {turn.narrative?.anomalies?.length ? (
        <Alert
          className="mt-3"
          type="warning"
          showIcon
          message="异常提示"
          description={
            <ul className="m-0 list-disc pl-4">
              {turn.narrative.anomalies.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          }
        />
      ) : null}

      {turn.error ? (
        <ErrorBlock
          error={turn.error}
          code={turn.code}
          onRetry={() => onRetry(turn.question)}
          onOpenSchema={onOpenSchema}
          onSuggest={() => onPickQuestion(REFUSE_SUGGESTIONS[0])}
        />
      ) : null}

      {turn.status === 'refused' && !turn.error ? (
        <RefuseCard
          message="该问题超出当前报表能回答的范围。"
          suggestions={REFUSE_SUGGESTIONS}
          onPick={onPickQuestion}
        />
      ) : null}

      {turn.clarify ? (
        <ClarifyCard
          clarify={turn.clarify}
          disabled={streaming}
          onSubmit={(patch) => onClarify(patch as Slot)}
        />
      ) : null}

      {turn.status === 'canceled' ? (
        <Alert
          className="mt-3"
          type="info"
          showIcon
          message="已取消（结果可能不完整）"
          description="已生成的部分内容保留在右侧面板，可继续追问。"
        />
      ) : null}

      {success ? (
        <Space className="mt-3" size={8} wrap>
          <Tooltip title="把本次生效的条件固化为当前口径">
            <Button
              size="small"
              icon={<TagsOutlined />}
              disabled={!turn.slot}
              onClick={() => turn.slot && onReuseSlot(turn.slot)}
            >
              沿用本次口径
            </Button>
          </Tooltip>
          <Button
            size="small"
            icon={<ReloadOutlined />}
            onClick={() => onRetry(turn.question)}
          >
            重新生成
          </Button>
          <Button size="small" icon={<CopyOutlined />} disabled={!code} onClick={() => void copySql()}>
            复制 SQL
          </Button>
          <Button size="small" icon={<ExportOutlined />} onClick={onExport}>
            导出图片
          </Button>
        </Space>
      ) : null}
    </div>
  );
}

export default function MessageStream({
  turns,
  onOpenSchema,
  onPickQuestion,
  onRetry,
  onReuseSlot,
  onClarify,
  onExport,
}: {
  turns: Turn[];
  onOpenSchema: () => void;
  onPickQuestion: (question: string) => void;
  onRetry: (question: string) => void;
  onReuseSlot: (slot: Slot) => void;
  onClarify: (patch: Slot) => void;
  onExport: () => void;
}) {
  return (
    <div className="zd-scroll min-h-0 flex-1 px-4 py-4">
      <div className="mx-auto flex max-w-[860px] flex-col gap-4">
        {turns.map((turn) =>
          turn.role === 'user' ? (
            <UserBubble key={turn.id} turn={turn} />
          ) : (
            <AssistantCard
              key={turn.id}
              turn={turn}
              onOpenSchema={onOpenSchema}
              onPickQuestion={onPickQuestion}
              onRetry={onRetry}
              onReuseSlot={onReuseSlot}
              onClarify={onClarify}
              onExport={onExport}
            />
          ),
        )}
      </div>
    </div>
  );
}
