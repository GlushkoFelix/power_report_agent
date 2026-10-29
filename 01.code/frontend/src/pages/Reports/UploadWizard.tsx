/**
 * 上传两步向导（UIUX 5.3 区域 1「上传区」+ 12.3 上传入库流程）。
 *
 * 三步形态（决策 D2：**页内步骤条**，与线框图一致）：
 *  ① 选择文件 → ② 预览与质量校验 → ③ 确认入库
 *
 * 三条后端行为的前端落法：
 * - **`is_duplicate=true` 不进预览步骤**：直接 Toast + 回调定位到现有报表（后端按 sha256 复用）；
 * - **长等待**：解析 207 万行约 30s、入库约 20s → 按钮 loading + **已耗时计时器**，
 *   >5s 追加"数据集较大，正在解析…"（UIUX 8.2）；
 * - **放弃上传**：确认前关掉向导会 `DELETE` 该 pending 报表（避免列表里留下半成品，
 *   pending 本就不出现在列表里，删掉是为了不留垃圾）。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Descriptions, Progress, Space, Steps, Table, Tag, Upload } from 'antd';
import { InboxOutlined, ReloadOutlined } from '@ant-design/icons';

import { ApiError } from '@/api/client';
import { reportApi } from '@/api/reports';
import type { ConfirmOut, UploadColumn, UploadOut } from '@/api/types';
import { toast } from '@/utils/toast';
import QualityPanel from './QualityPanel';
import ResultTable from '@/pages/Workbench/ResultTable';

const MAX_SIZE_MB = 200;
const ALLOWED_EXT = ['.csv', '.txt'];

interface Props {
  /** 入库完成（或命中重复）后回调：父级刷新列表并高亮该报表 */
  onFinished: (reportCode: string) => void;
  onOpenSchema: (reportCode: string) => void;
  onActivate: (reportCode: string) => void;
}

