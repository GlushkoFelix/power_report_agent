/**
 * 登录 / 注册（UIUX 5.2）。
 *
 * 规则落地：
 * - 注册成功**即自动登录**（后端直接返回 token），与登录同一套后续流程；
 * - 登录失败内联提示「连续失败 N 次将锁定」，账号锁定按 `data.locked_seconds` 倒计时；
 * - 成功后跳 `/`；URL 带 `next` 则跳回原页（401 拦截时写入）；
 * - 「保留用户输入」：401 场景下的未发送输入由输入区写入草稿，登录后在这里回填提示。
 */

import { useEffect, useMemo, useState } from 'react';
import { Alert, Button, Card, Form, Input, Segmented, Typography } from 'antd';
import { LockOutlined, ThunderboltFilled, UserOutlined } from '@ant-design/icons';
import { useNavigate, useSearchParams } from 'react-router-dom';

import { ApiError } from '@/api/client';
import { peekDraft } from '@/api/token';
import { useAuthStore } from '@/stores/auth';
import { toast } from '@/utils/toast';

type Mode = 'login' | 'register';

interface FormValues {
  username: string;
  password: string;
  confirm?: string;
  display_name?: string;
}

const CAPABILITIES = [
  '自然语言问数：一句提问得到 SQL、结果与解读',
  '双工具执行：SQL 直查 + pandas 沙箱（只读、超时强杀）',
  '结果可视化：图表与表格双向联动，口径全程可见',
];

