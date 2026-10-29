/**
 * 对话工作台状态机（UIUX 8.1：SSE 事件 → 界面映射；8.2：阶段时间线）。
 *
 * 三条不可动摇的设计：
 * 1. **单轮唯一**：同一会话同时只允许一个进行中的分析（UIUX 11「并发表现」），
 *    因此 `streaming` 为真时输入区禁用、芯片禁用编辑；
 * 2. **失败不静默**：`error` 事件落到消息上是错误块（卡片），网络层失败同样落成错误块，
 *    绝不留白；`error` 之后仍会收到 `done`，收到后只做"解除 loading + 记录 record_id"；
 * 3. **澄清不是错误**：`clarify` 事件把消息置为 `clarify` 态（主色卡片），
 *    点选后调 `POST /analysis/clarify` 就地继续，**不产生分析记录**（`done.record_id=null`）。
 */

import { create } from 'zustand';

import { ApiError } from '@/api/client';
import { analysisApi } from '@/api/analysis';
import { chartApi } from '@/api/charts';
import { conversationApi } from '@/api/conversations';
import type {
  ChartPayload,
  ClarifyPayload,
  CodePayload,
  Conversation,
  ConversationDetail,
  ErrorPayload,
  MetaPayload,
  NarrativePayload,
  RecordDetail,
  TablePayload,
} from '@/api/types';
import type { RerenderIn, Slot, SlotFilter } from '@/api/types';
import { presentationOf } from '@/api/errors';
import { toast } from '@/utils/toast';

// ── 消息与阶段模型 ─────────────────────────────────────────────────────────

export type StageKey = 'parsing' | 'generating' | 'executing' | 'chart' | 'narrative';

export const STAGE_SEQUENCE: StageKey[] = ['parsing', 'generating', 'executing', 'chart', 'narrative'];

export const STAGE_LABELS: Record<StageKey, string> = {
  parsing: '解析问题',
  generating: '生成 SQL/代码',
  executing: '沙箱执行',
  chart: '生成图表',
  narrative: '生成解读',
};

export interface StageState {
  key: StageKey;
  status: 'pending' | 'running' | 'done' | 'skipped';
  elapsedMs?: number;
}

export type TurnStatus =
  | 'streaming'
  | 'success'
  | 'failed'
  | 'refused'
  | 'clarify'
  | 'canceled';

export interface AssistantTurn {
  id: string;
  role: 'assistant';
  question: string;
  status: TurnStatus;
  stages: StageState[];
  slot: Slot | null;
  /** 最终执行的语句/代码（重写与换路会多次出现 `code` 事件，以最后一次为准） */
  code: CodePayload | null;
  codeHistory: CodePayload[];
  table: TablePayload | null;
  chart: ChartPayload | null;
  narrative: NarrativePayload | null;
  meta: MetaPayload | null;
  error: ErrorPayload | null;
  clarify: ClarifyPayload | null;
  recordId: string | null;
  /** 本轮提问时所在的报表（历史消息保留原报表标记，切换报表不重算） */
  reportCode: string | null;
  /** 历史加载时的记录编号：右栏据此**回放表格与图表**（`GET /analysis/records/{code}`） */
  recordCode?: string | null;
  /** 是否来自历史加载（历史消息的文本与澄清卡直接还原，表格/图表异步回放） */
  fromHistory?: boolean;
}

export interface UserTurn {
  id: string;
  role: 'user';
  text: string;
  reportCode?: string | null;
}

export type Turn = UserTurn | AssistantTurn;

function emptyStages(): StageState[] {
  return STAGE_SEQUENCE.map((key) => ({ key, status: 'pending' }));
}

