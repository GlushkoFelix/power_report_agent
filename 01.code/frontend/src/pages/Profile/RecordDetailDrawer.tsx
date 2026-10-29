/**
 * 分析记录详情抽屉（UIUX 5.5 区域 3 的「查看」）。
 *
 * 复用对话工作台的既有组件（`CodeBlock` / `ResultTable` + `Plotly` 容器），
 * 让"历史记录"与"对话里的那次结果"看到的是同一套东西：
 * 执行语句（SQL / pandas）、结果表、图表、解读与关键数值、模型调用流水。
 *
 * 数据源：`GET /analysis/records/{record_code}`（全量详情，含 `llm_calls`）。
 */

import { useEffect, useState } from 'react';
import { Alert, Descriptions, Drawer, Spin, Table, Tag } from 'antd';

import { ApiError } from '@/api/client';
import { analysisApi } from '@/api/analysis';
import type { LlmCall, RecordDetail } from '@/api/types';
import { useThemeStore } from '@/stores/theme';
import { formatDateTime, formatDuration } from '@/utils/format';
import { toast } from '@/utils/toast';
import CodeBlock from '@/pages/Workbench/CodeBlock';
import ResultChart from '@/pages/Workbench/ResultChart';
import ResultTable from '@/pages/Workbench/ResultTable';

export default function RecordDetailDrawer({
  recordCode,
  open,
  onClose,
}: {
  recordCode: string | null;
  open: boolean;
  onClose: () => void;
}) {
  const mode = useThemeStore((state) => state.mode);
  const [detail, setDetail] = useState<RecordDetail | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setDetail(null);
    if (!open || !recordCode) return;
    let alive = true;
    setLoading(true);
    analysisApi
      .record(recordCode)
      .then((data) => {
        if (alive) setDetail(data);
      })
      .catch((error: unknown) => {
        if (error instanceof ApiError) toast.error(error.message, error.requestId);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [open, recordCode]);

  const figure = detail?.chart ?? null;

  return (
    <Drawer
      title={`记录详情 · ${recordCode ?? ''}`}
      width={880}
      open={open}
      onClose={onClose}
      destroyOnClose
    >
      {loading ? (
        <div className="flex justify-center py-10">
          <Spin />
        </div>
      ) : !detail ? (
        <Alert type="warning" showIcon message="未加载到记录详情" />
      ) : (
        <div className="space-y-3">
          {detail.status !== 'success' ? (
            <Alert
              type="error"
              showIcon
              message={`本次未生成结果（${detail.error_code ?? '未知错误'}）`}
              description={detail.error_message ?? undefined}
            />
          ) : null}

          {detail.replayed_from ? (
            <Alert type="info" showIcon message={`该记录由 ${detail.replayed_from} 重跑产生`} />
          ) : null}

          <Descriptions size="small" column={2} bordered>
            <Descriptions.Item label="问题" span={2}>
              {detail.question}
            </Descriptions.Item>
            <Descriptions.Item label="状态">
              <Tag color={detail.status === 'success' ? 'green' : 'red'}>{detail.status}</Tag>
            </Descriptions.Item>
            <Descriptions.Item label="执行路径">{detail.route ?? '—'}</Descriptions.Item>
            <Descriptions.Item label="报表">{detail.report_code ?? '—'}</Descriptions.Item>
            <Descriptions.Item label="耗时">{formatDuration(detail.elapsed_ms)}</Descriptions.Item>
            <Descriptions.Item label="模型">{detail.llm_model ?? '—'}</Descriptions.Item>
            <Descriptions.Item label="tokens">{detail.llm_tokens}</Descriptions.Item>
            <Descriptions.Item label="提示词版本">{detail.prompt_version ?? '—'}</Descriptions.Item>
            <Descriptions.Item label="口径版本">{detail.schema_version ?? '—'}</Descriptions.Item>
            <Descriptions.Item label="重试 / 换路">
              {detail.retries} 次{detail.path_switched ? ' · 已换路' : ''}
            </Descriptions.Item>
            <Descriptions.Item label="创建时间">{formatDateTime(detail.created_at)}</Descriptions.Item>
            {detail.conversation_code ? (
              <Descriptions.Item label="所属会话">{detail.conversation_code}</Descriptions.Item>
            ) : null}
          </Descriptions>

          {detail.sql || detail.code_text ? (
            <CodeBlock
              code={
                detail.route === 'pandas'
                  ? { route: 'pandas', code: detail.code_text ?? '' }
                  : { route: 'sql', sql: detail.sql ?? '' }
              }
            />
          ) : null}

          {figure ? (
            <div className="zd-card p-3">
              <div className="mb-2 font-semibold">图表</div>
              <ResultChart figure={figure} mode={mode} height={280} />
            </div>
          ) : null}

          {detail.result?.columns?.length ? (
            <div className="zd-card p-3">
              <div className="mb-2 font-semibold">结果表（{detail.result.row_count} 行）</div>
              <ResultTable
                table={detail.result}
                focusIndex={null}
                onHoverRow={() => undefined}
              />
            </div>
          ) : null}

          {detail.narrative ? (
            <div className="zd-card p-3">
              <div className="mb-2 font-semibold">文字解读</div>
              <div className="whitespace-pre-wrap text-body">{detail.narrative}</div>
              {detail.anomalies?.length ? (
                <Alert
                  className="mt-2"
                  type="warning"
                  showIcon
                  message="异常提示"
                  description={
                    <ul className="m-0 list-disc pl-4">
                      {detail.anomalies.map((item) => (
                        <li key={item}>{item}</li>
                      ))}
                    </ul>
                  }
                />
              ) : null}
            </div>
          ) : null}

          {detail.llm_calls?.length ? (
            <div className="zd-card p-3">
              <div className="mb-2 font-semibold">模型调用流水</div>
              <Table<LlmCall>
                size="small"
                rowKey={(row) => `${row.stage}-${row.model_name}-${row.elapsed_ms}`}
                pagination={false}
                dataSource={detail.llm_calls}
                columns={[
                  { title: '阶段', dataIndex: 'stage', width: 120 },
                  { title: '模型', dataIndex: 'model_name', width: 160 },
                  { title: 'prompt', dataIndex: 'prompt_tokens', width: 90 },
                  { title: 'completion', dataIndex: 'completion_tokens', width: 100 },
                  { title: 'tokens', dataIndex: 'total_tokens', width: 90 },
                  {
                    title: '耗时',
                    dataIndex: 'elapsed_ms',
                    width: 90,
                    render: (value: number | null) => formatDuration(value),
                  },
                  { title: '状态', dataIndex: 'status', width: 80 },
                ]}
              />
            </div>
          ) : null}
        </div>
      )}
    </Drawer>
  );
}
