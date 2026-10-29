/**
 * 页面截图验收工具（无头 Edge + CDP 自动化）。
 *
 * 与 `svg2png.py` 同源思路：调用本机 Chromium 内核浏览器无头截图，不引入渲染库。
 * 区别是这里还要**驱动交互**（登录 → 点示例问题 → 等 SSE 出图），因此走 CDP
 * （Chrome DevTools Protocol）：`--remote-debugging-port` + WebSocket 直连，
 * 不依赖 puppeteer（避免为一次截图下载整套 Chromium）。
 *
 * 用法（前后端都在跑的前提下）：
 *     node 01.code/tools/ui_shot.mjs [--base http://127.0.0.1:5173] [--out 02.data/tmp/ui_shots]
 *
 * 产出（8 张）：login.png / workbench-empty.png / workbench-result.png / workbench-dark.png /
 *              reports.png / eval.png / eval-metrics.png / profile.png
 * 用途：前端批次的目视验收凭证（UIUX 各页面的落地效果）。
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

const args = process.argv.slice(2);
const argOf = (name, fallback) => {
  const index = args.indexOf(name);
  return index >= 0 && args[index + 1] ? args[index + 1] : fallback;
};

const BASE = argOf('--base', 'http://127.0.0.1:5173');
const OUT_DIR = path.resolve(argOf('--out', '02.data/tmp/ui_shots'));
const API = `${BASE}/api/v1`;
/**
 * 调试端口：**每次运行随机取**，原因有二——
 * ① 避开 Windows 的保留端口段（本机 `netsh interface ipv4 show excludedportrange` 显示
 *    9329–9428 等段被 Hyper-V 预留，绑定会报 WSAEACCES 0x271D，DevTools 直接起不来）；
 * ② `ui_flow_check.mjs` 用同一套机制：两者端口相同的话，并行运行会**互相抢同一个浏览器实例**
 *    （表现为断言莫名其妙失败：弹窗找不到、退出登录时页面停在别处），随机端口可彻底避免。
 */
const PORT = 9700 + Math.floor(Math.random() * 40);

/**
 * **与 `ui_flow_check.mjs` 互斥**：随机端口只解决"抢浏览器"，解决不了"抢数据"——
 * 两个脚本都往同一个账号（`demo_ui`）里建/删会话、发提问、跑评测，
 * 并行时对方的动作会打乱本脚本的截图内容（如侧栏多出会话、评测页多一个批次）。
 * 因此用锁文件强制串行；第二个启动者直接退出并提示。
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
const VIEWPORT = { width: 1440, height: 900 };

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function findBrowser() {
  return BROWSERS.find((candidate) => existsSync(candidate)) ?? null;
}

// ── 极简 CDP 客户端 ────────────────────────────────────────────────────────

class CDP {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    ws.addEventListener('message', (event) => {
      const message = JSON.parse(event.data);
      if (message.id && this.pending.has(message.id)) {
        const { resolve, reject } = this.pending.get(message.id);
        this.pending.delete(message.id);
        message.error ? reject(new Error(JSON.stringify(message.error))) : resolve(message.result);
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
    const result = await this.send('Runtime.evaluate', {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    return result?.result?.value;
  }

  async goto(url, settleMs = 2500) {
    await this.send('Page.navigate', { url });
    await sleep(settleMs);
  }

  async screenshot(fileName) {
    // 主题切换（`data-theme` → CSS 变量）后第一帧可能是**陈旧合成帧**（实测：DOM 已是暗色，
    // 截图却仍是亮色）→ 先丢弃一帧、再取一帧，保证拿到重绘后的画面。
    await this.send('Page.captureScreenshot', { format: 'png' });
    await sleep(400);
    const { data } = await this.send('Page.captureScreenshot', { format: 'png' });
    const target = path.join(OUT_DIR, fileName);
    writeFileSync(target, Buffer.from(data, 'base64'));
    console.log(`  [shot] ${fileName}`);
  }
}

async function connect() {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${PORT}/json/list`);
      const targets = await response.json();
      const page = targets.find((item) => item.type === 'page');
      if (page?.webSocketDebuggerUrl) return page.webSocketDebuggerUrl;
    } catch {
      /* 浏览器还没起来 */
    }
    await sleep(250);
  }
  throw new Error('无法连接 CDP（浏览器未就绪）');
}

