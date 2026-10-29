/**
 * 阶段时间线（UIUX 8.2）。
 *
 * 固定 5 个阶段：解析问题 → 生成 SQL/代码 → 沙箱执行 → 生成图表 → 生成解读；
 * 四种状态：已完成（青绿 ✓ + 耗时）/ 进行中（主色脉冲）/ 未开始（灰）/ 跳过（灰 +「跳过」）。
 * `aria-live="polite"` 让屏幕阅读器能播报阶段推进（UIUX 11）。
 */

import { Tooltip } from 'antd';

import type { StageState } from '@/stores/chat';
import { STAGE_LABELS } from '@/stores/chat';

function Dot({ status }: { status: StageState['status'] }) {
  if (status === 'done') {
    return (
      <span
        className="inline-flex h-3.5 w-3.5 items-center justify-center rounded-full text-[9px] text-white"
        style={{ background: 'var(--c-valley)' }}
      >
        ✓
      </span>
    );
  }
  if (status === 'running') {
    return (
      <span
        className="zd-pulse inline-block h-2.5 w-2.5 rounded-full"
        style={{ background: 'var(--c-primary)' }}
      />
    );
  }
  return (
    <span
      className="inline-block h-2.5 w-2.5 rounded-full"
      style={{ background: 'var(--c-text-disabled)' }}
    />
  );
}

export default function StageTimeline({
  stages,
  retries = 0,
}: {
  stages: StageState[];
  retries?: number;
}) {
  return (
    <div
      className="flex flex-wrap items-center gap-x-5 gap-y-1 text-table"
      aria-live="polite"
      aria-label="分析执行阶段"
    >
      {stages.map((stage) => {
        const done = stage.status === 'done';
        const running = stage.status === 'running';
        const skipped = stage.status === 'skipped';
        return (
          <span key={stage.key} className="flex items-center gap-1.5">
            <Dot status={stage.status} />
            <span
              style={{
                color:
                  done || running ? 'var(--c-text-main)' : 'var(--c-text-disabled)',
                fontWeight: running ? 600 : 400,
              }}
            >
              {STAGE_LABELS[stage.key]}
            </span>
            {done && stage.elapsedMs !== undefined ? (
              <span className="text-text-sub">{Math.max(0, stage.elapsedMs / 1000).toFixed(1)}s</span>
            ) : null}
            {running ? <span className="text-text-sub">…</span> : null}
            {skipped ? <span className="text-text-sub">（跳过）</span> : null}
            {stage.key === 'generating' && retries > 0 ? (
              <Tooltip title={`已带错误信息重写 ${retries} 次（自愈重试）`}>
                <span style={{ color: 'var(--c-peak)' }}>重试 {retries} 次</span>
              </Tooltip>
            ) : null}
          </span>
        );
      })}
    </div>
  );
}
