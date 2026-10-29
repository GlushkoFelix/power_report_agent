/**
 * 界面流程自检（无头 Edge + CDP）：把"用户实测反馈过的交互缺陷"固化成可重跑的断言。
 *
 * 用法（前后端都在跑的前提下，从仓库根目录执行）：
 *     node 01.code/tools/ui_flow_check.mjs
 *
 * 覆盖的 5 条（每条都对应一次真实缺陷，详见 UIUX 14.9）：
 *  A 空态是否给出上传入口（用户反馈：新建会话后看不到"上传报表"入口）
 *  B 连点「新建会话」是否重复建空会话 → **契约已升级为"草稿态"**：点按钮只开空白页、
 *    发出第一条消息才落库（用户反馈：侧栏堆出一串未命名空会话），用例随之改为
 *    "点击后侧栏不新增 / 连点也不新增"
 *  C 删除会话后再提问，主区是否同步显示（用户反馈：只有侧栏多了会话，主区不动）
 *  D 字段字典是否随报表切换而更新（用户反馈：换了报表，字典还是上一张表的字段）
 *  E 退出登录后重新登录是否回到首页（用户反馈：又被带回退出前那一页）
 *
 * 依赖：后端离线脚本模型（无需 Key）+ 预置报表 `r_daily` / `r_monthly`；
 * 账号 `demo_ui` 由脚本自动注册/登录。**该脚本会真的建/删会话**，请在演示库上跑。
 *
 * 实现注记（踩过的坑，改脚本前先看）：
 *  - antd 会在两个汉字之间插空格（"删 除"/"登 录"）→ 文本匹配前必须去掉空白；
 *  - antd `Dropdown` 默认 **hover 触发**，无头环境要派发 mouseenter 而不是 click；
 *  - 会话行的操作按钮在 hover 才显示（父容器 `display:none`）→ 要派发完整指针序列；
 *  - 本文件里用**模板字符串**拼 evaluate 代码，正则要写 `\\s`（写 `\s` 会被当成转义符）。
 */

import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

const BROWSERS = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
];
/**
 * 前端地址**可用环境变量覆盖**（默认 5173 保持历史口径）：
 *   node 01.code/tools/ui_flow_check.mjs            # → http://127.0.0.1:5173
 *   UI_BASE=http://127.0.0.1:8080 node ...          # → 指定端口
 * 为什么需要（2026-10-13 实测）：Windows 的**保留端口段**（`netsh int ipv4 show excludedportrange`）
 * 会把 5173 圈进去（当前是 5164–5263），此时 Vite 只能换端口（本项目改用 8080）——
 * 端口号不是稳定契约，写死会让脚本"代码没坏但跑不起来"。
 */
const BASE = process.env.UI_BASE || 'http://127.0.0.1:5173';
const API = `${BASE}/api/v1`;
/**
 * 调试端口**每次随机**：既避开 Windows 保留端口段（被 Hyper-V 预留时绑定会报 WSAEACCES 0x271D），
 * 也避免与 `ui_shot.mjs` 抢同一个浏览器实例——两者端口相同时并行跑，断言会莫名其妙失败
 * （弹窗找不到、退出登录时页面停在别处，都是"连到了别人的浏览器"）。
 */
const PORT = 9760 + Math.floor(Math.random() * 40);

/**
 * **与 `ui_shot.mjs` 互斥**：随机端口只解决"抢浏览器"，解决不了"抢数据"——
 * 两个脚本都往同一个账号（`demo_ui`）里建/删会话、发提问、跑评测，
 * 于是"会话数 +1""删完回到空态"这类计数断言会被对方的动作打乱（实测踩到）。
 * 因此用一个锁文件强制串行；第二个启动者直接退出并提示，而不是给出一堆假失败。
 */
const LOCK_FILE = path.resolve('02.data/tmp/_ui_tools.lock');
const LOCK_TTL_MS = 10 * 60 * 1000;

function acquireLock(tool) {
  try {
    const raw = existsSync(LOCK_FILE) ? JSON.parse(readFileSync(LOCK_FILE, 'utf-8')) : null;
    if (raw && Date.now() - Number(raw.at ?? 0) < LOCK_TTL_MS) {
      console.log(
        `检测到 ${raw.tool} 正在运行（PID ${raw.pid}）——两个 UI 脚本都会改同一账号的数据，` +
          '必须串行执行；本次直接退出。',
      );
      process.exit(2);
    }
  } catch {
    /* 锁文件损坏 → 当作无锁 */
  }
  mkdirSync(path.dirname(LOCK_FILE), { recursive: true });
  writeFileSync(LOCK_FILE, JSON.stringify({ tool, pid: process.pid, at: Date.now() }), 'utf-8');
}

