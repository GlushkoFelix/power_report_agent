/**
 * 页面标题条（v1.19.12 统一）。
 *
 * **为什么收敛成一个组件**：个人中心 / 报表管理 / 评测中心此前各写一份
 * `flex items-center gap-3` + `Typography.Title` + 小灰字，标题的行高是 `20px × 1.4 = 28px`，
 * 副标题只有 `12px` 且没有显式行高 —— 两者**基线不同**，看起来就是"没对齐"（用户实测反馈）。
 * 现在图标 / 标题 / 副标题统一 **28px 行高**（`h-7 items-center` + `!leading-7`），任何字号组合都齐。
 *
 * **「实现注脚」的处理**：像"数据来源 UCI #235""温度固定 0""判分与归因由后端自动完成"这类
 * **开发期说明**不该出现在最终上线界面（用户实测反馈），这里从页头撤下、收进标题右侧的 ⓘ 悬浮提示
 * （`note` 参数）——界面干净了，交付评审需要的口径信息也没丢。
 */

import { Tooltip, Typography } from 'antd';
import { InfoCircleOutlined } from '@ant-design/icons';

interface Props {
  icon: React.ReactNode;
  title: string;
  /** 面向用户的简短说明：只写"用户需要知道的操作信息"，不写实现细节 */
  subtitle?: string;
  /** 实现细节 / 交付说明：悬浮可见（不入正文，保持上线界面干净） */
  note?: string;
  /** 右侧操作区（按钮等） */
  extra?: React.ReactNode;
}

export default function PageHeader({ icon, title, subtitle, note, extra }: Props) {
  return (
    <div className="mb-4 flex h-7 items-center gap-3">
      <span
        className="flex h-7 items-center"
        style={{ fontSize: 18, color: 'var(--c-primary)' }}
      >
        {icon}
      </span>
      {/* `!mt-0` 不能少：项目没有引入 Tailwind preflight，`h4` 会保留浏览器默认的
          `margin-block-start: 1.33em`（18px 字号下 ≈ 23.94px）——在 28px 高的 flex 行里，
          `items-center` 居中的是"含外边距的盒子"，标题因此被顶下去 12px 并溢出容器底
          （实测：图标/副标题 top=80、标题 top=92；置 0 后三者对齐） */}
      <Typography.Title level={4} className="!mb-0 !mt-0 !text-[18px] !leading-7">
        {title}
      </Typography.Title>
      {subtitle ? (
        <span className="text-[12px] leading-7 text-text-sub">{subtitle}</span>
      ) : null}
      {note ? (
        <Tooltip title={note}>
          <InfoCircleOutlined
            className="cursor-help text-text-sub"
            style={{ fontSize: 12 }}
          />
        </Tooltip>
      ) : null}
      {extra ? <div className="ml-auto flex items-center gap-2">{extra}</div> : null}
    </div>
  );
}
