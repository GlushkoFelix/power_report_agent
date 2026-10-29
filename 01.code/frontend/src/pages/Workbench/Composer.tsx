/**
 * 输入区（UIUX 5.1 区域 6 + 8.3 等待体验 + 11 键盘操作）。
 *
 * 规则：**`Enter` 发送、`Shift + Enter` 换行**（用户反馈后从"Ctrl/⌘+Enter 发送"改成主流对话习惯；
 * `Ctrl/⌘ + Enter` 仍然发送，兼容旧习惯）；输入法组词中（`isComposing`）的 Enter 一律不拦——
 * 那是"选词上屏"，不是发送；
 * 发送后清空并禁用（可点「停止」中断 SSE，已生成内容保留）；
 * 空会话时展示 8 个示例问题，点击即问；
 * 401 场景下未发送的输入会被写入草稿，登录后回填（UIUX 9.4「保留用户输入」）。
 */

import { useEffect, useRef, useState } from 'react';
import { Button, Input, Tooltip } from 'antd';
import { SendOutlined, StopOutlined } from '@ant-design/icons';

import { takeDraft } from '@/api/token';
import { toast } from '@/utils/toast';

export const EXAMPLE_QUESTIONS = [
  '全期总用电量是多少',
  '哪个月用电量最高',
  '峰段用电占比多少',
  '有哪些用电异常日',
  '厨房用电占比',
  '周末与工作日对比',
  '2010年比2009年增长',
  '未计量用电多少',
];

interface Props {
  streaming: boolean;
  showExamples: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
}

export default function Composer({ streaming, showExamples, onSend, onStop }: Props) {
  const [value, setValue] = useState('');
  const inputRef = useRef<HTMLTextAreaElement | null>(null);

  // 登录后回填未发送的输入（401 拦截时写入）
  useEffect(() => {
    const draft = takeDraft();
    if (draft?.text) {
      setValue(draft.text);
      toast.info('已恢复上次未发送的输入');
    }
  }, []);

  const submit = (text?: string) => {
    const content = (text ?? value).trim();
    if (!content || streaming) return;
    onSend(content);
    setValue('');
  };

  return (
    <div className="shrink-0 border-t px-4 py-3" style={{ background: 'var(--c-bg-card)', borderColor: 'var(--c-line)' }}>
      <div className="flex items-end gap-2">
        <Input.TextArea
          ref={inputRef}
          value={value}
          autoSize={{ minRows: 2, maxRows: 6 }}
          maxLength={2000}
          disabled={streaming}
          placeholder='输入问题，例如「2020 年 7 月的总用电量是多少」'
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={(event) => {
            if (event.key !== 'Enter') return;
            // 中文/日文输入法组词中：Enter 是"候选上屏"，既不能发送也不能 preventDefault
            if (event.nativeEvent.isComposing || event.keyCode === 229) return;
            // Shift + Enter → 换行（交回 TextArea 的默认行为）
            if (event.shiftKey) return;
            // Enter（以及 Ctrl/⌘ + Enter）→ 发送
            event.preventDefault();
            submit();
          }}
        />
        {streaming ? (
          <Button danger size="large" icon={<StopOutlined />} onClick={onStop}>
            停止
          </Button>
        ) : (
          <Button type="primary" size="large" icon={<SendOutlined />} onClick={() => submit()}>
            发送
          </Button>
        )}
      </div>

      <div className="mt-1.5 text-[11px] text-text-sub">
        提示：按 Enter 发送，Shift + Enter 换行；系统会继承上方「当前口径」。
      </div>

      {showExamples ? (
        <div className="mt-3">
          <div className="mb-2 text-[11px] text-text-sub">试试这样问（点击即问）</div>
          <div className="grid grid-cols-2 gap-2 xl:grid-cols-4">
            {EXAMPLE_QUESTIONS.map((question) => (
              <Tooltip key={question} title={question}>
                <Button
                  size="small"
                  className="!justify-start"
                  block
                  disabled={streaming}
                  onClick={() => submit(question)}
                >
                  <span className="truncate">{question}</span>
                </Button>
              </Tooltip>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}