function releaseLock() {
  try {
    rmSync(LOCK_FILE, { force: true });
  } catch {
    /* ignore */
  }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let failed = 0;
function check(name, ok, extra = '') {
  if (!ok) failed += 1;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${extra ? `  ← ${extra}` : ''}`);
}

/**
 * 状态归一：删掉演示账号下的**空会话**（`message_count === 0`）。
 *
 * 为什么需要（实测踩到）：应用有一条"**已在空会话中 → 新建会话复用、不重复建**"的保护
 * （问题 2 的修复）。若上一轮跑完留下一条空会话，本轮开头的
 * `after === before + 1` 就会**假失败**（点击被保护吸收，计数不变）。
 * 走接口清理、**不调模型（零 token）**，让活跃会话落到非空那条，断言才与运行顺序无关。
 */
async function sweepEmptyConversations(token) {
  const headers = { Authorization: `Bearer ${token}` };
  const listed = await fetch(`${API}/conversations?page_size=100`, { headers }).then((r) => r.json());
  const items = listed?.data?.items ?? [];
  const empties = items.filter((item) => (item.message_count ?? 0) === 0);
  let removed = 0;
  for (const item of empties) {
    const res = await fetch(`${API}/conversations/${item.conversation_code}`, {
      method: 'DELETE',
      headers,
    });
    if (res.ok) removed += 1;
  }
  return { scanned: items.length, removed };
}

class CDP {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    ws.addEventListener('message', (e) => {
      const m = JSON.parse(e.data);
      if (m.id && this.pending.has(m.id)) {
        const { resolve, reject } = this.pending.get(m.id);
        this.pending.delete(m.id);
        m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result);
      }
    });
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  async evaluate(expression) {
    const r = await this.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    return r?.result?.value;
  }
  async goto(url, settle = 3000) {
    await this.send('Page.navigate', { url });
    await sleep(settle);
  }
}

/** 注入到页面里的 DOM 工具库（返回字符串，供每次 evaluate 复用） */
const HELPERS = `
window.__t = {
  // antd 会在两个汉字之间插空格（"删 除"/"登 录"）→ 匹配前先去掉所有空白
  text: (el) => (el?.textContent || '').replace(/\\s/g, ''),
  clickText: (text, tag = 'button') => {
    const el = [...document.querySelectorAll(tag)].find((n) => window.__t.text(n).includes(text));
    if (!el) return false;
    window.__t.pointer ? window.__t.pointer(el) : el.click();
    return true;
  },
  // 会话行计数：原先靠"每行都有「N 条消息」"来数，但该字段已按用户反馈从列表里去掉
  // → 改为直接数会话行容器（div class 里含 "group relative"，见 ConversationList）
  // 注：本块是模板字符串，注释里**不能用反引号**，否则会截断模板（踩过一次）
  convCount: () => document.querySelectorAll('div[class*="group relative"]').length,
  bodyHas: (text) => (document.body.innerText || '').includes(text),
  setInput: (selector, value) => {
    const el = document.querySelector(selector);
    if (!el) return false;
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement : HTMLInputElement;
    Object.getOwnPropertyDescriptor(proto.prototype, 'value').set.call(el, value);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    return true;
  },
  // 会话行的三个操作按钮（重命名 / 置顶 / 删除）只在 hover 时显示，
  // 且挂在 display:none 的容器里 → 必须逐个派发完整指针序列才会触发 antd 的触发器
  pointer: (el) => {
    ['mouseenter', 'mouseover', 'mousedown', 'mouseup', 'click'].forEach((type) =>
      el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true })),
    );
  },
  // 会话行操作已改为「⋮」下拉（长标题不再被并排按钮遮挡，见 ConversationList.tsx）：
  // ① hover 行（「⋮」仅在 group-hover / 选中时可见）；② 完整指针序列点开「⋮」；
  // ③ 再用 clickMenuText / clickModalOkText 按去空白文本点击菜单项与弹窗确认。
  // 点第一条会话把它选为"当前会话"（草稿态下没有当前会话，用例 C 需要先选中）
  selectFirstRow: () => {
    const rows = [...document.querySelectorAll('div[class*="group relative"]')];
    if (!rows.length) return false;
    window.__t.pointer(rows[0]);
    return true;
  },
  openRowMenu: () => {
    const rows = [...document.querySelectorAll('div[class*="group relative"]')];
    const active = rows.find((r) => (r.getAttribute('style') || '').includes('232, 239, 253')) ?? rows[0];
    if (!active) return 'no-row';
    active.dispatchEvent(new MouseEvent('mouseenter', { bubbles: true, cancelable: true }));
    const more = active.querySelector('button[aria-label="会话操作"]');
    if (!more) return 'no-more-button';
    window.__t.pointer(more);
    return 'opened';
  },
  clickMenuText: (text) => {
    const menu = document.querySelector('.ant-dropdown:not(.ant-dropdown-hidden)');
    if (!menu) return 'no-menu';
    const items = [...menu.querySelectorAll('li')];
    const el = items.find((n) => window.__t.text(n) === text) ?? items.find((n) => window.__t.text(n).includes(text));
    if (!el) return 'no-item:' + JSON.stringify(items.map((n) => window.__t.text(n)));
    window.__t.pointer(el);
    return true;
  },
  clickModalOkText: (text) => {
    const modal = document.querySelector('.ant-modal');
    if (!modal) return 'no-modal';
    const btns = [...modal.querySelectorAll('button')];
    const ok = btns.find((b) => window.__t.text(b) === text) ?? btns.find((b) => window.__t.text(b).includes(text));
    if (!ok) return 'no-ok:' + JSON.stringify(btns.map((b) => window.__t.text(b)));
    window.__t.pointer(ok);
    return true;
  },
  // 用户菜单已从顶栏移到**侧栏底部的账号卡片**（aria-label="用户菜单"），优先用它定位；
  // 兜底分支保留旧的"按用户名找 span"，便于在改版前的页面上调试。
  userMenu: () => document.querySelector('[aria-label="用户菜单"]')
    ?? [...document.querySelectorAll('span')]
      .find((s) => s.children.length && /demo_ui|未登录/.test(s.textContent || '')),
};
'tools-ready'`;

async function main() {
  acquireLock('ui_flow_check');
  const browser = BROWSERS.find((p) => existsSync(p));
  if (!browser) throw new Error('未找到 Edge / Chrome');
  const profile = mkdtempSync(path.join(tmpdir(), 'ui_flow_'));
  const child = spawn(
    browser,
    ['--headless=new', '--disable-gpu', '--hide-scrollbars', '--no-first-run',
     `--remote-debugging-port=${PORT}`, '--remote-allow-origins=*',
     `--user-data-dir=${profile}`, '--window-size=1440,900', 'about:blank'],
    { stdio: 'ignore' },
  );

  try {
    let wsUrl = null;
    for (let i = 0; i < 40 && !wsUrl; i += 1) {
      try {
        const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
        wsUrl = list.find((t) => t.type === 'page')?.webSocketDebuggerUrl ?? null;
      } catch { /* 未就绪 */ }
      if (!wsUrl) await sleep(250);
    }
    const ws = new WebSocket(wsUrl);
    await new Promise((r) => ws.addEventListener('open', r));
    const cdp = new CDP(ws);
    await cdp.send('Page.enable');
    await cdp.send('Runtime.enable');

    // 登录（API 直接拿 token，再注入 localStorage）
    let res = await fetch(`${API}/auth/register`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: 'demo_ui', password: 'demo12345' }),
    });
    let body = await res.json();
    if (body.code !== 0) {
      res = await fetch(`${API}/auth/login`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: 'demo_ui', password: 'demo12345' }),
      });
      body = await res.json();
    }
    const token = body.data.token;

    await cdp.goto(`${BASE}/login`, 2000);
    await cdp.evaluate(
      `localStorage.setItem('zdxb.token', ${JSON.stringify(token)});
       localStorage.setItem('zdxb.theme', 'light');`,
    );
    await cdp.goto(`${BASE}/`, 3500);
    await cdp.evaluate(HELPERS);

    // ── A/B：新建会话是**草稿态**（点了不落库，发出第一条消息才落库）──────────
    // 契约变更（用户反馈）：此前"点了就建"，侧栏会堆出一串「未命名会话 · 0 条消息」。
    // 状态归一仍保留：演示库里的历史空会话会让"发送后 +1"的口径不稳。
    const swept = await sweepEmptyConversations(token);
    console.log(`（状态归一：清理空会话 ${swept.removed}/${swept.scanned} 条）`);
    await cdp.goto(`${BASE}/`, 2500); // 重载以让侧栏反映清理结果
    await cdp.evaluate(HELPERS);

    const before = await cdp.evaluate('window.__t.convCount()');
    const clicked = await cdp.evaluate("window.__t.clickText('新建会话')");
    await sleep(1200);
    await cdp.evaluate(HELPERS);
    const afterCreate = await cdp.evaluate('window.__t.convCount()');
    check('新建会话：点击后侧栏不新增（草稿态，等发出消息才落库）', clicked && afterCreate === before,
      `before=${before} after=${afterCreate}`);

    const emptyState = await cdp.evaluate(
      "JSON.stringify({welcome: window.__t.bodyHas('你好，我是智电析报'), upload: window.__t.bodyHas('上传新报表') || window.__t.bodyHas('上传第一份报表')})",
    );
    const parsedEmpty = JSON.parse(emptyState);
    check('空态出现欢迎语', parsedEmpty.welcome);
    check('空态保留上传入口（问题 4）', parsedEmpty.upload);

    // 连点两次「新建会话」：草稿态天然幂等 → 仍然不应新增
    await cdp.evaluate("window.__t.clickText('新建会话'); window.__t.clickText('新建会话')");
    await sleep(1500);
    await cdp.evaluate(HELPERS);
    const afterDoubleClick = await cdp.evaluate('window.__t.convCount()');
    check('连点「新建会话」仍然不新增（问题 2：不再堆空会话）', afterDoubleClick === before,
      `after=${afterDoubleClick}`);

    // ── C：选中一条 → 删除它 → 空态 → 再提问，主区必须同步 ──────────────────
    // 草稿态下没有"当前会话"，先点第一条把它选为当前（这条用例测的是
    // "删除**当前**会话后主区是否同步"，所以必须先有当前会话）
    const selectedFirst = await cdp.evaluate('window.__t.selectFirstRow()');
    await sleep(2500);
    console.log(`（已选中第一条会话：${selectedFirst}）`);
    // 路径：hover 行 → 点「⋮」→ 点「删除会话」→ Modal 里点「删除」
    const openedMenu = await cdp.evaluate('window.__t.openRowMenu()');
    await sleep(700);
    const deleteItemClicked = await cdp.evaluate("window.__t.clickMenuText('删除会话')");
    await sleep(700);
    const confirmClicked = await cdp.evaluate("window.__t.clickModalOkText('删除')");
    check('会话操作「⋮」下拉可展开', openedMenu === 'opened', openedMenu);
    check('删除确认框弹出（诊断）', deleteItemClicked === true && confirmClicked === true,
      `menu=${deleteItemClicked} / ok=${confirmClicked}`);
    await sleep(1500);
    await cdp.evaluate(HELPERS);
    const afterDelete = await cdp.evaluate(
      "JSON.stringify({count: window.__t.convCount(), welcome: window.__t.bodyHas('你好，我是智电析报')})",
    );
    const parsedDelete = JSON.parse(afterDelete);

    check('删除后回到空态而不是跳别的会话（问题 1 前置）', parsedDelete.welcome && parsedDelete.count === afterCreate - 1,
      `count=${parsedDelete.count}`);

    // 删除后直接提问：主区应出现用户气泡 + 助手卡片（这正是用户反馈"只有侧栏有会话"的场景）
    await cdp.evaluate("window.__t.setInput('textarea', '峰段用电占比多少')");
    await sleep(200);
    await cdp.evaluate("window.__t.clickText('发送')");
    // 轮询等待而不是固定睡 5 秒：「执行信息」是 SSE 收尾才渲染的块，磁盘/冷启动稍慢就会
    // 偶发失败（实测遇到过一次）——最长等 25 秒，出现即继续
    let parsedAsk = { userBubble: false, stages: false, exec: false, count: 0 };
    for (let attempt = 0; attempt < 25; attempt += 1) {
      await sleep(1000);
      await cdp.evaluate(HELPERS);
      parsedAsk = JSON.parse(
        await cdp.evaluate(`JSON.stringify({
          userBubble: window.__t.bodyHas('峰段用电占比多少'),
          stages: window.__t.bodyHas('沙箱执行'),
          exec: window.__t.bodyHas('执行信息'),
          count: window.__t.convCount(),
        })`),
      );
      if (parsedAsk.userBubble && parsedAsk.stages && parsedAsk.exec) break;
    }
    check('删除后提问：主区出现提问气泡（问题 1）', parsedAsk.userBubble);
    // 失败时把现场打出来（视口 / 命中的关键文本 / 页面尾部），避免只看到 true/false 无从下手
    const askDiag = await cdp.evaluate(`JSON.stringify({
      w: window.innerWidth, h: window.innerHeight,
      bodyLen: (document.body.innerText || '').length,
      hits: ['沙箱执行', '执行信息', '关键数值', '结果解读', '生成 SQL', '错误', '停止生成', '正在']
        .filter((t) => (document.body.innerText || '').includes(t)),
      tail: (document.body.innerText || '').slice(-260).replace(/\\n+/g, ' | '),
    })`);
    check('删除后提问：主区出现执行阶段与执行信息', parsedAsk.stages && parsedAsk.exec,
      `stages=${parsedAsk.stages} exec=${parsedAsk.exec} ${askDiag}`);
    // **发首条消息才建会话**（草稿态契约）：建会话 → 重取列表是异步的，这里轮询等它涨上来，
    // 不然会抓到"列表正在刷新"的瞬时态（实测 count=0 假失败）
    let sidebarCount = parsedAsk.count;
    for (let i = 0; i < 12 && sidebarCount !== parsedDelete.count + 1; i += 1) {
      await sleep(350);
      sidebarCount = await cdp.evaluate('window.__t.convCount()');
    }
    check('删除后提问：侧栏新增会话（应比删除后 +1）', sidebarCount === parsedDelete.count + 1,
      `删除后=${parsedDelete.count} 提问后=${sidebarCount}`);

    // ── D：字段字典随报表切换更新（问题 3）───────────────────────────────
    await cdp.evaluate(`(() => {
      const s = document.querySelector('.ant-select-selector');
      s?.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
      s?.click();
    })()`);
    await sleep(800);
    const optionClicked = await cdp.evaluate(`(() => {
      const opt = document.querySelector('.ant-select-item-option[title*="r_monthly"]')
        ?? [...document.querySelectorAll('.ant-select-item-option')].find((o) => o.textContent.includes('r_monthly'));
      if (!opt) return false;
      opt.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
      opt.click();
      return true;
    })()`);
    await sleep(1500);
    await cdp.evaluate(HELPERS);
    await cdp.evaluate("window.__t.clickText('字段字典')");
    await sleep(1500);
    const drawerTitle = await cdp.evaluate(
      "document.querySelector('.ant-drawer-title')?.textContent?.trim() ?? ''",
    );
    check('字段字典随报表切换（问题 3）', optionClicked && drawerTitle.includes('r_monthly'),
      `title=${drawerTitle}`);
    await cdp.evaluate("document.querySelector('.ant-drawer-close')?.click()");
    await sleep(600);

    // ── E：退出登录 → 重新登录应回首页（问题 5）──────────────────────────
    // 账号卡片挂在侧栏底部，且是**点击**触发的 Dropdown（用户反馈：不要悬浮就弹出）
    // → 无头环境里必须派发完整指针序列，只 mouseenter 不会开
    await cdp.evaluate(`(() => {
      const menu = window.__t.userMenu();
      if (menu) ['mouseenter', 'mouseover', 'mousedown', 'mouseup', 'click'].forEach((t) =>
        menu.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true })));
    })()`);
    await sleep(900);
    const loggedOut = await cdp.evaluate(`(() => {
      const item = [...document.querySelectorAll('.ant-dropdown-menu-item')]
        .find((n) => n.textContent.includes('退出登录'));
      if (!item) return 'no-menu';
      item.click();
      return 'clicked';
    })()`);
    await sleep(1800);
    const afterLogout = await cdp.evaluate('JSON.stringify({path: location.pathname, search: location.search})');
    const parsedLogout = JSON.parse(afterLogout);
    // 注：URL 上是否残留 `?next=` 已不影响行为——登录页会按"主动退出"忽略它。
    // 真正的断言是下一步"重新登录落在首页"，这里只确认已经离开工作台。
    check('退出登录离开工作台回到登录页（问题 5）',
      parsedLogout.path === '/login',
      `${loggedOut} → ${afterLogout}`);

    await cdp.evaluate(HELPERS);
    const formDiag = await cdp.evaluate(`(() => {
      window.__t.setInput('#username', 'demo_ui');
      window.__t.setInput('#password', 'demo12345');
      return JSON.stringify({
        hasUser: !!document.querySelector('#username'),
        userValue: document.querySelector('#username')?.value ?? null,
        passValue: document.querySelector('#password')?.value ?? null,
        loginButtons: [...document.querySelectorAll('button')]
          .filter((b) => b.textContent.replace(/\\s/g, '') === '登录').length,
      });
    })()`);
    await sleep(400);
    await cdp.evaluate("window.__t.clickText('登录')");
    await sleep(3500);
    const afterLogin = await cdp.evaluate(`JSON.stringify({
      path: location.pathname,
      search: location.search,
      errors: [...document.querySelectorAll('.ant-form-item-explain-error')].map((n) => n.textContent),
      alert: document.querySelector('.ant-alert-message')?.textContent ?? null,
    })`);
    check('重新登录落在首页（问题 5）', JSON.parse(afterLogin).path === '/',
      `${formDiag} → ${afterLogin}`);

    // ── F. 报表管理页 ────────────────────────────────────────────────────
    await cdp.goto(`${BASE}/reports`, 3500);
    await cdp.evaluate(HELPERS);
    const reportsDiag = await cdp.evaluate(`JSON.stringify({
      hasTitle: window.__t.bodyHas('报表管理'),
      presetRows: window.__t.bodyHas('系统预置'),
      uploadEntry: window.__t.bodyHas('上传报表'),
      disabledDelete: [...document.querySelectorAll('button')]
        .filter((b) => b.textContent.replace(/\\s/g, '') === '删除' && b.disabled).length,
        // 预置 5 张：断言表格行数（每行含报表名 + 编号，用行的选择器最稳）
      rows: document.querySelectorAll('.ant-table-row').length,
    })`);
    const parsedReports = JSON.parse(reportsDiag);
    check('报表管理：预置报表列表渲染', parsedReports.hasTitle && parsedReports.presetRows && parsedReports.rows >= 5,
      reportsDiag);
    check('报表管理：预置报表删除按钮置灰（is_deletable 驱动）', parsedReports.disabledDelete >= 3,
      `disabled=${parsedReports.disabledDelete}`);

    await cdp.evaluate("window.__t.clickText('质量')");
    await sleep(1500);
    const qualityTitle = await cdp.evaluate(
      "document.querySelector('.ant-drawer-title')?.textContent?.trim() ?? ''",
    );
    check('报表管理：质量报告抽屉可打开', qualityTitle.includes('数据质量报告'), `title=${qualityTitle}`);
    await cdp.evaluate("document.querySelector('.ant-drawer-close')?.click()");
    await sleep(600);

    await cdp.evaluate(HELPERS);
    await cdp.evaluate("window.__t.clickText('预览')");
    await sleep(1800);
    const previewTitle = await cdp.evaluate(
      "document.querySelector('.ant-drawer-title')?.textContent?.trim() ?? ''",
    );
    check('报表管理：数据预览抽屉可打开', previewTitle.includes('数据预览'), `title=${previewTitle}`);
    await cdp.evaluate("document.querySelector('.ant-drawer-close')?.click()");
    await sleep(600);

    // ── G. 评测中心 ─────────────────────────────────────────────────────
    await cdp.goto(`${BASE}/eval`, 3500);
    await cdp.evaluate(HELPERS);
    const evalDiag = await cdp.evaluate(`JSON.stringify({
      hasStart: window.__t.bodyHas('发起评测'),
      hasModel: window.__t.bodyHas('温度：0'),
      hasRuns: window.__t.bodyHas('历史批次'),
      runRows: document.querySelectorAll('.ant-table-row').length,
    })`);
    const parsedEval = JSON.parse(evalDiag);
    check('评测中心：发起面板与模型信息（只读）', parsedEval.hasStart && parsedEval.hasModel, evalDiag);
    check('评测中心：历史批次列表有数据（截图脚本跑过自检批次）', parsedEval.hasRuns && parsedEval.runRows >= 1,
      `runRows=${parsedEval.runRows}`);

    // ── H. 个人中心 ─────────────────────────────────────────────────────
    await cdp.goto(`${BASE}/profile`, 4000);
    await cdp.evaluate(HELPERS);
    const profileDiag = await cdp.evaluate(`JSON.stringify({
      hasUser: window.__t.bodyHas('demo_ui'),
      hasStats: window.__t.bodyHas('我的数据统计') && window.__t.bodyHas('近 7 天分析次数'),
      hasRecords: window.__t.bodyHas('我的分析记录'),
      disabledDanger: [...document.querySelectorAll('button')]
        .filter((b) => b.disabled && /清空历史记录|删除账号|修改密码/.test(b.textContent)).length,
    })`);
    const parsedProfile = JSON.parse(profileDiag);
    // 两栏底部对齐（v1.14.3）：左栏"危险区"卡与右栏"我的分析记录"卡底边应齐平
    const alignDiag = await cdp.evaluate(`JSON.stringify((() => {
      const cards = [...document.querySelectorAll('.ant-card')];
      const byTitle = (re) => cards.find((c) => re.test(c.querySelector('.ant-card-head-title')?.textContent || ''));
      const danger = byTitle(/危险区/);
      const records = byTitle(/我的分析记录/);
      if (!danger || !records) return { found: false };
      const card = records.getBoundingClientRect();
      const pagination = records.querySelector('.ant-pagination')?.getBoundingClientRect() ?? null;
      return {
        found: true,
        dangerBottom: Math.round(danger.getBoundingClientRect().bottom),
        recordsBottom: Math.round(card.bottom),
        paginationFromBottom: pagination ? Math.round(card.bottom - pagination.bottom) : null,
      };
    })())`);
    const parsedAlign = JSON.parse(alignDiag);
    check('个人中心：右栏表格卡与左栏危险区底部对齐（±2px）',
      parsedAlign.found && Math.abs(parsedAlign.dangerBottom - parsedAlign.recordsBottom) <= 2
        && parsedAlign.paginationFromBottom !== null && parsedAlign.paginationFromBottom <= 45,
      alignDiag);
    check('个人中心：账号信息与统计渲染', parsedProfile.hasUser && parsedProfile.hasStats, profileDiag);

    // **契约升级（v1.19.12）**：个人中心四件事的接口已全部落地（此前是"按钮置灰 + 后续版本"）。
    // 断言随之从"必须置灰"改为"**必须可用，且危险操作有二次确认**"——
    // 旧断言（`disabledDanger >= 3`）在这版必挂，因为它钉的是已修复的缺口。
    const entryDiag = await cdp.evaluate(`JSON.stringify((() => {
      const norm = (s) => (s || '').replace(/\\s/g, '');
      const btn = (label) =>
        [...document.querySelectorAll('button')].find((b) => norm(b.textContent) === label) || null;
      const want = ['修改密码', '编辑资料', '清空历史记录', '删除账号'];
      return {
        present: want.filter((label) => btn(label)).length,
        disabled: want.filter((label) => btn(label)?.disabled).length,
      };
    })())`);
    const parsedEntry = JSON.parse(entryDiag);
    check('个人中心：改密 / 改资料 / 清空 / 删号四个入口齐备且可用（接口已闭环）',
      parsedEntry.present === 4 && parsedEntry.disabled === 0, entryDiag);

    /** 点开某个入口的弹窗 → 读诊断信息 → 取消关闭（**绝不提交**，以免动演示数据） */
    const openDialog = async (label, diagScript) => {
      const clicked = await cdp.evaluate(`(() => {
        const norm = (s) => (s || '').replace(/\\s/g, '');
        const btn = [...document.querySelectorAll('button')].find((b) => norm(b.textContent) === '${label}');
        if (!btn) return 'no-button';
        window.__t.pointer(btn);
        return 'clicked';
      })()`);
      await sleep(900);
      const diag = await cdp.evaluate(`JSON.stringify(${diagScript})`);
      await cdp.evaluate(`(() => {
        const norm = (s) => (s || '').replace(/\\s/g, '');
        const cancel = [...document.querySelectorAll('.ant-modal-footer button')]
          .find((b) => norm(b.textContent) === '取消');
        if (cancel) window.__t.pointer(cancel);
        return cancel ? 'closed' : 'no-cancel';
      })()`);
      await sleep(600);
      return { clicked, diag };
    };

    // ① 修改密码：三个密码框（当前 / 新 / 确认），且不给"用户名"这类越权字段
    const pwdDialog = await openDialog('修改密码', `({
      title: document.querySelector('.ant-modal-title')?.textContent?.trim() ?? '',
      passwordInputs: document.querySelectorAll('.ant-modal-body input[type=password]').length,
      hasTokenNote: (document.querySelector('.ant-modal-body')?.textContent || '').includes('不会自动退出'),
    })`);
    const parsedPwd = JSON.parse(pwdDialog.diag);
    check('个人中心：修改密码弹窗（当前 + 新 + 确认三个密码框，并如实说明 token 不回收）',
      pwdDialog.clicked === 'clicked' && parsedPwd.title.includes('修改密码')
        && parsedPwd.passwordInputs >= 3 && parsedPwd.hasTokenNote,
      pwdDialog.diag);

    // ② 编辑资料：用户名只读 + 显示名 / 邮箱可改
    const profileDialog = await openDialog('编辑资料', `({
      title: document.querySelector('.ant-modal-title')?.textContent?.trim() ?? '',
      usernameDisabled: [...document.querySelectorAll('.ant-modal-body input')]
        .some((i) => i.disabled && i.value === 'demo_ui'),
      labels: [...document.querySelectorAll('.ant-modal-body label')].map((l) => l.textContent.trim()),
    })`);
    const parsedProfileDialog = JSON.parse(profileDialog.diag);
    check('个人中心：编辑资料弹窗（用户名只读，显示名 / 邮箱可改）',
      profileDialog.clicked === 'clicked' && parsedProfileDialog.title.includes('编辑资料')
        && parsedProfileDialog.usernameDisabled && parsedProfileDialog.labels.length >= 2,
      profileDialog.diag);

    // ③ 清空历史记录：必须先选"清到什么程度"（两种口径），并提示不可撤销
    const clearDialog = await openDialog('清空历史记录', `({
      title: document.querySelector('.ant-modal-title')?.textContent?.trim() ?? '',
      scopes: document.querySelectorAll('.ant-modal-body .ant-radio-wrapper').length,
      irreversible: (document.querySelector('.ant-modal-body')?.textContent || '').includes('不可撤销'),
    })`);
    const parsedClear = JSON.parse(clearDialog.diag);
    check('个人中心：清空历史弹窗（两种口径 + 不可撤销警示）',
      clearDialog.clicked === 'clicked' && parsedClear.title.includes('清空历史记录')
        && parsedClear.scopes === 2 && parsedClear.irreversible,
      clearDialog.diag);

    // ④ 删除账号：密码 + 用户名双确认，且**未勾选确认时主按钮禁用**
    const deleteDialog = await openDialog('删除账号', `({
      title: document.querySelector('.ant-modal-title')?.textContent?.trim() ?? '',
      needsUsername: (document.querySelector('.ant-modal-body')?.textContent || '').includes('输入用户名以确认'),
      typedConfirm: (document.querySelector('.ant-modal-body')?.textContent || '').includes('永久删除'),
      okDisabled: [...document.querySelectorAll('.ant-modal-footer button')]
        .filter((b) => (b.textContent || '').includes('永久删除账号'))
        .every((b) => b.disabled),
    })`);
    const parsedDeleteAccount = JSON.parse(deleteDialog.diag);
    check('个人中心：删除账号弹窗（密码 + 用户名双确认，未勾选时主按钮禁用）',
      deleteDialog.clicked === 'clicked' && parsedDeleteAccount.title.includes('删除账号')
        && parsedDeleteAccount.needsUsername && parsedDeleteAccount.okDisabled,
      deleteDialog.diag);

    const openedDetail = await cdp.evaluate(`(() => {
      const view = [...document.querySelectorAll('button')]
        .find((b) => b.textContent.replace(/\\s/g, '') === '查看');
      if (!view) return 'no-view-button';
      window.__t.pointer(view);
      return 'clicked';
    })()`);
    await sleep(2200);
    const detailTitle = await cdp.evaluate(
      "document.querySelector('.ant-drawer-title')?.textContent?.trim() ?? ''",
    );
    check('个人中心：记录详情抽屉（查看）可用', openedDetail === 'clicked' && detailTitle.includes('记录详情'),
      `${openedDetail} / title=${detailTitle}`);

    // 个人中心**不放**模型/密钥内容（决策：见 PRD 6.5——密钥属部署级配置，页面既不填写也不展示）
    const profileHasKeyBlock = await cdp.evaluate(`JSON.stringify({
      hasKeyCard: [...document.querySelectorAll('.ant-card')]
        .some((node) => /模型与密钥|API Key|密钥来源/.test(node.textContent)),
      hasKeyInput: [...document.querySelectorAll('input')]
        .some((node) => /api[_-]?key|密钥/i.test((node.placeholder || '') + (node.name || ''))),
    })`);
    const parsedKeyBlock = JSON.parse(profileHasKeyBlock);
    check('个人中心：不含模型/密钥填写或展示入口（决策：部署级配置）',
      !parsedKeyBlock.hasKeyCard && !parsedKeyBlock.hasKeyInput, profileHasKeyBlock);

    await cdp.evaluate("document.querySelector('.ant-drawer-close')?.click()");
    await sleep(500);

    // ── I. 全局导航（v1.14.1 修复：进得去也要回得来）──────────────────────
    const navDiag = await cdp.evaluate(`JSON.stringify({
      entries: [...document.querySelectorAll('nav button')].map((b) => b.textContent.trim()),
      active: document.querySelector('nav button[aria-current="page"]')?.textContent?.trim() ?? null,
    })`);
    const parsedNav = JSON.parse(navDiag);
    check('导航：顶栏有四个入口且高亮当前页', parsedNav.entries.length === 4 && parsedNav.active === '个人中心',
      navDiag);

    // 个人中心 → 对话工作台（用户反馈的"回不了首页"）
    await cdp.evaluate(`(() => {
      const target = [...document.querySelectorAll('nav button')]
        .find((b) => b.textContent.includes('对话工作台'));
      target?.click();
    })()`);
    await sleep(2000);
    const backHome = await cdp.evaluate('location.pathname');
    check('导航：个人中心 → 对话工作台（回首页）', backHome === '/', `path=${backHome}`);

    // 对话工作台 → 评测中心（此前只能手输 URL）
    await cdp.evaluate(`(() => {
      const target = [...document.querySelectorAll('nav button')]
        .find((b) => b.textContent.includes('评测中心'));
      target?.click();
    })()`);
    await sleep(2500);
    const toEval = await cdp.evaluate('location.pathname');
    check('导航：对话工作台 → 评测中心', toEval === '/eval', `path=${toEval}`);

    // Logo 点击回首页
    await cdp.evaluate(`(() => {
      const logo = [...document.querySelectorAll('header > div')]
        .find((node) => node.textContent.includes('智电析报'));
      logo?.click();
    })()`);
    await sleep(2000);
    const logoHome = await cdp.evaluate('location.pathname');
    check('导航：Logo 点击回首页', logoHome === '/', `path=${logoHome}`);

    ws.close();
    console.log(failed === 0 ? '\n全部通过' : `\n失败 ${failed} 项`);
    process.exitCode = failed === 0 ? 0 : 1;
  } finally {
    child.kill();
    await sleep(300);
    try { rmSync(profile, { recursive: true, force: true }); } catch { /* 占用忽略 */ }
    releaseLock();
  }
}

main().catch((e) => {
  console.error('FAIL:', e.message);
  releaseLock();
  process.exit(1);
});