export default function LoginPage() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const login = useAuthStore((state) => state.login);
  const register = useAuthStore((state) => state.register);
  const status = useAuthStore((state) => state.status);
  const loggedOutByUser = useAuthStore((state) => state.loggedOutByUser);

  const [mode, setMode] = useState<Mode>('login');
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<{ message: string; hint?: string } | null>(null);
  const [lockedSeconds, setLockedSeconds] = useState(0);
  const [form] = Form.useForm<FormValues>();

  /**
   * 登录后去哪：
   * - **主动退出** 过来的 → 一律回首页（否则会被 `?next=` 带回退出前那一页，用户反馈过）；
   * - **登录态失效**（401）过来的 → 回原页，不丢用户正在做的事。
   */
  const next = useMemo(
    () => (loggedOutByUser ? '/' : (params.get('next') ?? '/')),
    [loggedOutByUser, params],
  );
  // 只读草稿：登录页只提示"输入已保留"，真正的回填由输入区消费（避免两边都消费）
  const draft = useMemo(() => peekDraft(), []);

  // 已登录用户直接跳走（避免回退到登录页又看到表单）
  useEffect(() => {
    if (status === 'authenticated') {
      navigate(next, { replace: true });
    }
  }, [status, next, navigate]);

  // 账号锁定倒计时（E_AUTH_LOCKED 的 data.locked_seconds）
  useEffect(() => {
    if (lockedSeconds <= 0) return;
    const timer = window.setInterval(() => {
      setLockedSeconds((value) => (value <= 1 ? 0 : value - 1));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [lockedSeconds]);

  const handleSubmit = async (values: FormValues) => {
    setSubmitting(true);
    setFormError(null);
    try {
      if (mode === 'login') {
        await login({ username: values.username, password: values.password });
        toast.success('登录成功');
      } else {
        await register({
          username: values.username,
          password: values.password,
          display_name: values.display_name || null,
        });
        toast.success('注册成功，已自动登录');
      }
      navigate(next, { replace: true });
    } catch (error) {
      if (error instanceof ApiError) {
        if (error.code === 'E_AUTH_LOCKED') {
          const seconds = Number((error.data?.locked_seconds as number | undefined) ?? 300);
          setLockedSeconds(seconds);
        }
        setFormError({ message: error.message, hint: error.hint });
      } else {
        setFormError({ message: '登录失败，请稍后重试' });
      }
    } finally {
      setSubmitting(false);
    }
  };

  const switchMode = (value: Mode) => {
    setMode(value);
    setFormError(null);
    form.resetFields(['password', 'confirm']);
  };

  return (
    // 登录页在外壳之外，文档层已锁死滚动 → 自己当滚动容器（矮窗口下仍可滚到表单）
    <div className="zd-scroll flex h-screen">
      {/* 左栏品牌区（<1024px 隐藏，UIUX 10.2） */}
      <div
        className="hidden w-[45%] min-w-[420px] flex-col justify-between p-12 lg:flex"
        style={{
          background: 'linear-gradient(160deg, var(--c-primary-grad-from), var(--c-primary-grad-to))',
          color: '#fff',
        }}
      >
        <div>
          <div className="flex items-center gap-3">
            <ThunderboltFilled style={{ fontSize: 28 }} />
            <span className="text-[28px] font-semibold leading-none">智电析报</span>
          </div>
          <p className="mt-3 text-[13px] opacity-90">
            基于 LLM 的能源电力报表智能分析 Agent
          </p>
          <ul className="mt-10 space-y-3 text-[13px] leading-6 opacity-95">
            {CAPABILITIES.map((item) => (
              <li key={item} className="flex gap-2">
                <span>·</span>
                <span>{item}</span>
              </li>
            ))}
          </ul>
        </div>
        <div className="text-[11px] opacity-80">
          数据来源：UCI Machine Learning Repository #235 · 结论仅供参考，不构成计量依据
        </div>
      </div>

      {/* 右栏表单卡片 */}
      <div
        className="flex flex-1 items-center justify-center p-6"
        style={{ background: 'var(--c-bg-page)' }}
      >
        <Card
          className="zd-card w-full max-w-[420px]"
          styles={{ body: { padding: 28 } }}
          title={null}
        >
          <Segmented<Mode>
            className="mb-5 w-full"
            block
            value={mode}
            onChange={switchMode}
            options={[
              { label: '登录', value: 'login' },
              { label: '注册', value: 'register' },
            ]}
          />

          {draft?.text ? (
            <Alert
              className="mb-4"
              type="info"
              showIcon
              message="上次未发送的输入已保留"
              description="登录后可在输入框中继续发送。"
            />
          ) : null}

          {formError ? (
            <Alert
              className="mb-4"
              type="error"
              showIcon
              message={formError.message}
              description={formError.hint}
            />
          ) : null}

          {lockedSeconds > 0 ? (
            <Alert
              className="mb-4"
              type="warning"
              showIcon
              message={`账号已锁定，请在 ${Math.ceil(lockedSeconds / 60)} 分钟后重试（剩余 ${lockedSeconds}s）`}
            />
          ) : null}

          <Form<FormValues> form={form} layout="vertical" requiredMark={false} onFinish={handleSubmit}>
            <Form.Item
              name="username"
              label="用户名"
              rules={[
                { required: true, message: '请输入用户名' },
                { pattern: /^[A-Za-z0-9_]{4,20}$/, message: '4–20 位字母、数字或下划线' },
              ]}
            >
              <Input prefix={<UserOutlined />} placeholder="4–20 位字母/数字/下划线" allowClear />
            </Form.Item>

            {mode === 'register' ? (
              <Form.Item name="display_name" label="显示名（可选）">
                <Input placeholder="用于界面展示，如：张工" maxLength={32} allowClear />
              </Form.Item>
            ) : null}

            <Form.Item
              name="password"
              label="密码"
              rules={[
                { required: true, message: '请输入密码' },
                { min: 8, max: 64, message: '密码长度 8–64 位' },
                {
                  validator: (_rule, value: string) =>
                    !value ||
                    (/[A-Za-z]/.test(value) && /\d/.test(value))
                      ? Promise.resolve()
                      : Promise.reject(new Error('密码需同时包含字母与数字')),
                },
              ]}
            >
              <Input.Password prefix={<LockOutlined />} placeholder="≥8 位，需含字母与数字" />
            </Form.Item>

            {mode === 'register' ? (
              <Form.Item
                name="confirm"
                label="确认密码"
                dependencies={['password']}
                rules={[
                  { required: true, message: '请再次输入密码' },
                  ({ getFieldValue }) => ({
                    validator: (_rule, value: string) =>
                      !value || value === getFieldValue('password')
                        ? Promise.resolve()
                        : Promise.reject(new Error('两次输入的密码不一致')),
                  }),
                ]}
              >
                <Input.Password prefix={<LockOutlined />} placeholder="再次输入密码" />
              </Form.Item>
            ) : null}

            <Button
              type="primary"
              htmlType="submit"
              block
              size="large"
              loading={submitting}
              disabled={lockedSeconds > 0}
            >
              {mode === 'login' ? '登录' : '注册并登录'}
            </Button>
          </Form>

          <Typography.Paragraph
            type="secondary"
            className="!mb-0 !mt-4 !text-[11px] leading-5"
          >
            {mode === 'login'
              ? '连续失败 5 次将锁定 5 分钟；系统不会提示账号是否存在。'
              : '注册成功即自动登录；密码经 bcrypt 加密存储。'}
          </Typography.Paragraph>
        </Card>

        <p className="absolute bottom-4 text-[11px] text-text-sub">
          免责声明：本系统结论由大模型基于报表数据生成，仅供参考，不作为计量或结算依据。
        </p>
      </div>
    </div>
  );
}