function newId(prefix: string): string {
  return `${prefix}_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
}

function freshTurn(question: string, reportCode: string | null): AssistantTurn {
  return {
    id: newId('turn'),
    role: 'assistant',
    question,
    status: 'streaming',
    stages: emptyStages(),
    slot: null,
    code: null,
    codeHistory: [],
    table: null,
    chart: null,
    narrative: null,
    meta: null,
    error: null,
    clarify: null,
    recordId: null,
    reportCode,
  };
}

/** 历史消息 → turn（助手消息可能是澄清卡 JSON，也可能是纯文本解读） */
function historyTurns(
  items: { role: 'user' | 'assistant'; content: string | null; record_code?: string | null }[],
  reportCode: string | null,
): Turn[] {
  const turns: Turn[] = [];
  let lastQuestion = '';
  for (const item of items) {
    if (item.role === 'user') {
      lastQuestion = item.content ?? '';
      turns.push({ id: newId('user'), role: 'user', text: lastQuestion, reportCode });
      continue;
    }
    const raw = item.content ?? '';
    let parsed: Record<string, unknown> | null = null;
    if (raw.trim().startsWith('{')) {
      try {
        parsed = JSON.parse(raw) as Record<string, unknown>;
      } catch {
        parsed = null;
      }
    }
    const turn = freshTurn(lastQuestion, reportCode);
    turn.fromHistory = true;
    turn.recordCode = item.record_code ?? null;
    turn.stages = turn.stages.map((stage) => ({ ...stage, status: 'done' }));
    if (parsed && parsed.type === 'clarify') {
      turn.status = 'clarify';
      turn.clarify = {
        question: String(parsed.question ?? ''),
        options: (parsed.options as ClarifyPayload['options']) ?? [],
        missing_slot: String(parsed.missing_slot ?? 'metric'),
        partial_slot: (parsed.partial_slot as Slot) ?? {},
      };
      turn.slot = turn.clarify.partial_slot;
    } else {
      turn.status = 'success';
      turn.narrative = { text: raw, key_numbers: [], anomalies: [] };
    }
    turns.push(turn);
  }
  return turns;
}

/**
 * 记录详情 → 前端图表载荷。
 *
 * `analyses.chart_json` 按数据库设计只存**纯 Plotly figure**，但口径标记
 * （类型/单位/TopN/合并数/R3 收敛/抽稀/缺口）都被后端写进了 `layout.meta`
 * （见 `chart_service._figure_meta` 的注释），因此这里能从 figure 内部完整还原右栏文案。
 */
function chartFromRecord(detail: RecordDetail): ChartPayload | null {
  const figure = (detail.chart as Record<string, unknown> | null) ?? null;
  if (!figure) return null;
  const layout = (figure.layout as Record<string, unknown> | undefined) ?? {};
  const meta = (layout.meta as Record<string, unknown> | undefined) ?? {};
  return {
    figure,
    chart_type: String(meta.chart_type ?? 'line'),
    fallback: null,
    reason: null,
    unit: (meta.unit as string | null) ?? null,
    x_axis: (meta.x_axis as string | null) ?? null,
    series: (meta.series as string[] | undefined) ?? [],
    r3_adjusted: Boolean(meta.r3_adjusted),
    r3_note: (meta.r3_note as string | null) ?? null,
    dropped_columns: (meta.dropped_columns as string[] | undefined) ?? [],
    excluded_columns: (meta.excluded_columns as string[] | undefined) ?? [],
    top_n: (meta.top_n as number | undefined) ?? null,
    merged_count: Number(meta.merged_count ?? 0),
    downsampled_from: (meta.downsampled_from as number | undefined) ?? null,
    gap_count: Number(meta.gap_count ?? 0),
  };
}

/** 用记录详情补全历史 turn（表格 / 图表 / 代码 / 解读 / 执行信息） */
function hydrateFromRecord(turn: AssistantTurn, detail: RecordDetail): AssistantTurn {
  const code: CodePayload | null =
    detail.route === 'pandas'
      ? detail.code_text
        ? { route: 'pandas', code: detail.code_text }
        : null
      : detail.sql
        ? { route: 'sql', sql: detail.sql }
        : null;

  return {
    ...turn,
    status: detail.status === 'success' ? 'success' : 'failed',
    recordId: detail.record_code,
    slot: (detail.slot as Slot) ?? turn.slot,
    code,
    codeHistory: code ? [code] : [],
    table: detail.result?.columns?.length ? detail.result : null,
    chart: chartFromRecord(detail),
    narrative: detail.narrative
      ? {
          text: detail.narrative,
          key_numbers: detail.key_numbers ?? [],
          anomalies: detail.anomalies ?? [],
        }
      : turn.narrative,
    error:
      detail.status === 'success'
        ? null
        : {
            code: detail.error_code ?? 'E_INTERNAL',
            message: detail.error_message ?? '本次未生成结果',
            retries: detail.retries,
            path_switched: detail.path_switched,
          },
    meta: {
      elapsed_ms: detail.elapsed_ms ?? 0,
      retries: detail.retries ?? 0,
      llm_tokens: detail.llm_tokens ?? 0,
      model: detail.llm_model ?? '',
      llm_impl: '',
    },
  };
}

/** 加载历史后，回放**最后一条有记录的结果**（右栏与执行信息不再是空的） */
async function hydrateLastResult(turns: Turn[]): Promise<Turn[]> {
  const target = [...turns]
    .reverse()
    .find((turn) => turn.role === 'assistant' && turn.recordCode) as AssistantTurn | undefined;
  if (!target?.recordCode) return turns;
  try {
    const detail = await analysisApi.record(target.recordCode);
    return turns.map((turn) =>
      turn.role === 'assistant' && turn.id === target.id ? hydrateFromRecord(turn, detail) : turn,
    );
  } catch {
    // 记录已被删除等场景：保持"文本已还原、结果不回放"的降级状态，不打扰用户
    return turns;
  }
}

// ── Store ──────────────────────────────────────────────────────────────────

/** 进行中的流（放模块级变量：它不需要触发渲染） */
let controller: AbortController | null = null;

/** 是否已做过"进入应用时自动打开最近会话"（**只做一次**：删除会话后不该再自动跳走） */
let autoSelectDone = false;

/** 会话切换的序号：并发切换时只认最后一次（防"切 A 又切 B，A 的响应后到把 B 覆盖"） */
let selectionSeq = 0;

interface ChatState {
  conversations: Conversation[];
  conversationsLoading: boolean;
  /** 会话列表的标题关键词（服务端 `GET /conversations?q=` 模糊筛选；空串 = 不过滤） */
  conversationKeyword: string;
  /** 正在新建会话（按钮据此 loading，防重复点击建出一堆空会话） */
  creating: boolean;
  activeCode: string | null;
  /** 会话详情（活跃报表 + 服务端 slot） */
  activeDetail: ConversationDetail | null;
  /** 尚无会话时用户先挑的报表（新建会话时带上，避免"选了没用"） */
  preferredReportCode: string | null;
  turns: Turn[];
  messagesLoading: boolean;
  streaming: boolean;
  /** 服务端生效的条件（芯片栏数据源） */
  slot: Slot | null;
  /** 芯片编辑后待提交的增量（UIUX 6.3：随下一次提问提交） */
  pendingPatch: Slot | null;

  loadConversations: () => Promise<void>;
  /** 设置标题关键词并立即按它重取列表（传空串 = 取消筛选） */
  setConversationKeyword: (keyword: string) => Promise<void>;
  /** 登录 / 退出时重置（切账号不能看到上一个人的会话；同时复位"自动选会话"标记） */
  reset: () => void;
  /** 进入应用时自动打开最近会话（**仅首次**，见 `autoSelectDone`） */
  ensureInitialSelection: () => Promise<void>;
  /**
   * 进入**草稿态**（「新建会话」按钮的行为）。
   *
   * 契约（用户反馈后定案）：点按钮**只在主区开一个空白页，不落库**——
   * 侧栏里出现会话记录要等用户真正发出第一条消息（那时 `send` 才调 `createConversation`）。
   * 此前是"点了就建"，结果侧栏会堆出一串 `未命名会话 · 0 条消息`。
   */
  startNewConversation: () => void;
  createConversation: (activeReportCode?: string | null) => Promise<string | null>;
  selectConversation: (code: string, options?: { silent?: boolean }) => Promise<void>;
  renameConversation: (code: string, title: string) => Promise<void>;
  togglePin: (code: string, pinned: boolean) => Promise<void>;
  deleteConversation: (code: string) => Promise<void>;
  /** 批量置顶 / 取消置顶（后端无批量端点 → 串行调用，末尾统一刷新一次列表） */
  batchPin: (codes: string[], pinned: boolean) => Promise<void>;
  /** 批量删除（同上串行；删到当前会话时回到空态，与单条删除语义一致） */
  batchDelete: (codes: string[]) => Promise<void>;
  setActiveReport: (reportCode: string) => Promise<void>;

  send: (text: string, slotPatch?: Slot | null) => Promise<void>;
  answerClarify: (patch: Slot) => Promise<void>;
  stop: () => void;

  editChip: (field: string, value: unknown) => void;
  clearChip: (field: string) => void;
  clearPendingPatch: () => void;

  /** 换类型 / 换粒度 / topN：走 `POST /charts/rerender`（不调 LLM，后端回写 chart_json） */
  rerender: (payload: Omit<RerenderIn, 'record_code'>) => Promise<void>;
}

export const useChatStore = create<ChatState>((set, get) => ({
  conversations: [],
  conversationsLoading: false,
  conversationKeyword: '',
  creating: false,
  activeCode: null,
  activeDetail: null,
  preferredReportCode: null,
  turns: [],
  messagesLoading: false,
  streaming: false,
  slot: null,
  pendingPatch: null,

  loadConversations: async () => {
    set({ conversationsLoading: true });
    try {
      // 带当前关键词：搜索期内新建 / 删除 / 置顶后的刷新仍保持同一筛选口径
      const keyword = get().conversationKeyword.trim();
      const result = await conversationApi.list(keyword ? { q: keyword } : undefined);
      set({ conversations: result.items });
    } catch (error) {
      if (error instanceof ApiError) toast.error(error.message, error.requestId);
    } finally {
      set({ conversationsLoading: false });
    }
  },

  setConversationKeyword: async (keyword) => {
    set({ conversationKeyword: keyword });
    await get().loadConversations();
  },

  reset: () => {
    // 中断进行中的流，并复位模块级标记（`autoSelectDone` / `selectionSeq`）
    controller?.abort();
    controller = null;
    autoSelectDone = false;
    selectionSeq += 1;
    set({
      conversations: [],
      conversationsLoading: false,
      conversationKeyword: '',
      creating: false,
      activeCode: null,
      activeDetail: null,
      preferredReportCode: null,
      turns: [],
      messagesLoading: false,
      streaming: false,
      slot: null,
      pendingPatch: null,
    });
  },

  ensureInitialSelection: async () => {
    // 只在"首次拿到会话列表"时自动打开最近一条；删除当前会话后不再自动跳走
    if (autoSelectDone || get().activeCode || get().streaming) return;
    if (get().conversations.length) {
      autoSelectDone = true;
      await get().selectConversation(get().conversations[0].conversation_code, { silent: true });
    }
  },

  startNewConversation: () => {
    if (get().streaming) {
      toast.warning('本轮分析尚未结束，请先停止或等待完成');
      return;
    }
    // 标记"已做过自动选会话"：否则列表刷新后 `ensureInitialSelection` 会把用户又拽回旧会话
    autoSelectDone = true;
    selectionSeq += 1; // 丢弃在途的详情请求，避免它把草稿态覆盖回旧会话
    set({
      activeCode: null,
      activeDetail: null,
      turns: [],
      slot: {},
      pendingPatch: null,
    });
  },

  createConversation: async (activeReportCode) => {
    // 防重复点击：① 正在建 → 直接忽略；② 当前已是空会话 → 复用而不是再建一条
    if (get().creating) return get().activeCode;
    if (get().streaming) {
      toast.warning('本轮分析尚未结束，请先停止或等待完成');
      return null;
    }
    const current = get().activeDetail;
    if (current && get().activeCode && current.message_count === 0 && get().turns.length === 0) {
      return get().activeCode;
    }

    set({ creating: true });
    try {
      const created = await conversationApi.create({
        active_report_code: activeReportCode ?? get().preferredReportCode ?? null,
      });
      autoSelectDone = true; // 新会话是用户显式意图，之后不再自动切走
      set({
        activeCode: created.conversation_code,
        activeDetail: { ...created, slot: {} },
        turns: [],
        slot: {},
        pendingPatch: null,
      });
      await get().loadConversations();
      return created.conversation_code;
    } catch (error) {
      if (error instanceof ApiError) toast.error(error.message, error.requestId);
      return null;
    } finally {
      set({ creating: false });
    }
  },

  selectConversation: async (code, options) => {
    if (get().streaming) {
      if (!options?.silent) toast.warning('本轮分析尚未结束，请先停止或等待完成');
      return;
    }
    if (code === get().activeCode && !options?.silent) return;

    const seq = ++selectionSeq;
    set({ activeCode: code, messagesLoading: true, turns: [], slot: null, pendingPatch: null });
    try {
      const [detail, messages] = await Promise.all([
        conversationApi.detail(code),
        conversationApi.messages(code, { limit: 100 }),
      ]);
      // 期间用户又切了别的会话（或已开始提问）→ 丢弃本次结果，避免覆盖新内容
      if (seq !== selectionSeq) return;
      const restored = historyTurns(messages.items, detail.active_report_code);
      set({
        activeDetail: detail,
        slot: detail.slot ?? {},
        turns: restored,
      });
      // 结果快照单独回放（表格/图表/执行信息），失败不影响已还原的对话文本
      const hydrated = await hydrateLastResult(restored);
      if (seq === selectionSeq && hydrated !== restored) set({ turns: hydrated });
    } catch (error) {
      if (error instanceof ApiError) toast.error(error.message, error.requestId);
    } finally {
      if (seq === selectionSeq) set({ messagesLoading: false });
    }
  },

  renameConversation: async (code, title) => {
    try {
      await conversationApi.patch(code, { title });
      const detail = get().activeDetail;
      set({
        conversations: get().conversations.map((item) =>
          item.conversation_code === code ? { ...item, title } : item,
        ),
        // 会话详情也一并更新：主区左上角的会话名称读它（否则重命名后标题不变）
        activeDetail: detail && detail.conversation_code === code ? { ...detail, title } : detail,
      });
    } catch (error) {
      if (error instanceof ApiError) toast.error(error.message, error.requestId);
    }
  },

  togglePin: async (code, pinned) => {
    try {
      const result = await conversationApi.patch(code, { is_pinned: pinned });
      if (result.evicted_pinned_code) {
        toast.info('置顶最多 5 条，已取消最早置顶的会话');
      }
      await get().loadConversations();
    } catch (error) {
      if (error instanceof ApiError) toast.error(error.message, error.requestId);
    }
  },

  deleteConversation: async (code) => {
    try {
      await conversationApi.remove(code);
      toast.success('会话已删除');
      const rest = get().conversations.filter((item) => item.conversation_code !== code);
      set({ conversations: rest });
      if (get().activeCode === code) {
        // 删掉的是当前会话 → 回到空态（欢迎语 + 示例问题），**不再自动跳到别的会话**
        autoSelectDone = true;
        selectionSeq += 1;
        set({ activeCode: null, activeDetail: null, turns: [], slot: null, pendingPatch: null });
      }
    } catch (error) {
      if (error instanceof ApiError) toast.error(error.message, error.requestId);
    }
  },

  batchPin: async (codes, pinned) => {
    if (!codes.length) return;
    try {
      let evicted = false;
      // 串行而非并发：置顶有"最多 5 条、超出挤掉最早"的服务端规则，并发下谁被挤掉不可预期
      for (const code of codes) {
        const result = await conversationApi.patch(code, { is_pinned: pinned });
        if (result.evicted_pinned_code) evicted = true;
      }
      if (evicted) toast.info('置顶最多 5 条，已取消最早置顶的会话');
      await get().loadConversations();
    } catch (error) {
      if (error instanceof ApiError) toast.error(error.message, error.requestId);
      await get().loadConversations(); // 部分成功也要让列表回到真实状态
    }
  },

  batchDelete: async (codes) => {
    if (!codes.length) return;
    try {
      for (const code of codes) {
        await conversationApi.remove(code);
      }
      toast.success(`已删除 ${codes.length} 个会话`);
      set({
        conversations: get().conversations.filter((item) => !codes.includes(item.conversation_code)),
      });
      const active = get().activeCode;
      if (active && codes.includes(active)) {
        // 与单条删除一致：回到空态（欢迎语 + 示例问题），不自动跳到别的会话
        autoSelectDone = true;
        selectionSeq += 1;
        set({ activeCode: null, activeDetail: null, turns: [], slot: null, pendingPatch: null });
      }
    } catch (error) {
      if (error instanceof ApiError) toast.error(error.message, error.requestId);
      await get().loadConversations();
    }
  },

  setActiveReport: async (reportCode) => {
    const { activeCode, activeDetail } = get();
    if (!activeCode) {
      // 还没有会话：先记住选择，新建会话时带上（`createConversation` 会用它）
      set({ preferredReportCode: reportCode, slot: {}, pendingPatch: null });
      toast.info('已选择报表，将在新建会话时生效');
      return;
    }
    try {
      await conversationApi.patch(activeCode, { active_report_code: reportCode });
      set({
        activeDetail: activeDetail
          ? { ...activeDetail, active_report_code: reportCode }
          : activeDetail,
        // 后端切换活跃报表时会清空会话条件 → 芯片栏随之清空（UIUX 6.3）
        slot: {},
        pendingPatch: null,
      });
      toast.info('条件已重置：已切换到新报表的默认口径');
    } catch (error) {
      if (error instanceof ApiError) toast.error(error.message, error.requestId);
    }
  },

  send: async (text, slotPatch) => {
    const state = get();
    const message = text.trim();
    if (!message) return;
    if (state.streaming) {
      toast.warning('当前已有分析在进行中');
      return;
    }

    let conversationCode = state.activeCode;
    if (!conversationCode) {
      conversationCode = await get().createConversation();
      if (!conversationCode) return;
    }

    const reportCode = get().activeDetail?.active_report_code ?? null;
    const assistant = freshTurn(message, reportCode);
    const userTurn: UserTurn = { id: newId('user'), role: 'user', text: message, reportCode };
    const merged = slotPatch ?? get().pendingPatch ?? null;

    set({
      turns: [...get().turns, userTurn, assistant],
      streaming: true,
      pendingPatch: null,
    });

    await runStream(
      set,
      (handlers, signal) =>
        analysisApi.chat(
          {
            conversation_code: conversationCode as string,
            message,
            slot_patch: merged ?? undefined,
          },
          handlers,
          signal,
        ),
      assistant.id,
    );

    await get().loadConversations();
  },

  answerClarify: async (patch) => {
    const state = get();
    const conversationCode = state.activeCode;
    if (!conversationCode || state.streaming) return;

    // 澄清卡不产生分析记录：点选后就地续跑，事件序列与 chat 相同
    const lastTurn = [...state.turns].reverse().find((turn) => turn.role === 'assistant');
    const question = (lastTurn as AssistantTurn | undefined)?.question ?? '';
    const assistant = freshTurn(question, state.activeDetail?.active_report_code ?? null);

    set({ turns: [...get().turns, assistant], streaming: true });

    await runStream(
      set,
      (handlers, signal) =>
        analysisApi.clarify({ conversation_code: conversationCode, question, slot_patch: patch }, handlers, signal),
      assistant.id,
    );

    await get().loadConversations();
  },

  stop: () => {
    controller?.abort();
    controller = null;
    set((state) => ({
      streaming: false,
      turns: state.turns.map((turn) =>
        turn.role === 'assistant' && turn.status === 'streaming'
          ? { ...turn, status: 'canceled' as TurnStatus }
          : turn,
      ),
    }));
  },

  editChip: (field, value) => {
    const { slot, pendingPatch } = get();
    const nextSlot: Slot = { ...(slot ?? {}) };
    if (field === 'filters') {
      nextSlot.filters = value as SlotFilter[];
    } else if (value === null || value === undefined) {
      delete nextSlot[field];
    } else {
      nextSlot[field] = value;
    }
    set({
      slot: nextSlot,
      pendingPatch: { ...(pendingPatch ?? {}), [field]: value as SlotFilter[] | unknown },
    });
  },

  clearChip: (field) => {
    const nextSlot: Slot = { ...(get().slot ?? {}) };
    if (field === 'filters') {
      nextSlot.filters = [];
    } else {
      delete nextSlot[field];
    }
    // 「清除」= 该字段不传：后端暂无显式清除语义（UIUX 6.3），因此不进 pendingPatch
    set({
      slot: nextSlot,
      pendingPatch: { ...(get().pendingPatch ?? {}), [field]: field === 'filters' ? [] : null },
    });
  },

  clearPendingPatch: () => set({ pendingPatch: null }),

  rerender: async (payload) => {
    const state = get();
    const target = [...state.turns]
      .reverse()
      .find((turn) => turn.role === 'assistant' && turn.recordId) as AssistantTurn | undefined;
    if (!target?.recordId) {
      toast.warning('本条结果尚未落库，暂不能重绘');
      return;
    }
    try {
      const chart = await chartApi.rerender({ record_code: target.recordId, ...payload });
      set({
        turns: state.turns.map((turn) =>
          turn.role === 'assistant' && turn.id === target.id ? { ...turn, chart } : turn,
        ),
      });
    } catch (error) {
      if (error instanceof ApiError) {
        const presentation = presentationOf(error.code);
        if (error.code === 'E_CHART_UNSUPPORTED') {
          const supported = (error.data?.supported as string[] | undefined)?.join(' / ');
          toast.warning(`${error.message}${supported ? `（可用类型：${supported}）` : ''}`);
        } else if (error.code === 'E_GRANULARITY_UNAVAILABLE') {
          toast.warning(error.message);
        } else if (presentation.severity === 'toast') {
          toast.error(error.message, error.requestId);
        }
      }
    }
  },
}));

// ── 流式执行（chat / clarify 共用）─────────────────────────────────────────

type SetState = (partial: Partial<ChatState> | ((state: ChatState) => Partial<ChatState>)) => void;

async function runStream(
  set: SetState,
  start: (
    handlers: { onEvent: (event: string, data: unknown) => void; onClose?: () => void },
    signal: AbortSignal,
  ) => Promise<void>,
  assistantId: string,
): Promise<void> {
  const marks: Partial<Record<StageKey, number>> = { parsing: Date.now() };
  controller = new AbortController();

  const patch = (updater: (turn: AssistantTurn) => AssistantTurn) => {
    set((state) => ({
      turns: state.turns.map((turn) =>
        turn.role === 'assistant' && turn.id === assistantId ? updater(turn) : turn,
      ),
    }));
  };

  const finishStage = (turn: AssistantTurn, key: StageKey): AssistantTurn => ({
    ...turn,
    stages: turn.stages.map((stage) => {
      if (stage.key !== key) return stage;
      const started = marks[key] ?? marks.parsing ?? Date.now();
      return { ...stage, status: 'done', elapsedMs: Date.now() - started };
    }),
  });

  const startStage = (turn: AssistantTurn, key: StageKey): AssistantTurn => {
    marks[key] = Date.now();
    return {
      ...turn,
      stages: turn.stages.map((stage) =>
        stage.key === key && stage.status === 'pending' ? { ...stage, status: 'running' } : stage,
      ),
    };
  };

  const skipRemaining = (turn: AssistantTurn, from: StageKey): AssistantTurn => {
    const index = STAGE_SEQUENCE.indexOf(from);
    return {
      ...turn,
      stages: turn.stages.map((stage, position) =>
        position >= index && stage.status !== 'done'
          ? { ...stage, status: 'skipped' as const }
          : stage,
      ),
    };
  };

  try {
    await start(
      {
        onEvent: (event, data) => {
          const payload = data as never;
          patch((turn) => {
            switch (event) {
              case 'status': {
                const stage = (payload as { stage?: string }).stage;
                if (stage === 'parsing') return startStage(turn, 'parsing');
                if (stage === 'executing') {
                  const next = finishStage(finishStage(turn, 'parsing'), 'generating');
                  return startStage(next, 'executing');
                }
                return turn;
              }
              case 'slot':
                set({ slot: payload as Slot });
                return finishStage({ ...turn, slot: payload as Slot }, 'parsing');
              case 'code': {
                const code = payload as CodePayload;
                const next = finishStage(turn, 'generating');
                return {
                  ...next,
                  code,
                  codeHistory: [...turn.codeHistory, code],
                  question: turn.question,
                };
              }
              case 'table': {
                const next = finishStage(turn, 'executing');
                return startStage({ ...next, table: payload as TablePayload }, 'chart');
              }
              case 'chart': {
                const next = finishStage(turn, 'chart');
                return startStage({ ...next, chart: payload as ChartPayload }, 'narrative');
              }
              case 'narrative': {
                const next = finishStage(turn, 'narrative');
                return { ...next, narrative: payload as NarrativePayload };
              }
              case 'meta':
                return { ...turn, meta: payload as MetaPayload };
              case 'clarify': {
                const clarify = payload as ClarifyPayload;
                const next = skipRemaining(turn, 'generating');
                return { ...next, status: 'clarify', clarify, slot: clarify.partial_slot ?? turn.slot };
              }
              case 'error': {
                const error = payload as ErrorPayload;
                const next = skipRemaining(turn, 'parsing');
                return {
                  ...next,
                  error,
                  status: error.code === 'E_OUT_OF_SCOPE' ? 'refused' : 'failed',
                };
              }
              case 'done': {
                const recordId = (payload as { record_id: string | null }).record_id;
                const finished = turn.stages.map((stage) =>
                  stage.status === 'running'
                    ? {
                        ...stage,
                        status: 'done' as const,
                        elapsedMs: Date.now() - (marks[stage.key] ?? marks.parsing ?? Date.now()),
                      }
                    : stage,
                );
                const status: TurnStatus = turn.error
                  ? turn.status
                  : turn.status === 'clarify'
                    ? 'clarify'
                    : 'success';
                return { ...turn, stages: finished, recordId, status };
              }
              default:
                return turn;
            }
          });
        },
      },
      controller.signal,
    );
  } catch (error) {
    if ((error as Error)?.name === 'AbortError') {
      // 用户主动停止：保留已生成内容，标注已取消（UIUX 8.3）
      patch((turn) => ({ ...turn, status: 'canceled' }));
    } else {
      const apiError = error instanceof ApiError ? error : null;
      patch((turn) =>
        skipRemaining(
          {
            ...turn,
            status: 'failed',
            error: {
              code: apiError?.code ?? 'E_INTERNAL',
              message: apiError?.message ?? '分析请求失败，请稍后重试',
              detail: apiError?.requestId,
            },
          },
          'parsing',
        ),
      );
      if (apiError && presentationOf(apiError.code).severity === 'toast') {
        toast.error(apiError.message, apiError.requestId);
      }
    }
  } finally {
    controller = null;
    set({ streaming: false });
  }
}

/** 右栏展示用：最后一条"有结果"的助手消息 */
export function lastResultTurn(turns: Turn[]): AssistantTurn | null {
  for (let index = turns.length - 1; index >= 0; index -= 1) {
    const turn = turns[index];
    if (turn.role === 'assistant' && (turn.table || turn.chart || turn.status === 'streaming')) {
      return turn;
    }
  }
  return null;
}
