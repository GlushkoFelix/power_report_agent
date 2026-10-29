/**
 * 评测中心页（UIUX 5.4）。
 *
 * 页面结构：① 发起评测 ② 历史批次（**自适应高度**列表）③ 批次详情在**浮窗**里打开。
 *
 * **v1.19.12 三处调整（用户实测反馈）**：
 * 1. **详情改浮窗**：此前详情固定在页面底部——历史批次一多，点「查看」后要翻到页面最底部才看得到，
 *    第一眼往往以为"没反应"。现在改为**浮窗（Modal 92% 宽）**，点开即在视线内；
 * 2. **删掉底部板块**：详情只有浮窗一处入口，页面上不再重复渲染整块详情；
 * 3. **历史批次自适应高度**：列表按视口撑开、内部滚动，不再把页面撑长。
 *
 * **进度如实降级（决策 D4）**：批次是服务端异步任务，运行期只有 `status` 与 `question_total`，
 * 逐题结果与指标要等结束才落库 → 这里只显示"运行中 · 共 N 题 + 已耗时"，
 * **不推算"已完成 x/N"、不显示假的实时正确率**；「停止跟踪」= 停止前端轮询（服务端仍会跑完）。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Card, Modal, Space, Tag, Typography } from 'antd';
import { BarChartOutlined, ReloadOutlined, StopOutlined } from '@ant-design/icons';

import { ApiError } from '@/api/client';
import { EVAL_STATUS_LABELS, evalApi } from '@/api/eval';
import type { EvalRunDetail, EvalRunSummary } from '@/api/types';
import PageHeader from '@/layouts/PageHeader';
import { formatDateTime } from '@/utils/format';
import { toast } from '@/utils/toast';
import MetricsPanel from './MetricsPanel';
import RunsList from './RunsList';
import RunQuestionsTable from './RunQuestionsTable';
import StartPanel from './StartPanel';

const POLL_INTERVAL_MS = 2500;

export default function EvalPage() {
  const [runs, setRuns] = useState<EvalRunSummary[]>([]);
  const [runsLoading, setRunsLoading] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<EvalRunDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [tracking, setTracking] = useState(false);
  const trackingRef = useRef(true);
  const [elapsed, setElapsed] = useState(0);
  const startedAtRef = useRef<number | null>(null);

  const loadRuns = useCallback(async (selectLatest = false) => {
    setRunsLoading(true);
    try {
      const data = await evalApi.runs({ page: 1, page_size: 20 });
      setRuns(data.items);
      if (selectLatest && data.items.length) {
        setSelected(data.items[0].run_code);
      }
    } catch (error) {
      if (error instanceof ApiError) toast.error(error.message, error.requestId);
    } finally {
      setRunsLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadRuns();
  }, [loadRuns]);

  // 批次详情 + 轮询（运行中才继续；结束时自动停并刷新列表指标）
  useEffect(() => {
    if (!selected) return;
    let alive = true;
    trackingRef.current = true;
    setTracking(true);
    setDetail(null);
    startedAtRef.current = Date.now();

    const fetchOnce = async (): Promise<EvalRunDetail | null> => {
      try {
        const data = await evalApi.run(selected, true);
        if (alive) setDetail(data);
        return data;
      } catch (error) {
        if (alive && error instanceof ApiError) toast.error(error.message, error.requestId);
        return null;
      }
    };

    setDetailLoading(true);
    void fetchOnce().finally(() => {
      if (alive) setDetailLoading(false);
    });

    const timer = window.setInterval(async () => {
      if (!trackingRef.current) return;
      const data = await fetchOnce();
      if (!data) return;
      if (data.status === 'finished' || data.status === 'failed') {
        trackingRef.current = false;
        setTracking(false);
        window.clearInterval(timer);
        void loadRuns();
      }
    }, POLL_INTERVAL_MS);

    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [selected, loadRuns]);

  // 运行中的已耗时（每 0.5s 刷新一次，纯前端计时）
  useEffect(() => {
    if (!tracking || !selected) return;
    const timer = window.setInterval(() => {
      if (startedAtRef.current) {
        setElapsed(Math.floor((Date.now() - startedAtRef.current) / 1000));
      }
    }, 500);
    return () => window.clearInterval(timer);
  }, [tracking, selected]);

  /** 上一批次（用于「与上批次差值」，后端没有基线接口，前端自己比） */
  const previous = useMemo(
    () =>
      runs.find(
        (run) => run.run_code !== selected && run.status === 'finished' && run.exec_correct_rate !== null,
      ) ?? null,
    [runs, selected],
  );

  const running = detail?.status === 'pending' || detail?.status === 'running';
  const questions = detail?.questions ?? [];
  const finished = detail?.status === 'finished';

  const closeModal = () => {
    trackingRef.current = false;
    setTracking(false);
    setSelected(null);
  };

  const exportReport = async (kind: 'pdf' | 'md') => {
    if (!selected) return;
    try {
      if (kind === 'pdf') {
        await evalApi.downloadReportPdf(selected);
        toast.success('PDF 报告已开始下载');
      } else {
        await evalApi.downloadReport(selected);
        toast.success('Markdown 报告已开始下载');
      }
    } catch {
      toast.error('报告导出失败：批次未完成或报告缺失');
    }
  };

  return (
    <div className="mx-auto flex max-w-[1440px] flex-col gap-4 p-6">
      <PageHeader
        icon={<BarChartOutlined />}
        title="评测中心"
        subtitle="120 题题库 · 统一口径自动判分"
        note="题库含 6 类题型与口径陷阱题；温度固定 0 以保证同题可复现；判分与归因由后端自动完成（结果集逐值比对，列序/行序无关，允许展示级舍入）。"
        extra={
          <Button
            icon={<ReloadOutlined />}
            onClick={() => {
              void loadRuns();
              if (selected) void evalApi.run(selected, true).then(setDetail).catch(() => undefined);
            }}
          >
            刷新
          </Button>
        }
      />

      <StartPanel
        onStarted={(runCode) => {
          setSelected(runCode);
          void loadRuns();
        }}
      />

      <Card
        className="zd-card"
        size="small"
        title="历史批次"
        extra={
          <span className="text-[11px] text-text-sub">
            共 {runs.length} 个批次 · 点行或「查看」在浮窗中打开详情
          </span>
        }
      >
        <RunsList
          runs={runs}
          activeCode={selected}
          loading={runsLoading}
          onSelect={(runCode) => setSelected(runCode)}
        />
      </Card>

      {/* 批次详情浮窗（v1.19.12）：详情只在这里打开，页面底部不再挂一整块详情 */}
      <Modal
        open={Boolean(selected)}
        onCancel={closeModal}
        width="92%"
        style={{ top: 24 }}
        destroyOnClose={false}
        title={
          <span className="flex flex-wrap items-center gap-2">
            <span>批次详情</span>
            <Tag>{selected}</Tag>
            <Tag
              color={
                detail?.status === 'finished'
                  ? 'green'
                  : detail?.status === 'failed'
                    ? 'red'
                    : 'processing'
              }
            >
              {EVAL_STATUS_LABELS[detail?.status ?? 'pending'] ?? detail?.status}
            </Tag>
            {detail?.impl === 'selfcheck' ? (
              <Tag color="orange">自检回放（不代表模型能力）</Tag>
            ) : null}
          </span>
        }
        footer={
          <div className="flex items-center gap-2">
            {running && tracking ? (
              <Button
                size="small"
                icon={<StopOutlined />}
                onClick={() => {
                  trackingRef.current = false;
                  setTracking(false);
                  toast.info('已停止跟踪（服务端仍会跑完，稍后回来刷新即可看到结果）');
                }}
              >
                停止跟踪
              </Button>
            ) : null}
            <div className="ml-auto flex items-center gap-2">
              <Button
                size="small"
                disabled={!finished}
                onClick={() => void exportReport('pdf')}
              >
                导出 PDF
              </Button>
              <Button
                size="small"
                disabled={!finished}
                onClick={() => void exportReport('md')}
              >
                导出 Markdown
              </Button>
              <Button type="primary" size="small" onClick={closeModal}>
                关闭
              </Button>
            </div>
          </div>
        }
        styles={{ body: { maxHeight: 'calc(100vh - 220px)', overflowY: 'auto' } }}
      >
        {running ? (
          <Alert
            className="mb-3"
            type="info"
            showIcon
            message={`${detail?.status === 'pending' ? '排队中' : '运行中'} · 共 ${detail?.question_total ?? 0} 题 · 已耗时 ${elapsed}s`}
            description={
              tracking
                ? '逐题结果与指标会在批次结束后一次性下发（后端未提供逐题进度接口），因此这里不显示"已完成 x/N"。'
                : '已停止跟踪，服务端仍在执行；完成后点「刷新」即可看到指标与逐题明细。'
            }
          />
        ) : null}

        {detail?.status === 'failed' ? (
          <Alert
            className="mb-3"
            type="error"
            showIcon
            message="批次执行失败"
            description={detail.error_message ?? '未提供失败原因'}
          />
        ) : null}

        <div className="mb-3 flex flex-wrap gap-x-6 gap-y-1 text-[11px] text-text-sub">
          <span>模型：{detail?.model_name ?? '—'}</span>
          <span>温度：{detail?.temperature ?? 0}</span>
          <span>提示词版本：{detail?.prompt_version ?? '—'}</span>
          <span>口径版本：{detail?.schema_version ?? '—'}</span>
          <span>创建：{formatDateTime(detail?.created_at)}</span>
          <span>完成：{formatDateTime(detail?.finished_at)}</span>
        </div>

        {detail ? <MetricsPanel detail={detail} previous={previous} /> : null}

        {questions.length ? (
          <div className="mt-4">
            <RunQuestionsTable questions={questions} />
          </div>
        ) : null}

        {detailLoading && !detail ? (
          <div className="py-6 text-center text-text-sub">正在加载批次详情…</div>
        ) : null}

        {!detailLoading && !detail && selected ? (
          <Alert type="warning" showIcon message="未加载到批次详情" description="可点「刷新」重试。" />
        ) : null}

        <Space className="mt-4">
          <Typography.Text type="secondary" className="!text-[11px]">
            提示：报告为 A4 横向 PDF，含指标卡、分类正确率、bad case 归因与逐题「生成 SQL vs 标准 SQL」对照。
          </Typography.Text>
        </Space>
      </Modal>
    </div>
  );
}
