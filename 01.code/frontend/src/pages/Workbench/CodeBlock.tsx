/**
 * SQL / pandas 代码折叠块（UIUX 5.1 区域 5、8.1）。
 *
 * 三条契约：
 * 1. 默认**只显示一行摘要**，展开后按路径标签（SQL / pandas）着色；
 * 2. `code` 事件里的 SQL 是**已过守卫并补过 LIMIT 的最终执行语句**，前端原样展示、不二次改写；
 * 3. 重写/换路会出现多次 `code` 事件 → 传进来的已是最后一次（历史保留在 `codeHistory`）。
 */

import { useState } from 'react';
import { Button, Tag, Tooltip } from 'antd';
import { CheckOutlined, CopyOutlined, DownOutlined, RightOutlined } from '@ant-design/icons';

import type { CodePayload } from '@/api/types';

export default function CodeBlock({
  code,
  onOpenSchema,
}: {
  code: CodePayload;
  onOpenSchema?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);

  const text = (code.route === 'pandas' ? code.code : code.sql) ?? '';
  const isSql = code.route !== 'pandas';

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      /* 剪贴板不可用时静默（用户仍可手动选择） */
    }
  };

  return (
    <div
      className="mt-3 rounded-input border text-table"
      style={{ borderColor: 'var(--c-line-soft)', background: 'var(--c-bg-weak2)' }}
    >
      <div className="flex items-center gap-2 px-3 py-1.5">
        <Button
          type="text"
          size="small"
          onClick={() => setOpen((value) => !value)}
          icon={open ? <DownOutlined /> : <RightOutlined />}
          className="!px-0"
        >
          <span className="text-text-sub">
            {isSql ? '已执行的 SQL' : '已执行的 pandas 代码'}
          </span>
        </Button>
        <Tag color={isSql ? 'blue' : 'purple'} style={{ marginInlineEnd: 0 }}>
          {isSql ? 'SQL' : 'pandas'}
        </Tag>
        <Tooltip title="已通过只读白名单校验（守卫 / AST 预检）">
          <Tag icon={<CheckOutlined />} color="green" style={{ marginInlineEnd: 0 }}>
            只读校验通过
          </Tag>
        </Tooltip>
        <div className="ml-auto flex items-center gap-1">
          {onOpenSchema ? (
            <Button type="link" size="small" onClick={onOpenSchema} className="!px-1">
              查看字段字典
            </Button>
          ) : null}
          <Button
            type="text"
            size="small"
            icon={copied ? <CheckOutlined /> : <CopyOutlined />}
            onClick={() => void copy()}
          >
            {copied ? '已复制' : '复制'}
          </Button>
        </div>
      </div>
      {open ? (
        <pre
          className="zd-scroll m-0 max-h-56 overflow-auto border-t px-3 py-2 font-mono text-[11px] leading-5"
          style={{ borderColor: 'var(--c-line-soft)', color: 'var(--c-text-main)' }}
        >
          {text}
        </pre>
      ) : null}
    </div>
  );
}
