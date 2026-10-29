/**
 * 打字机效果（UIUX 8.1：`narrative` 事件到达后逐字追加，每段 40ms/字以内）。
 *
 * 只对"本轮实时产出"的解读生效；历史消息直接整段渲染（避免刷新时整屏重打一遍）。
 * 通过 `requestAnimationFrame` 批量推进（一次多字），既保留观感又不产生大量重渲染。
 */

import { useEffect, useRef, useState } from 'react';

const CHARS_PER_TICK = 3;

export default function Typewriter({ text, animate = true }: { text: string; animate?: boolean }) {
  const [shown, setShown] = useState(animate ? '' : text);
  const frameRef = useRef<number | null>(null);

  useEffect(() => {
    if (!animate) {
      setShown(text);
      return;
    }
    // 已展示的部分不回退（`text` 变化时为增量追加）
    let cursor = shown.length > text.length ? 0 : shown.length;
    const tick = () => {
      cursor = Math.min(cursor + CHARS_PER_TICK, text.length);
      setShown(text.slice(0, cursor));
      if (cursor < text.length) {
        frameRef.current = window.setTimeout(tick, 24);
      }
    };
    if (cursor < text.length) {
      frameRef.current = window.setTimeout(tick, 24);
    }
    return () => {
      if (frameRef.current) window.clearTimeout(frameRef.current);
    };
    // 仅在文本变化时重启推进
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text, animate]);

  return (
    <div className="whitespace-pre-wrap text-body leading-5">
      {shown}
      {animate && shown.length < text.length ? (
        <span className="zd-pulse" style={{ color: 'var(--c-primary)' }}>
          ▌
        </span>
      ) : null}
    </div>
  );
}