async function login() {
  const payload = { username: 'demo_ui', password: 'demo12345' };
  let response = await fetch(`${API}/auth/register`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  let body = await response.json();
  if (body.code !== 0) {
    response = await fetch(`${API}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    body = await response.json();
  }
  if (body.code !== 0) throw new Error(`登录失败：${body.message}`);
  return body.data.token;
}

async function main() {
  acquireLock('ui_shot');
  const browser = findBrowser();
  if (!browser) throw new Error('未找到 Edge / Chrome');
  mkdirSync(OUT_DIR, { recursive: true });

  const profile = mkdtempSync(path.join(tmpdir(), 'ui_shot_'));
  const child = spawn(
    browser,
    [
      '--headless=new',
      '--disable-gpu',
      '--hide-scrollbars',
      '--no-first-run',
      '--no-default-browser-check',
      `--remote-debugging-port=${PORT}`,
      // Chrome 111+ 会校验 DevTools WS 的 Origin，非浏览器客户端需显式放行
      '--remote-allow-origins=*',
      `--user-data-dir=${profile}`,
      `--window-size=${VIEWPORT.width},${VIEWPORT.height}`,
      'about:blank',
    ],
    { stdio: 'ignore' },
  );

  try {
    const wsUrl = await connect();
    const ws = new WebSocket(wsUrl);
    await new Promise((resolve) => ws.addEventListener('open', resolve));
    const cdp = new CDP(ws);
    await cdp.send('Page.enable');
    await cdp.send('Runtime.enable');
    // 不调 `Emulation.setDeviceMetricsOverride`：视口交给 `--window-size`，
    // 度量覆盖会切到另一条合成路径，实测截出来的画面与真实 DOM 不一致。

    const token = await login();
    console.log('登录成功，开始截图…');

    // ① 登录页
    await cdp.goto(`${BASE}/login`, 2000);
    await cdp.screenshot('login.png');

    // ② 注入 token 后的工作台（空会话态）
    await cdp.evaluate(
      `localStorage.setItem('zdxb.token', ${JSON.stringify(token)});
       localStorage.setItem('zdxb.theme', 'light');`,
    );
    await cdp.goto(`${BASE}/`, 3500);
    await cdp.screenshot('workbench-empty.png');

    // ③ 新建会话（示例问题只在空会话里展示）→ 点示例问题 → 等 SSE 出图表与表格
    const created = await cdp.evaluate(`(() => {
      const target = [...document.querySelectorAll('button')]
        .find((item) => item.textContent.trim() === '新建会话');
      if (!target) return 'not-found';
      target.click();
      return 'clicked';
    })()`);
    await sleep(1500);

    const clicked = await cdp.evaluate(`(() => {
      const buttons = [...document.querySelectorAll('button')];
      const target = buttons.find((item) => item.textContent.trim() === '峰段用电占比多少')
        ?? buttons.find((item) => item.textContent.trim() === '全期总用电量是多少');
      if (!target) return 'not-found';
      target.click();
      return 'clicked:' + target.textContent.trim();
    })()`);
    console.log(`  新建会话 = ${created}，点击示例问题 = ${clicked}`);
    await sleep(5000);
    await cdp.screenshot('workbench-result.png');

    // ④ 暗色主题下的同一页面（UIUX 4.2 暗色模式 + 7.2 图表随主题）
    await cdp.evaluate("localStorage.setItem('zdxb.theme', 'dark')");
    await cdp.goto(`${BASE}/`, 3500);
    const themeState = await cdp.evaluate(
      `JSON.stringify({stored: localStorage.getItem('zdxb.theme'),
        applied: document.documentElement.getAttribute('data-theme'),
        bodyBg: getComputedStyle(document.body).backgroundColor})`,
    );
    console.log('  暗色检查 =', themeState);
    await cdp.screenshot('workbench-dark.png');

    // ⑤ 第二批三页（报表管理 / 评测中心 / 个人中心）
    await cdp.evaluate("localStorage.setItem('zdxb.theme', 'light')");

    await cdp.goto(`${BASE}/reports`, 3000);
    await cdp.screenshot('reports.png');

    await cdp.goto(`${BASE}/eval`, 3000);
    // 真跑一次 3 题自检（gold 回放，不消耗 token）→ 截图里能看到指标卡与逐题结果
    const evaluStarted = await cdp.evaluate(`(() => {
      const helpers = {
        setInput: (el, value) => {
          Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, value);
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
          el.blur();
        },
      };
      const sampled = [...document.querySelectorAll('.ant-segmented-item-label')]
        .find((n) => n.textContent.trim() === '抽样');
      sampled?.click();
      const numberInput = document.querySelector('.ant-input-number-input');
      if (numberInput) {
        numberInput.focus();
        helpers.setInput(numberInput, '3');
      }
      const start = [...document.querySelectorAll('button')]
        .find((b) => b.textContent.replace(/\\s/g, '') === '开始评测');
      if (!start) return 'no-button';
      start.click();
      // 回读抽样题量，便于在日志里判断"是不是真跑 3 题"
      return 'started, limit=' + (document.querySelector('.ant-input-number-input')?.value ?? '?');
    })()`);
    console.log('  发起自检评测 =', evaluStarted);

    // 等批次真正结束：指标面板出现"平均响应时间"即说明 metrics 已下发（运行期后端不下发）
    let metricsReady = false;
    for (let attempt = 0; attempt < 30 && !metricsReady; attempt += 1) {
      await sleep(2000);
      metricsReady = await cdp.evaluate(
        "document.body.innerText.includes('平均响应时间') && document.body.innerText.includes('bad case 归因')",
      );
    }
    console.log('  指标面板就绪 =', metricsReady ? '是' : '否（超时，截图仍会保存）');
    await cdp.screenshot('eval.png');

    // 滚到指标卡与逐题结果（页面较长，一屏装不下）
    await cdp.evaluate(`(() => {
      const anchor = [...document.querySelectorAll('div')]
        .find((node) => node.children.length === 0 && node.textContent.trim() === '按题型');
      (anchor ?? document.scrollingElement).scrollIntoView?.({ block: 'start' });
      document.scrollingElement.scrollTop -= 120;
    })()`);
    await sleep(800);
    await cdp.screenshot('eval-metrics.png');

    await cdp.goto(`${BASE}/profile`, 3500);
    await cdp.screenshot('profile.png');

    ws.close();
    console.log(`完成，输出目录：${OUT_DIR}`);
  } finally {
    child.kill();
    await sleep(300);
    try {
      rmSync(profile, { recursive: true, force: true });
    } catch {
      /* 浏览器进程可能仍占用 profile，忽略 */
    }
    releaseLock();
  }
}

main().catch((error) => {
  console.error('FAIL:', error.message);
  releaseLock();
  process.exit(1);
});
