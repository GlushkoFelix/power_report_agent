/**
 * 对话工作台（UIUX 5.1 三栏：左 240 会话列表 / 中 flex 对话流 / 右 408 图表与表格）。
 *
 * 数据流全部落在 `chatStore`（SSE 状态机），本页面只做编排与联动：
 * - 输入区发送 → `chatStore.send`（SSE 流式）→ 对话流逐项填充、右栏随之出图；
 * - 芯片编辑 → `slot_patch` 随下次提问提交，「按新条件重算」立即重跑上一次问题；
 * - 图表点击 ↔ 表格行 hover 的联动在 `ResultPanel` 内部完成；
 * - 字段字典抽屉按当前报表加载（`GET /reports/{code}/schema`）。
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { Button, Drawer, Space, Typography } from 'antd';
import { BarChartOutlined, CloudUploadOutlined, ThunderboltFilled } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';

import { useChatStore, lastResultTurn } from '@/stores/chat';
import { useReportStore } from '@/stores/reports';
import { toast } from '@/utils/toast';
import Composer from './Composer';
import ConversationList from './ConversationList';
import MessageStream from './MessageStream';
import ResultPanel from './ResultPanel';
import SchemaDrawer from './SchemaDrawer';
import SlotBar from './SlotBar';

export default function WorkbenchPage() {
  const navigate = useNavigate();
  const turns = useChatStore((state) => state.turns);
  const streaming = useChatStore((state) => state.streaming);
  const activeDetail = useChatStore((state) => state.activeDetail);
  const preferredReportCode = useChatStore((state) => state.preferredReportCode);
  const send = useChatStore((state) => state.send);
  const answerClarify = useChatStore((state) => state.answerClarify);
  const stop = useChatStore((state) => state.stop);
  const editChip = useChatStore((state) => state.editChip);
  const rerender = useChatStore((state) => state.rerender);
  const reports = useReportStore((state) => state.items);

  /**
   * 会话栏折叠状态。**窄屏默认收起**（<768px）：手机实测 390px 视口下，
   * 240px 的会话栏会把主区挤到约 150px —— 气泡与输入框会一字一行（任务书要求适配移动端浏览器）。
   * 用户仍可手动展开（点收起态的小按钮），跨断点时按视口重新判定。
   */
  const [collapsed, setCollapsed] = useState(
    () => typeof window !== 'undefined' && window.innerWidth < 768,
  );
  const [schemaOpen, setSchemaOpen] = useState(false);
  const [exportSignal, setExportSignal] = useState(0);
  /** <1366px 时右栏收进底部抽屉（UIUX 10.1 的"右栏折叠"） */
  const [resultOpen, setResultOpen] = useState(false);
  const streamRef = useRef<HTMLDivElement>(null);

  /**
   * 主区左上角的**会话名称**：取 `conversations` 里的当前会话（与侧栏同一份数据），
   * 因此重命名后立即同步；`activeDetail` 作为兜底（列表尚未刷新时仍能显示）。
   */
  const activeTitle = useChatStore((state) => {
    const found = state.conversations.find((item) => item.conversation_code === state.activeCode);
    return found?.title ?? state.activeDetail?.title ?? null;
  });

  const resultTurn = useMemo(() => lastResultTurn(turns), [turns]);
  const reportCode = activeDetail?.active_report_code ?? preferredReportCode ?? null;
  const report = reports.find((item) => item.report_code === reportCode) ?? null;
  const lastQuestion = [...turns].reverse().find((turn) => turn.role === 'user');

  // 新消息 / 阶段推进时贴底滚动
  useEffect(() => {
    const node = streamRef.current?.querySelector('.zd-scroll');
    if (node) node.scrollTop = node.scrollHeight;
  }, [turns.length, resultTurn?.status]);

  // 视口跨过 768px 时重新判定会话栏折叠状态（手机竖屏 ↔ 横屏 / 桌面）
  useEffect(() => {
    const query = window.matchMedia('(max-width: 767px)');
    const apply = () => setCollapsed(query.matches);
    apply();
    query.addEventListener('change', apply);
    return () => query.removeEventListener('change', apply);
  }, []);

  const recalculate = () => {
    const question = lastQuestion && 'text' in lastQuestion ? lastQuestion.text : '';
    if (!question) {
      toast.warning('还没有可重算的问题，请先提问');
      return;
    }
    void send(question);
  };

  return (
    <div className="flex h-full min-h-0">
      <ConversationList collapsed={collapsed} onToggle={() => setCollapsed((value) => !value)} />

      <section className="flex min-w-0 flex-1 flex-col" style={{ background: 'var(--c-bg-page)' }}>
        {/* 左上角：当前会话名称（跟随重命名实时变化；无会话时不占位） */}
        {activeTitle ? (
          <div
            aria-label="会话名称"
            className="flex h-11 shrink-0 items-center gap-2 border-b px-4"
            style={{ background: 'var(--c-bg-card)', borderColor: 'var(--c-line)' }}
          >
            <Typography.Text
              strong
              ellipsis={{ tooltip: activeTitle }}
              style={{ fontSize: 15, color: 'var(--c-text-main)', maxWidth: 520 }}
            >
              {activeTitle}
            </Typography.Text>
          </div>
        ) : null}

        <SlotBar onRecalculate={recalculate} streaming={streaming} />

        <div ref={streamRef} className="flex min-h-0 flex-1 flex-col">
          {turns.length === 0 ? (
            /* **空态统一（v1.19.12）**：有报表 / 没报表此前是两套排布（一个带按钮组、一个只有单按钮，
               且信息行位置不同），用户反馈"删掉报表后换了另一个页面，不妥当"。现在统一为同一张卡：
               标题 + 说明 + **一行"当前数据源"信息**（有报表就报报表，没有就说明怎么开始）+
               同一组按钮（无报表时上传按钮提升为主要操作）。 */
            <div className="zd-scroll flex min-h-0 flex-1 items-center justify-center p-6">
              <div className="zd-card max-w-[560px] p-6 text-center">
                <ThunderboltFilled style={{ fontSize: 28, color: 'var(--c-primary)' }} />
                <Typography.Title level={4} className="!mb-1 !mt-2">
                  你好，我是智电析报
                </Typography.Title>
                <Typography.Paragraph type="secondary" className="!mb-4">
                  用一个问题开始：例如「全期总用电量是多少」「按月看用电趋势」。
                  结果会同时给出 SQL/代码、数据表格、图表与文字解读，且标注每一个口径。
                </Typography.Paragraph>
                <Typography.Paragraph type="secondary" className="!mb-3 !text-[11px]">
                  {report ? (
                    <>
                      当前报表：{report.filename}（{report.report_code}）· 数据范围{' '}
                      {report.time_start ?? '—'} ~ {report.time_end ?? '—'} ·{' '}
                      {report.row_count ?? '—'} 行
                    </>
                  ) : (
                    <>还没有可用报表。先上传一份带时间列的 CSV，系统会自动建表、生成字段说明，之后就能直接提问。</>
                  )}
                </Typography.Paragraph>
                <Space>
                  <Button
                    size="small"
                    type={report ? 'default' : 'primary'}
                    icon={<CloudUploadOutlined />}
                    onClick={() => navigate('/reports')}
                  >
                    {report ? '上传新报表' : '上传第一份报表'}
                  </Button>
                  <Button size="small" type="text" onClick={() => navigate('/reports')}>
                    报表管理
                  </Button>
                </Space>
              </div>
            </div>
          ) : (
            <MessageStream
              turns={turns}
              onOpenSchema={() => setSchemaOpen(true)}
              onPickQuestion={(question) => void send(question)}
              onRetry={(question) => void send(question)}
              onReuseSlot={(slot) => {
                // 「沿用本次口径」：把该轮解析出的条件写回芯片栏（下一次提问即生效）
                Object.entries(slot).forEach(([field, value]) => editChip(field, value));
                toast.success('已将本次口径写入条件栏');
              }}
              onClarify={(patch) => void answerClarify(patch)}
              onExport={() => setExportSignal((value) => value + 1)}
            />
          )}
        </div>

        {/* 窄屏（<1366px）时右栏收起：用抽屉承接"图表 / 表格 / 执行信息"三件套 */}
        <div className="px-4 pt-2 xl:hidden">
          <Button
            block
            icon={<BarChartOutlined />}
            onClick={() => setResultOpen(true)}
            disabled={!resultTurn}
          >
            {resultTurn ? '查看图表与表格' : '图表与表格（暂无结果）'}
          </Button>
        </div>

        <Composer
          streaming={streaming}
          showExamples={turns.length === 0}
          onSend={(text) => void send(text)}
          onStop={stop}
        />
      </section>

      <div className="hidden xl:flex">
        <ResultPanel
          turn={resultTurn}
          onRerender={(payload) => void rerender(payload)}
          onOpenSchema={() => setSchemaOpen(true)}
          exportSignal={exportSignal}
        />
      </div>

      <Drawer
        title="结果：图表与数据表格"
        placement="bottom"
        height="78%"
        open={resultOpen}
        onClose={() => setResultOpen(false)}
        destroyOnClose={false}
        styles={{ body: { padding: 0 } }}
      >
        {/* 同一份面板组件：抽屉与右栏行为完全一致（联动、重绘、导出都可用） */}
        <div className="flex h-full overflow-hidden">
          <ResultPanel
            variant="drawer"
            turn={resultTurn}
            onRerender={(payload) => void rerender(payload)}
            onOpenSchema={() => setSchemaOpen(true)}
          />
        </div>
      </Drawer>

      <SchemaDrawer reportCode={reportCode} open={schemaOpen} onClose={() => setSchemaOpen(false)} />
    </div>
  );
}
