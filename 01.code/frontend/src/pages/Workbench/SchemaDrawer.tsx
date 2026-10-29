/**
 * Schema 数据字典抽屉（UIUX 5.3 区域 4）。
 *
 * 契约要点：
 * - 列清单取 `GET /reports/{report_code}/schema` 的 `structured.columns`：
 *   **可否 SUM**（比率列显式标"否"）、**派生列角标**（`derived=true` 不是用户自己的列）；
 * - 底部「查看完整口径与红线规则」渲染同一接口的 `text` 字段——它与注入给模型的说明
 *   **逐字一致**（上传报表走生成路径，含 U1 口径未知声明）；
 * - `truncated=true` 时提示"字段说明已压缩"。
 */

import { useEffect, useState } from 'react';
import { Alert, Collapse, Drawer, Spin, Table, Tag } from 'antd';

import { ApiError } from '@/api/client';
import { reportApi } from '@/api/reports';
import type { ReportSchema, SchemaColumn } from '@/api/types';
import { toast } from '@/utils/toast';

export default function SchemaDrawer({
  reportCode,
  open,
  onClose,
}: {
  reportCode: string | null;
  open: boolean;
  onClose: () => void;
}) {
  const [schema, setSchema] = useState<ReportSchema | null>(null);
  const [loading, setLoading] = useState(false);

  // 关闭抽屉或切换报表/会话时**先清空**上一张表的字典：
  // 否则"换到别的会话后打开字段字典，看到的还是上一张表的字段"（用户实测反馈过）。
  useEffect(() => {
    setSchema(null);
    if (!open || !reportCode) return;

    let alive = true;
    setLoading(true);
    reportApi
      .schema(reportCode)
      .then((data) => {
        if (alive) setSchema(data);
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
  }, [open, reportCode]);

  const columns = (schema?.structured?.columns ?? []) as SchemaColumn[];

  return (
    <Drawer
      title={`字段字典 · ${reportCode ?? ''}`}
      width={560}
      open={open}
      onClose={onClose}
      destroyOnClose
    >
      {loading ? (
        <div className="flex justify-center py-10">
          <Spin />
        </div>
      ) : !schema ? (
        <Alert type="warning" showIcon message="未加载到 Schema" />
      ) : (
        <>
          {schema.text_source === 'generated' ? (
            <Alert
              className="mb-3"
              type="info"
              showIcon
              message="本表为用户上传数据，未应用系统口径"
            />
          ) : null}
          {schema.truncated ? (
            <Alert className="mb-3" type="warning" showIcon message="字段说明已压缩（超出 token 预算）" />
          ) : null}

          <div className="mb-2 text-[11px] text-text-sub">
            口径版本：{schema.caliber_version ?? '—'} · 注入估算 tokens：{schema.est_tokens}
          </div>

          <Table<SchemaColumn>
            size="small"
            rowKey="name"
            pagination={false}
            dataSource={columns}
            columns={[
              {
                title: '字段',
                dataIndex: 'name',
                render: (value: string, row) => (
                  <span className="flex items-center gap-1">
                    <span className="font-mono">{value}</span>
                    {row.derived ? <Tag color="default">派生</Tag> : null}
                  </span>
                ),
              },
              {
                title: '类型 / 单位',
                render: (_value, row) => (
                  <span>
                    {row.type}
                    {row.unit && row.unit !== '—' ? ` · ${row.unit}` : ''}
                  </span>
                ),
              },
              {
                title: '可否 SUM',
                width: 90,
                render: (_value, row) => (
                  <Tag color={row.agg === 'sum' ? 'green' : 'default'} style={{ marginInlineEnd: 0 }}>
                    {row.agg === 'sum' ? '可求和' : '否'}
                  </Tag>
                ),
              },
            ]}
          />

          <Collapse
            className="mt-4"
            items={[
              {
                key: 'text',
                label: '查看完整口径与红线规则（与模型收到的说明逐字一致）',
                children: (
                  <pre className="zd-scroll m-0 max-h-[420px] overflow-auto whitespace-pre-wrap font-mono text-[11px] leading-5">
                    {schema.text}
                  </pre>
                ),
              },
            ]}
          />
        </>
      )}
    </Drawer>
  );
}