export default function UploadWizard({ onFinished, onOpenSchema, onActivate }: Props) {
  const [step, setStep] = useState(0);
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [parsing, setParsing] = useState(false);
  const [result, setResult] = useState<UploadOut | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [confirmed, setConfirmed] = useState<ConfirmOut | null>(null);
  const [error, setError] = useState<{ message: string; hint?: string } | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const startedAt = useRef<number | null>(null);

  // 已耗时计时器：解析与入库都属于长等待，用户必须看得到"还在跑"
  useEffect(() => {
    const running = uploading || parsing || confirming;
    if (!running) return;
    startedAt.current = Date.now();
    setElapsed(0);
    const timer = window.setInterval(() => {
      setElapsed(Math.floor((Date.now() - (startedAt.current ?? Date.now())) / 1000));
    }, 500);
    return () => window.clearInterval(timer);
  }, [uploading, parsing, confirming]);

  const reset = useCallback(() => {
    setStep(0);
    setFile(null);
    setResult(null);
    setConfirmed(null);
    setError(null);
    setProgress(0);
  }, []);

  /** 放弃上传：删除未确认的 pending 报表（后端无物理表，删的只是登记） */
  const abort = useCallback(async () => {
    const code = result?.report_code;
    reset();
    if (!code) return;
    try {
      await reportApi.remove(code);
      toast.info('已取消上传');
    } catch {
      // 取消失败不打扰用户（pending 超 24h 由后端惰性回收）
    }
  }, [result, reset]);

  const startUpload = useCallback(
    async (target: File) => {
      setError(null);
      setUploading(true);
      setParsing(false);
      try {
        const data = await reportApi.upload(target, setProgress);
        // 文件已上传完，剩余时间是服务端解析
        setParsing(true);
        if (data.is_duplicate) {
          toast.info('该文件已上传过，已为您定位到现有报表');
          reset();
          onFinished(data.report_code);
          return;
        }
        setResult(data);
        setStep(1);
      } catch (err) {
        if (err instanceof ApiError) setError({ message: err.message, hint: err.hint });
        else setError({ message: '上传失败，请重试' });
      } finally {
        setUploading(false);
        setParsing(false);
      }
    },
    [onFinished, reset],
  );

  const beforeUpload = (target: File) => {
    const lower = target.name.toLowerCase();
    const ext = lower.slice(lower.lastIndexOf('.'));
    if (!ALLOWED_EXT.includes(ext)) {
      setError({ message: '文件类型不支持', hint: '仅支持 .csv / .txt；Excel 请先另存为 UTF-8 CSV' });
      return Upload.LIST_IGNORE;
    }
    if (target.size > MAX_SIZE_MB * 1024 * 1024) {
      setError({
        message: `文件 ${(target.size / 1024 / 1024).toFixed(1)}MB 超过 ${MAX_SIZE_MB}MB 上限`,
        hint: '建议按月 / 年拆分后再上传',
      });
      return Upload.LIST_IGNORE;
    }
    setFile(target);
    void startUpload(target);
    return false; // 由我们自己发请求（需要带 Bearer 头）
  };

  const confirm = async () => {
    if (!result) return;
    setError(null);
    setConfirming(true);
    try {
      const data = await reportApi.confirm(result.report_code);
      setConfirmed(data);
      setStep(2);
      toast.success('已入库，可直接对话分析');
      onFinished(result.report_code);
    } catch (err) {
      if (err instanceof ApiError) setError({ message: err.message, hint: err.hint });
    } finally {
      setConfirming(false);
    }
  };

  const mappingRows = Object.entries(result?.column_mapping ?? {});

  return (
    <div className="zd-card p-4">
      <Steps
        size="small"
        current={step}
        items={[
          { title: '选择文件', description: '.csv / .txt，≤200MB' },
          { title: '预览与质量校验', description: '确认字段映射与数据体检' },
          { title: '确认入库', description: '动态建表并建立索引' },
        ]}
      />

      {error ? (
        <Alert
          className="mt-3"
          type="error"
          showIcon
          message={error.message}
          description={error.hint}
        />
      ) : null}

      {step === 0 ? (
        <div className="mt-4">
          <Upload.Dragger
            accept=".csv,.txt"
            maxCount={1}
            showUploadList={false}
            beforeUpload={beforeUpload}
            disabled={uploading || parsing}
          >
            <p className="ant-upload-drag-icon">
              <InboxOutlined />
            </p>
            <p className="ant-upload-text">点击或拖拽文件到此处上传</p>
            <p className="ant-upload-hint text-text-sub">
              需包含时间列（如 Date / 日期）；文件名将作为表名，中文列名会自动规范化
            </p>
          </Upload.Dragger>

          {uploading || parsing ? (
            <div className="mt-3">
              <div className="mb-1 flex items-center gap-2 text-body">
                <span className="font-semibold">{file?.name}</span>
                <span className="text-text-sub">
                  {parsing ? '正在解析数据…' : `正在上传… ${progress}%`}
                </span>
                <span className="zd-num ml-auto text-text-sub">已耗时 {elapsed}s</span>
              </div>
              <Progress
                percent={parsing ? 100 : progress}
                status="active"
                showInfo={false}
              />
              {elapsed > 5 ? (
                <div className="mt-1 text-[11px] text-text-sub">
                  数据集较大，正在解析明细并做质量体检（207 万行约 30s）…
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}

      {step === 1 && result ? (
        <div className="mt-4 space-y-3">
          <Descriptions size="small" column={3} bordered>
            <Descriptions.Item label="文件名">{result.filename}</Descriptions.Item>
            <Descriptions.Item label="目标表名">{result.table_name}</Descriptions.Item>
            <Descriptions.Item label="行数">{result.row_count}</Descriptions.Item>
            <Descriptions.Item label="列数">
              {result.column_count}（原始 {result.raw_column_count}）
            </Descriptions.Item>
            <Descriptions.Item label="编码 / 分隔符">
              {result.encoding} / {result.delimiter === ',' ? '逗号' : result.delimiter}
            </Descriptions.Item>
            <Descriptions.Item label="时间列">{result.time_column}</Descriptions.Item>
            <Descriptions.Item label="数据范围" span={2}>
              {result.time_start ?? '—'} ~ {result.time_end ?? '—'}
            </Descriptions.Item>
            <Descriptions.Item label="待确认有效期">
              {result.pending_expires_at}
            </Descriptions.Item>
          </Descriptions>

          <div>
            <div className="mb-2 font-semibold">字段映射</div>
            <Table<UploadColumn>
              size="small"
              rowKey="name"
              pagination={false}
              scroll={{ y: 200 }}
              dataSource={result.columns}
              columns={[
                { title: '原始列名', dataIndex: 'origin_name', width: 140 },
                {
                  title: '安全列名',
                  dataIndex: 'name',
                  width: 160,
                  render: (value: string, row) => (
                    <span className="font-mono">
                      {value}
                      {row.derived ? <Tag className="ml-1">派生</Tag> : null}
                    </span>
                  ),
                },
                { title: '类型', dataIndex: 'type', width: 90 },
                { title: '单位', dataIndex: 'unit', width: 70 },
                {
                  title: '可否 SUM',
                  width: 90,
                  render: (_value, row) => (
                    <Tag color={row.agg === 'sum' ? 'green' : 'default'}>
                      {row.agg === 'sum' ? '可求和' : '否'}
                    </Tag>
                  ),
                },
              ]}
            />
            {mappingRows.length ? (
              <div className="mt-1 text-[11px] text-text-sub">
                共 {mappingRows.length} 列发生过重命名（中文/特殊字符 → 安全列名）
              </div>
            ) : null}
          </div>

          <div>
            <div className="mb-2 font-semibold">前 {result.preview.preview_rows} 行预览</div>
            <ResultTable
              table={{
                columns: result.preview.columns,
                rows: result.preview.rows,
                row_count: result.preview.returned_rows,
                truncated: false,
              }}
              focusIndex={null}
              onHoverRow={() => undefined}
            />
          </div>

          <div>
            <div className="mb-2 font-semibold">数据质量体检</div>
            <QualityPanel quality={result.quality} />
          </div>

          <Space>
            <Button type="primary" loading={confirming} onClick={() => void confirm()}>
              确认入库
            </Button>
            <Button danger onClick={() => void abort()}>
              放弃上传
            </Button>
            {confirming ? (
              <span className="zd-num text-text-sub">已耗时 {elapsed}s（正在分批灌数并建索引）</span>
            ) : null}
          </Space>
        </div>
      ) : null}

      {step === 2 && confirmed ? (
        <div className="mt-4 space-y-3">
          {confirmed.already_confirmed ? (
            <Alert type="info" showIcon message="该报表已入库（重复确认，已按幂等处理）" />
          ) : (
            <Alert
              type="success"
              showIcon
              message={`入库完成：写入 ${confirmed.inserted_rows} 行`}
              description={`建索引 ${confirmed.indexes.length} 个 · 耗时 ${confirmed.elapsed_ms}ms · 中间文件${
                confirmed.intermediate_removed ? '已清理' : '仍在磁盘'
              }`}
            />
          )}
          <div className="text-text-sub">
            Schema 描述已自动生成，现在可以直接对话分析这张表。
          </div>
          <Space>
            <Button
              type="primary"
              onClick={() => {
                onActivate(confirmed.report_code);
                reset();
              }}
            >
              设为当前报表
            </Button>
            <Button onClick={() => onOpenSchema(confirmed.report_code)}>查看字段字典</Button>
            <Button icon={<ReloadOutlined />} onClick={reset}>
              继续上传
            </Button>
          </Space>
        </div>
      ) : null}
    </div>
  );
}
