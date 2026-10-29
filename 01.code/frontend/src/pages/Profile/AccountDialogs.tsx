/**
 * 个人中心的四个弹窗（PRD F6.4）：修改密码 / 编辑资料 / 清空历史记录 / 删除账号。
 *
 * 三条与"不伪造"相关的取舍（沿用本项目一贯原则）：
 * 1. **危险操作要够贵**：清空历史必须先选"清到什么程度"并看到后果；删除账号要**密码 + 输入用户名**
 *    双确认（后端同样校验，前端只是提前拦截误触）；
 * 2. **结果如实回显**：清掉多少条记录、保留多少条安全日志，一律用后端返回的数字，不写"已全部清空"
 *    这种模糊说法；
 * 3. **密码不进任何"看得见"的地方**：只走请求体，不进 URL、不进提示文案、不落 localStorage。
 */

import { useEffect, useState } from 'react';
import {
  Alert,
  Button,
  Checkbox,
  Descriptions,
  Divider,
  Form,
  Input,
  Modal,
  Radio,
  Space,
  Tag,
  Typography,
} from 'antd';

import { ApiError } from '@/api/client';
import { authApi, type ClearScope } from '@/api/auth';
import type { User } from '@/api/types';
import { useAuthStore } from '@/stores/auth';
import { toast } from '@/utils/toast';

/** 把接口错误统一渲染成"原因 + 下一步"（UIUX 9.4），返回给调用方复用 */
function errorText(error: unknown): { message: string; hint?: string } {
  if (error instanceof ApiError) {
    return { message: error.message, hint: error.hint };
  }
  return { message: '操作失败，请稍后重试' };
}

const PASSWORD_RULE = /^(?=.*[A-Za-z])(?=.*\d).{8,64}$/;

// ── ① 修改密码 ──────────────────────────────────────────────────────────────

export function PasswordDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [form] = Form.useForm<{ old_password: string; new_password: string; confirm: string }>();
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<{ message: string; hint?: string } | null>(null);

  useEffect(() => {
    if (open) {
      form.resetFields();
      setError(null);
    }
  }, [open, form]);

  const submit = async () => {
    const values = await form.validateFields();
    setSubmitting(true);
    setError(null);
    try {
      await authApi.changePassword({
        old_password: values.old_password,
        new_password: values.new_password,
      });
      toast.success('密码已修改（其它设备上的登录不会自动退出）');
      onClose();
    } catch (exception) {
      setError(errorText(exception));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      title="修改密码"
      open={open}
      onCancel={onClose}
      onOk={() => void submit()}
      okText="确认修改"
      cancelText="取消"
      confirmLoading={submitting}
      destroyOnClose
      width={460}
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
      <Form form={form} layout="vertical" requiredMark={false} preserve={false}>
        <Form.Item
          name="old_password"
          label="当前密码"
          rules={[{ required: true, message: '请输入当前密码' }]}
        >
          <Input.Password autoComplete="current-password" placeholder="用于确认身份" />
        </Form.Item>
        <Form.Item
          name="new_password"
          label="新密码"
          rules={[
            { required: true, message: '请输入新密码' },
            {
              validator: (_rule, value: string) =>
                !value || PASSWORD_RULE.test(value)
                  ? Promise.resolve()
                  : Promise.reject(new Error('至少 8 位，且需同时包含字母与数字')),
            },
          ]}
        >
          <Input.Password autoComplete="new-password" placeholder="≥8 位，含字母与数字" />
        </Form.Item>
        <Form.Item
          name="confirm"
          label="确认新密码"
          dependencies={['new_password']}
          rules={[
            { required: true, message: '请再次输入新密码' },
            ({ getFieldValue }) => ({
              validator: (_rule, value: string) =>
                !value || value === getFieldValue('new_password')
                  ? Promise.resolve()
                  : Promise.reject(new Error('两次输入的新密码不一致')),
            }),
          ]}
        >
          <Input.Password autoComplete="new-password" />
        </Form.Item>
      </Form>
      <Typography.Text type="secondary" className="!text-[11px]">
        说明：本项目使用无状态 JWT，改密后**不回收**已签发的登录凭证——其它设备上的登录不会自动退出。
      </Typography.Text>
    </Modal>
  );
}

// ── ② 编辑资料 ──────────────────────────────────────────────────────────────

export function ProfileDialog({
  open,
  user,
  onClose,
}: {
  open: boolean;
  user: User | null;
  onClose: () => void;
}) {
  const setUser = useAuthStore((state) => state.setUser);
  const [form] = Form.useForm<{ display_name?: string; email?: string }>();
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<{ message: string; hint?: string } | null>(null);

  useEffect(() => {
    if (open) {
      form.setFieldsValue({
        display_name: user?.display_name ?? '',
        email: user?.email ?? '',
      });
      setError(null);
    }
  }, [open, user, form]);

  const submit = async () => {
    const values = await form.validateFields();
    const name = (values.display_name ?? '').trim();
    const email = (values.email ?? '').trim();
    // **只提交改动过的字段**：后端对未提交字段保持原值（`model_fields_set` 语义），
    // 显式传空串等于"清空该字段"
    const payload: { display_name?: string | null; email?: string | null } = {};
    if (name !== (user?.display_name ?? '')) payload.display_name = name || null;
    if (email !== (user?.email ?? '')) payload.email = email || null;
    if (Object.keys(payload).length === 0) {
      toast.info('没有需要保存的改动');
      onClose();
      return;
    }

    setSubmitting(true);
    setError(null);
    try {
      const updated = await authApi.updateProfile(payload);
      setUser(updated);
      toast.success('资料已更新');
      onClose();
    } catch (exception) {
      setError(errorText(exception));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      title="编辑资料"
      open={open}
      onCancel={onClose}
      onOk={() => void submit()}
      okText="保存"
      cancelText="取消"
      confirmLoading={submitting}
      destroyOnClose
      width={460}
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
      <Form form={form} layout="vertical" requiredMark={false} preserve={false}>
        <Form.Item label="用户名">
          <Input value={user?.username ?? ''} disabled />
        </Form.Item>
        <Form.Item
          name="display_name"
          label="显示名"
          rules={[{ max: 32, message: '显示名不超过 32 个字符' }]}
        >
          <Input placeholder="留空则回退为用户名" allowClear />
        </Form.Item>
        <Form.Item
          name="email"
          label="邮箱"
          rules={[
            {
              validator: (_rule, value: string) =>
                !value || /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(value)
                  ? Promise.resolve()
                  : Promise.reject(new Error('邮箱格式不正确（示例：name@example.com）')),
            },
          ]}
        >
          <Input placeholder="可选" allowClear />
        </Form.Item>
      </Form>
    </Modal>
  );
}

// ── ③ 清空历史记录 ──────────────────────────────────────────────────────────

export function ClearRecordsDialog({
  open,
  onClose,
  onCleared,
}: {
  open: boolean;
  onClose: () => void;
  onCleared: () => void;
}) {
  const [scope, setScope] = useState<ClearScope>('records');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<{ message: string; hint?: string } | null>(null);

  useEffect(() => {
    if (open) {
      setScope('records');
      setError(null);
    }
  }, [open]);

  const submit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const result = await authApi.clearRecords(scope);
      const parts = [`已删除 ${result.deleted_records} 条分析记录`];
      if (result.deleted_conversations) parts.push(`${result.deleted_conversations} 个会话`);
      if (result.deleted_messages) parts.push(`${result.deleted_messages} 条对话消息`);
      if (result.deleted_llm_calls) parts.push(`连带 ${result.deleted_llm_calls} 条模型调用流水`);
      toast.success(parts.join('、'));
      onCleared();
      onClose();
    } catch (exception) {
      setError(errorText(exception));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      title="清空历史记录"
      open={open}
      onCancel={onClose}
      onOk={() => void submit()}
      okText="确认清空"
      cancelText="取消"
      okButtonProps={{ danger: true }}
      confirmLoading={submitting}
      destroyOnClose
      width={520}
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
      <Radio.Group
        value={scope}
        onChange={(event) => setScope(event.target.value as ClearScope)}
        className="flex flex-col gap-2"
      >
        <Radio value="records">
          <span className="font-semibold">只清分析记录</span>
          <div className="text-[11px] text-text-sub">
            删除每轮问答生成的 SQL/代码、结果表、图表与解读；**会话与对话文本保留**（与逐条删除一致）
          </div>
        </Radio>
        <Radio value="all">
          <span className="font-semibold">清空全部会话与对话历史</span>
          <div className="text-[11px] text-text-sub">
            连同会话列表、对话消息与置顶状态一起清空（评测批次记录也一并清除）
          </div>
        </Radio>
      </Radio.Group>
      <Divider className="!my-3" />
      <Alert
        type="warning"
        showIcon
        message="此操作不可撤销"
        description={
          <span className="text-[11px]">
            被删除的记录与模型调用流水<b>无法恢复</b>；安全审计日志按合规要求保留（用于安全追溯）。
          </span>
        }
      />
    </Modal>
  );
}

// ── ④ 删除账号 ──────────────────────────────────────────────────────────────

export function DeleteAccountDialog({
  open,
  user,
  onClose,
}: {
  open: boolean;
  user: User | null;
  onClose: () => void;
}) {
  const logout = useAuthStore((state) => state.logout);
  const [form] = Form.useForm<{ password: string; confirm_username: string }>();
  const [submitting, setSubmitting] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const [error, setError] = useState<{ message: string; hint?: string } | null>(null);

  useEffect(() => {
    if (open) {
      form.resetFields();
      setAcknowledged(false);
      setError(null);
    }
  }, [open, form]);

  const submit = async () => {
    const values = await form.validateFields();
    setSubmitting(true);
    setError(null);
    try {
      const result = await authApi.deleteAccount({
        password: values.password,
        confirm_username: values.confirm_username.trim(),
      });
      toast.success(
        `账号 ${result.username} 已删除：清理 ${result.deleted_reports} 张上传报表、` +
          `${result.deleted_conversations} 个会话、${result.deleted_records} 条分析记录；` +
          `安全日志保留 ${result.kept_audit_logs} 条`,
      );
      onClose();
      // 账号已不存在 → 前端登出（token 清除 + 业务状态复位），路由守卫随后跳登录页
      await logout();
    } catch (exception) {
      setError(errorText(exception));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      title={<span style={{ color: 'var(--c-anomaly)' }}>删除账号（不可撤销）</span>}
      open={open}
      onCancel={onClose}
      onOk={() => void submit()}
      okText="永久删除账号"
      cancelText="取消"
      okButtonProps={{ danger: true, disabled: !acknowledged }}
      confirmLoading={submitting}
      destroyOnClose
      width={520}
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

      <Alert
        type="error"
        showIcon
        className="mb-3"
        message="删除后无法恢复"
        description={
          <span className="text-[11px]">
            将删除：你上传的<b>全部报表</b>（含物理数据表）、全部会话与对话历史、分析记录、
            模型调用流水、评测批次；<b>安全审计日志按合规保留</b>（归属字段置空）。
          </span>
        }
      />

      <Descriptions size="small" column={1} className="mb-3">
        <Descriptions.Item label="待删除账号">
          <Tag color="red" style={{ marginInlineEnd: 0 }}>
            {user?.username ?? '—'}
          </Tag>
        </Descriptions.Item>
      </Descriptions>

      <Form form={form} layout="vertical" requiredMark={false} preserve={false}>
        <Form.Item
          name="password"
          label="当前密码"
          rules={[{ required: true, message: '请输入当前密码' }]}
        >
          <Input.Password autoComplete="current-password" placeholder="用于确认身份" />
        </Form.Item>
        <Form.Item
          name="confirm_username"
          label="输入用户名以确认"
          rules={[
            { required: true, message: '请输入用户名' },
            {
              validator: (_rule, value: string) =>
                !value || value.trim() === (user?.username ?? '')
                  ? Promise.resolve()
                  : Promise.reject(new Error('与当前用户名不一致')),
            },
          ]}
        >
          <Input placeholder={user?.username ?? ''} autoComplete="off" />
        </Form.Item>
      </Form>

      <Checkbox
        checked={acknowledged}
        onChange={(event) => setAcknowledged(event.target.checked)}
      >
        <span className="text-[12px]">我已了解上述后果，确认永久删除该账号</span>
      </Checkbox>

      <Space className="mt-3" size={4}>
        <Typography.Text type="secondary" className="!text-[11px]">
          提示：若只想清理数据、保留账号，请用左侧「清空历史记录」。
        </Typography.Text>
      </Space>
    </Modal>
  );
}

/** 危险区两个入口的按钮（放在 `Profile/index.tsx` 里调用，保持危险区视觉一致） */
export function DangerButton({
  children,
  onClick,
  icon,
}: {
  children: React.ReactNode;
  onClick: () => void;
  icon?: React.ReactNode;
}) {
  return (
    <Button size="small" danger icon={icon} onClick={onClick}>
      {children}
    </Button>
  );
}
