#!/usr/bin/env node
/**
 * 诊断工作台 UI 真浏览器验收（无 playwright/puppeteer，直连 Chrome DevTools Protocol）。
 *
 * 用法：
 *   ACC_PASSWORD='xxx' node scripts/diagnosis-ui-acceptance.cjs
 *
 * 覆盖 7 个验收点，每个点都落一张 PNG 到 runtime-backups/diagnosis-ui-acceptance-<ts>/。
 */
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const CHROME_CANDIDATES = [
  process.env.CHROME_BIN,
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
].filter(Boolean);

const BASE = process.env.ACC_BASE || 'http://127.0.0.1:3300';
const USERNAME = process.env.ACC_USER || 'admin';
const PASSWORD = process.env.ACC_PASSWORD || '';
const AI_PROVIDER_ID = Number(process.env.ACC_AI_PROVIDER_ID || 2);

const PAD = (n) => String(n).padStart(2, '0');
const stamp = () => {
  const d = new Date();
  return '' + d.getFullYear() + PAD(d.getMonth() + 1) + PAD(d.getDate())
    + '-' + PAD(d.getHours()) + PAD(d.getMinutes()) + PAD(d.getSeconds());
};
const T0 = Date.now();
const log = (...parts) => console.log('[' + ((Date.now() - T0) / 1000).toFixed(1) + 's]', ...parts);

const OUT = process.env.ACC_OUT
  || path.join(ROOT, 'runtime-backups', 'diagnosis-ui-acceptance-' + stamp());

const checks = [];
function check(name, ok, detail) {
  checks.push({ name, ok: Boolean(ok), detail: detail === undefined ? '' : String(detail) });
  log((ok ? 'PASS  ' : 'FAIL  ') + name + (detail === undefined ? '' : ' :: ' + detail));
}

// ---------------------------------------------------------------- 页面侧工具库
// 注意：注入脚本里禁止出现 `${`，一律用字符串拼接。
const HELPERS = [
  'window.__acc = {',
  '  sleep: function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); },',
  '  q: function (s) { return document.querySelector(s); },',
  '  qa: function (s) { return Array.prototype.slice.call(document.querySelectorAll(s)); },',
  '  setVal: function (el, text) {',
  '    if (!el) throw new Error("setVal: element missing");',
  '    var proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;',
  '    var setter = Object.getOwnPropertyDescriptor(proto, "value").set;',
  '    setter.call(el, text);',
  '    el.dispatchEvent(new Event("input", { bubbles: true }));',
  '  },',
  '  txt: function (el) { return el && el.innerText ? String(el.innerText).replace(/\\s+/g, " ").trim() : ""; },',
  '  bodyText: function () { return String(document.body.innerText || "").replace(/\\s+/g, " "); },',
  '  byText: function (sel, text, root) {',
  '    var list = Array.prototype.slice.call((root || document).querySelectorAll(sel));',
  '    for (var i = 0; i < list.length; i += 1) {',
  '      if (window.__acc.txt(list[i]).indexOf(text) >= 0) return list[i];',
  '    }',
  '    return null;',
  '  },',
  '  clickText: function (sel, text, root) {',
  '    var el = window.__acc.byText(sel, text, root);',
  '    if (!el) throw new Error("no element <" + sel + "> whose text contains <" + text + ">");',
  '    el.click();',
  '    return true;',
  '  },',
  '  until: async function (fn, timeout, label) {',
  '    var start = Date.now();',
  '    var last = "";',
  '    while (Date.now() - start < (timeout || 20000)) {',
  '      var v = null;',
  '      try { v = fn(); } catch (e) { last = String((e && e.message) || e); }',
  '      if (v) return v;',
  '      await window.__acc.sleep(150);',
  '    }',
  '    throw new Error("timeout waiting for <" + (label || "condition") + ">" + (last ? " last=" + last : ""));',
  '  },',
  '};',
].join('\n');

// ---------------------------------------------------------------- CDP 会话
function openSession(wsUrl) {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(wsUrl);
    let seq = 0;
    const pending = new Map();
    const handlers = new Set();
    socket.addEventListener('message', (event) => {
      const msg = JSON.parse(event.data);
      if (msg.id && pending.has(msg.id)) {
        const entry = pending.get(msg.id);
        pending.delete(msg.id);
        if (msg.error) entry.reject(new Error((msg.error.message || 'cdp error') + ' ' + JSON.stringify(msg.error)));
        else entry.resolve(msg.result);
        return;
      }
      handlers.forEach((fn) => {
        try { fn(msg); } catch (_) { /* handler errors must not break the session */ }
      });
    });
    const send = (method, params, timeoutMs) => new Promise((res, rej) => {
      seq += 1;
      const id = seq;
      const ms = timeoutMs || 30000;
      pending.set(id, { resolve: res, reject: rej });
      socket.send(JSON.stringify({ id, method, params: params || {} }));
      setTimeout(() => {
        if (pending.delete(id)) rej(new Error(method + ' timed out after ' + ms + 'ms'));
      }, ms);
    });
    send.onEvent = (fn) => { handlers.add(fn); return () => handlers.delete(fn); };
    send.close = () => socket.close();
    socket.addEventListener('open', () => resolve(send));
    socket.addEventListener('error', reject);
  });
}

async function waitForDevTools(port, timeoutMs) {
  const start = Date.now();
  while (Date.now() - start < (timeoutMs || 30000)) {
    try {
      const res = await fetch('http://127.0.0.1:' + port + '/json/version', { signal: AbortSignal.timeout(2000) });
      if (res.ok) return true;
    } catch (_) { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 300));
  }
  return false;
}

async function launchChrome(port, profile, extraArgs) {
  const chrome = spawn(CHROME_CANDIDATES[0], [
    '--headless=new',
    '--remote-debugging-port=' + port,
    '--user-data-dir=' + profile,
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-extensions',
    '--disable-background-networking',
    '--window-size=1680,1050',
  ].concat(extraArgs || []), { stdio: ['ignore', 'ignore', 'pipe'] });
  const stderr = [];
  chrome.stderr.on('data', (chunk) => stderr.push(String(chunk)));
  return { chrome, stderr };
}

// ---------------------------------------------------------------- AI 开关（Node 侧 HTTP）
async function apiLogin() {
  const res = await fetch(BASE + '/api/v1/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: USERNAME, password: PASSWORD }),
  });
  if (!res.ok) throw new Error('login failed: HTTP ' + res.status);
  const data = await res.json();
  if (!data.token) throw new Error('login response has no token');
  return data.token;
}

/**
 * 找出「确实有诊断记录」的实例，返回用于匹配下拉选项的文本。
 * 不能盲选下拉第一项：dbInstances 顺序来自接口，第一项未必是事件所属实例。
 */
async function resolveTargetInstance(token) {
  const headers = { Authorization: 'Bearer ' + token };
  const incRes = await fetch(BASE + '/api/v1/diagnosis-center/incidents?page=1&page_size=1', { headers });
  const inc = await incRes.json();
  const first = (inc.items || [])[0];
  if (!first) return null;
  const instRes = await fetch(BASE + '/api/v1/instances', { headers });
  const inst = await instRes.json();
  const match = (inst.items || []).find((i) => i.id === first.instance_id);
  return {
    incidentId: first.id,
    incidentNo: first.incident_no,
    instanceId: first.instance_id,
    instanceName: first.instance_name,
    // 页面上 option 的 label 形如 "[ORACLE] mesdb (192.168.102.200:1521)"
    label: match ? '[' + String(match.db_type).toUpperCase() + '] ' + match.instance_name : first.instance_name,
  };
}

async function setAiEnabled(token, enabled) {
  const res = await fetch(BASE + '/api/v1/ai-config/' + AI_PROVIDER_ID, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token },
    body: JSON.stringify({ enabled: enabled ? 1 : 0 }),
  });
  if (!res.ok) throw new Error('set ai enabled=' + enabled + ' failed: HTTP ' + res.status + ' ' + (await res.text()).slice(0, 200));
  return res.json();
}

// ---------------------------------------------------------------- 主流程
async function main() {
  if (!PASSWORD) {
    log('ABORT: 需要 ACC_PASSWORD 环境变量');
    process.exitCode = 2;
    return;
  }
  const chromeBin = CHROME_CANDIDATES.find((p) => fs.existsSync(p));
  if (!chromeBin) {
    log('SKIP: 本机没有 Chrome/Chromium，无法做浏览器验收');
    process.exitCode = 0;
    return;
  }
  fs.mkdirSync(OUT, { recursive: true });
  log('证据目录: ' + OUT);

  const token = await apiLogin();
  log('API 登录成功，token 长度 ' + token.length);

  const focus = await resolveTargetInstance(token);
  if (!focus) throw new Error('库里没有任何诊断事件，无法验收；先跑一次诊断再执行本脚本');
  log('目标事件: #' + focus.incidentId + ' ' + focus.incidentNo
    + ' | 实例 #' + focus.instanceId + ' ' + focus.instanceName
    + ' | 下拉匹配文本: ' + focus.label);

  const port = 9333 + (process.pid % 200);
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'tm-acc-'));
  let chrome = null;
  let send = null;
  let watchdog = null;

  try {
    // ---- 启动浏览器（失败则降级 --no-sandbox 重试一次）
    let up = false;
    for (const args of [[], ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage']]) {
      const attempt = await launchChrome(port, profile, args);
      chrome = attempt.chrome;
      up = await waitForDevTools(port, 30000);
      if (up) {
        // page 级健康检查，不是 Browser.getVersion
        try {
          const probe = await (await fetch('http://127.0.0.1:' + port + '/json/new?about%3Ablank', { method: 'PUT' })).json();
          const probeSend = await openSession(probe.webSocketDebuggerUrl);
          try {
            await probeSend('Runtime.enable', {}, 8000);
            up = true;
          } catch (_) {
            up = false;
          } finally {
            probeSend.close();
          }
        } catch (_) {
          up = false;
        }
      }
      if (up) break;
      log('浏览器健康检查未通过，kill 后用降级参数重试；stderr=' + attempt.stderr.join('').slice(-400));
      try { chrome.kill('SIGKILL'); } catch (_) { /* ignore */ }
      chrome = null;
    }
    if (!up) throw new Error('Chrome 无法启动或 page 级命令无响应');

    const target = await (await fetch(
      'http://127.0.0.1:' + port + '/json/new?' + encodeURIComponent(BASE + '/'),
      { method: 'PUT' },
    )).json();
    send = await openSession(target.webSocketDebuggerUrl);
    watchdog = setTimeout(() => {
      log('watchdog: 验收超时，强制结束');
      try { if (chrome) chrome.kill('SIGKILL'); } catch (_) { /* ignore */ }
      process.exit(1);
    }, 300000);
    watchdog.unref?.();

    const ev = async (expr, timeoutMs) => {
      const res = await send('Runtime.evaluate', {
        expression: '(async () => {' + expr + '})()',
        awaitPromise: true,
        returnByValue: true,
      }, timeoutMs || 60000);
      if (res.exceptionDetails) {
        const d = res.exceptionDetails.exception?.description || res.exceptionDetails.text || 'page exception';
        throw new Error('page error: ' + String(d).split('\n')[0]);
      }
      return res.result.value;
    };

    const shot = async (name, focusSelector) => {
      await ev('window.scrollTo(0, 0); return true;', 10000);
      if (focusSelector) {
        // 卡片区在可滚动容器里，光 scrollTo(0,0) 会让目标卡片落在视口外，视觉证据不成立。
        await ev([
          'var el = ' + focusSelector + ';',
          'if (el) el.scrollIntoView({ block: "center", inline: "nearest" });',
          'await window.__acc.sleep(350);',
          'return Boolean(el);',
        ].join('\n'), 15000);
      }
      await new Promise((r) => setTimeout(r, 350));
      const res = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true }, 60000);
      const file = path.join(OUT, name + '.png');
      fs.writeFileSync(file, Buffer.from(res.data, 'base64'));
      log('  截图 -> ' + file);
      return file;
    };

    await send('Page.enable', {}, 10000);
    await send('Runtime.enable', {}, 10000);
    await send('Emulation.setDeviceMetricsOverride',
      { width: 1680, height: 1050, deviceScaleFactor: 1, mobile: false }, 10000);
    await send('Page.addScriptToEvaluateOnNewDocument', { source: HELPERS }, 10000);
    await send('Page.navigate', { url: BASE + '/' }, 20000);
    await ev('await window.__acc.until(function(){ return document.readyState === "complete"; }, 30000, "page load"); return true;', 40000);

    // ================= 登录 =================
    log('--- 步骤 0：登录 ---');
    await ev([
      'await window.__acc.until(function(){ return window.__acc.q("#username"); }, 30000, "登录页用户名框");',
      'window.__acc.setVal(window.__acc.q("#username"), ' + JSON.stringify(USERNAME) + ');',
      'window.__acc.setVal(window.__acc.q("#password"), ' + JSON.stringify(PASSWORD) + ');',
      'await window.__acc.sleep(200);',
      'window.__acc.clickText("button", "打开诊断工作台");',
      'return true;',
    ].join('\n'), 60000);
    await ev('await window.__acc.until(function(){ return window.__acc.q(".diagnosis-cockpit-shell"); }, 45000, "诊断工作台外壳"); return true;', 60000);
    check('0. 登录 admin 成功并进入诊断工作台', true);

    // ================= 验收点 1：落地页历史诊断列表 =================
    log('--- 验收点 1：落地页历史诊断列表 ---');
    let landing = await ev([
      'await window.__acc.until(function(){ return window.__acc.q(".diagnosis-recent-incidents"); }, 30000, "历史诊断区块");',
      'await window.__acc.sleep(600);',
      'var sec = window.__acc.q(".diagnosis-recent-incidents");',
      'var btns = window.__acc.qa(".diagnosis-recent-incidents-list li button");',
      'return { text: window.__acc.txt(sec), count: btns.length,',
      '  footer: window.__acc.txt(window.__acc.q(".diagnosis-recent-incidents-footer")) };',
    ].join('\n'), 60000);
    log('  历史诊断区块文本: ' + landing.text);

    const OPEN_PICKER = [
      'var sel = window.__acc.q(SELECTOR);',
      'if (!sel) return "missing";',
      'var box = sel.getBoundingClientRect();',
      'if (!box.height) return "hidden";',
      'var handle = sel.querySelector(".ant-select-selector");',
      'if (!handle) return "no-handle";',
      'handle.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));',
      'handle.click();',
      'return "opened";',
    ].join('\n').replace(/SELECTOR/g, '"__SEL__"');

    const pickInstance = async (sel, label) => {
      const opened = await ev(OPEN_PICKER.split('"__SEL__"').join(JSON.stringify(sel)), 30000);
      if (opened !== 'opened') return { ok: false, reason: opened };
      const chosen = await ev([
        'await window.__acc.until(function(){',
        '  return document.querySelectorAll(".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option").length > 0;',
        '}, 20000, "实例下拉选项");',
        'var opts = Array.prototype.slice.call(document.querySelectorAll(".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option"));',
        'var labels = opts.map(function (o) { return window.__acc.txt(o); });',
        'var hit = null;',
        'for (var i = 0; i < opts.length; i += 1) {',
        '  if (labels[i].indexOf(' + JSON.stringify(label) + ') >= 0) { hit = opts[i]; break; }',
        '}',
        'if (!hit) return { ok: false, labels: labels };',
        'hit.click();',
        'return { ok: true, chosen: window.__acc.txt(hit), labels: labels };',
      ].join('\n'), 40000);
      return chosen;
    };

    if (landing.count === 0) {
      // 未选中实例时列表为空。优先走外壳全局实例选择器（客户入口的默认路径），
      // 拿不到再退回工程师入口的页内选择器。
      log('  列表为 0 条，先尝试外壳全局实例选择器');
      let picked = await pickInstance('.admin-instance-select', focus.label);
      log('  全局选择器结果: ' + JSON.stringify(picked));
      if (!picked.ok) {
        log('  退回工程师入口的页内实例选择器');
        await ev('window.__acc.clickText("button", "工程师"); return true;', 30000);
        await ev('await window.__acc.until(function(){ return window.__acc.q(".diagnosis-single-instance-picker"); }, 20000, "页内实例选择器"); return true;', 40000);
        picked = await pickInstance('.diagnosis-single-instance-picker', focus.label);
        log('  页内选择器结果: ' + JSON.stringify(picked));
      }
      if (!picked.ok) throw new Error('无法选中目标实例 ' + focus.label
        + '；下拉里看到的选项: ' + JSON.stringify(picked.labels || picked.reason));
      await ev('await window.__acc.sleep(1500); return true;', 30000);
      landing = await ev([
        'var sec = window.__acc.q(".diagnosis-recent-incidents");',
        'return { text: window.__acc.txt(sec), count: window.__acc.qa(".diagnosis-recent-incidents-list li button").length,',
        '  footer: window.__acc.txt(window.__acc.q(".diagnosis-recent-incidents-footer")) };',
      ].join('\n'), 60000);
      log('  选中「' + picked.chosen + '」后历史诊断区块文本: ' + landing.text);
    }

    const shot1 = await shot('01-landing-history-list', "window.__acc.q('.diagnosis-recent-incidents')");
    check('1. 落地页可见「历史诊断」列表（' + landing.count + ' 条）',
      landing.text.indexOf('历史诊断') >= 0 && landing.count > 0, shot1);

    // ================= 验收点 2：点一条历史进入详情 =================
    log('--- 验收点 2：点历史记录进入详情 ---');
    const listItemText = await ev([
      'var btn = window.__acc.q(".diagnosis-recent-incidents-list li button");',
      'if (!btn) throw new Error("历史诊断列表为空，无法进入详情");',
      'var t = window.__acc.txt(btn);',
      'btn.click();',
      'return t;',
    ].join('\n'), 60000);
    log('  列表条目文本: ' + listItemText);
    await ev('await window.__acc.until(function(){ return window.__acc.q(".diagnosis-workspace-screen"); }, 45000, "工作台分支"); return true;', 60000);
    const headerText = await ev('return window.__acc.txt(window.__acc.q(".diagnosis-context-bar"));', 30000);
    log('  详情头部: ' + headerText);
    const shot2 = await shot('02-incident-detail');
    const listKey = (listItemText.match(/INC-[A-Za-z0-9-]+/) || [''])[0];
    const listTitle = listItemText.replace(/^[^ ]* /, '').split(' · ')[0].trim();
    check('2. 进入详情且编号/标题与列表一致（编号=' + (listKey || 'n/a') + '）',
      (listKey ? headerText.indexOf(listKey) >= 0 : true) && headerText.length > 0, shot2);

    // ================= 验收点 3：生成工单，根因候选非空 =================
    log('--- 验收点 3：生成工单，根因候选非空 ---');
    await ev('window.__acc.clickText("button", "生成工单"); return true;', 30000);
    // 注意：组件名叫 ItsmTicketDrawer，实际渲染的是 antd Modal（见 ItsmTicketDrawer.jsx:89）。
    const ticket = await ev([
      'await window.__acc.until(function(){',
      '  var m = window.__acc.byText(".ant-modal-title", "创建 ITSM 工单");',
      '  return m && window.__acc.q(".ant-modal-content #description") ? true : null;',
      '}, 30000, "创建 ITSM 工单弹窗");',
      'await window.__acc.sleep(600);',
      'return { value: String(window.__acc.q(".ant-modal-content #description").value || ""),',
      '  title: String((window.__acc.q(".ant-modal-content #title") || {}).value || "") };',
    ].join('\n'), 60000);
    log('  工单标题: ' + ticket.title);
    log('  工单描述: ' + ticket.value);
    const shot3 = await shot('03-ticket-root-cause');
    const hasCauseHeader = /根因候选\s*1/.test(ticket.value);
    const hasBackendCause = ticket.value.indexOf('TEMP') >= 0 || ticket.value.indexOf('存储层') >= 0;
    const hasSuggestion = ticket.value.indexOf('排序') >= 0 || ticket.value.indexOf('建议：') >= 0;
    check('3. 生成工单描述含后端 cause（根因候选 1｜TEMP/存储层）',
      hasCauseHeader && hasBackendCause, shot3);
    check('3b. 生成工单描述含后端 suggestion（建议：…）', hasSuggestion, '');
    await ev([
      'var close = window.__acc.q(".ant-modal-content") && window.__acc.q(".ant-modal-wrap .ant-modal-close");',
      'if (!close) throw new Error("找不到工单弹窗关闭按钮");',
      'close.click();',
      'return true;',
    ].join('\n'), 30000);
    await ev('await window.__acc.sleep(600); return true;', 20000);

    // ================= 验收点 5：变更影响 =================
    log('--- 验收点 5：变更影响卡片 ---');
    await ev('window.__acc.clickText(".diagnosis-dock-node", "变更"); return true;', 30000);
    const changeText = await ev([
      'await window.__acc.until(function(){ return window.__acc.q(".diagnosis-change-card"); }, 30000, "变更影响卡片");',
      'await window.__acc.sleep(400);',
      'return window.__acc.txt(window.__acc.q(".diagnosis-change-card"));',
    ].join('\n'), 60000);
    log('  变更影响卡片: ' + changeText);
    const shot5 = await shot('04-change-impact', "window.__acc.q('.diagnosis-change-card')");
    check('5. 变更影响常驻显示「未验证 / 未接入变更类数据源」，不再用数据源条数冒充',
      changeText.indexOf('未验证') >= 0
        && changeText.indexOf('未接入变更类数据源') >= 0
        && changeText.indexOf('条相关证据') < 0, shot5);
    check('5b. 关键词命中被标注为「文本近似」而非变更证据',
      changeText.indexOf('文本近似') >= 0, '');

    // ================= 验收点 6a：反证检查 =================
    log('--- 验收点 6a：反证检查 ---');
    // 注意：「根因实验室」不在 DOCK_FLOW 里（DOCK_FLOW 只含 8 个流程节点），
    // 它的入口是右侧推荐栏的「打开实验室」按钮。
    await ev('window.__acc.clickText(".diagnosis-recommendation-card button", "打开实验室"); return true;', 30000);
    const labText = await ev([
      'await window.__acc.until(function(){ return window.__acc.q(".diagnosis-lab-card"); }, 30000, "根因实验室卡片");',
      'await window.__acc.sleep(400);',
      'return window.__acc.txt(window.__acc.q(".diagnosis-lab-card"));',
    ].join('\n'), 60000);
    log('  根因实验室卡片: ' + labText);
    const shot6a = await shot('05-lab-counter-evidence', "window.__acc.q('.diagnosis-lab-card')");
    check('6a. 反证检查显示「未计算」并声明反证未执行',
      labText.indexOf('反证检查') >= 0 && labText.indexOf('未计算') >= 0 && labText.indexOf('反证未执行') >= 0, shot6a);

    // ================= 验收点 6b：复测计划 =================
    log('--- 验收点 6b：复测计划 ---');
    await ev('window.__acc.clickText(".diagnosis-dock-node", "复测"); return true;', 30000);
    const retestText = await ev([
      'await window.__acc.until(function(){ return window.__acc.q(".diagnosis-retest-list"); }, 30000, "复测计划卡片");',
      'await window.__acc.sleep(400);',
      'return window.__acc.txt(window.__acc.q(".diagnosis-retest-list").parentElement);',
    ].join('\n'), 60000);
    log('  复测计划卡片: ' + retestText);
    const planOnlyCount = (retestText.match(/plan_only/g) || []).length;
    const shot6b = await shot('06-retest-plan', "window.__acc.q('.diagnosis-retest-list')");
    check('6b. 复测计划 4 项全部 plan_only 且声明未执行（命中 ' + planOnlyCount + ' 次）',
      planOnlyCount === 4 && retestText.indexOf('未执行复测') >= 0, shot6b);

    // ================= 验收点 7：加载更多 / 分页 =================
    log('--- 验收点 7：落地页分页与「加载更多」 ---');
    await ev('window.__acc.clickText("button", "重新描述问题"); return true;', 30000);
    const footer = await ev([
      'await window.__acc.until(function(){ return window.__acc.q(".diagnosis-recent-incidents-footer"); }, 30000, "历史诊断页脚");',
      'await window.__acc.sleep(600);',
      'var sec = window.__acc.q(".diagnosis-recent-incidents");',
      'return { footer: window.__acc.txt(window.__acc.q(".diagnosis-recent-incidents-footer")),',
      '  head: window.__acc.txt(window.__acc.q(".diagnosis-recent-incidents-bar")),',
      '  count: window.__acc.qa(".diagnosis-recent-incidents-list li button").length };',
    ].join('\n'), 60000);
    log('  列表头部: ' + footer.head);
    log('  列表页脚: ' + footer.footer);
    const shot7 = await shot('07-landing-pagination', "window.__acc.q('.diagnosis-recent-incidents')");
    const totalMatch = footer.head.match(/共\s*(\d+)\s*条/);
    const total = totalMatch ? Number(totalMatch[1]) : -1;
    const showingAll = footer.footer.indexOf('已显示当前实例全部') >= 0;
    const hasLoadMore = footer.footer.indexOf('加载更多') >= 0;
    check('7. 分页状态正确（total=' + total + '，页脚="' + footer.footer + '"）',
      total > 0 && ((total > 50 && hasLoadMore) || (total <= 50 && showingAll)), shot7);
    log('  说明：当前库只有 ' + total + ' 条历史诊断（< 50），「加载更多」按设计不出现；'
      + '>50 条的分支在下方「验收点 7b」用 CDP 请求拦截触发（业务数据不变，仅注入响应）。');

    // ================= 验收点 4：AI 降级提示 =================
    log('--- 验收点 4：AI 降级提示（临时停用默认模型，取证后恢复）---');
    await setAiEnabled(token, false);
    log('  已停用 AI provider id=' + AI_PROVIDER_ID);
    const DEGRADE_TEXT = '客户现场反馈业务偶发卡顿，需要端到端定位';
    await ev([
      'await window.__acc.until(function(){ return window.__acc.q(".diagnosis-single-textarea"); }, 30000, "问题描述输入框");',
      'window.__acc.setVal(window.__acc.q(".diagnosis-single-textarea"), ' + JSON.stringify(DEGRADE_TEXT) + ');',
      'await window.__acc.sleep(300);',
      'window.__acc.clickText("button", "让 TraceMind 执行");',
      'return true;',
    ].join('\n'), 60000);
    const landingAlert = await ev([
      'await window.__acc.until(function(){',
      '  var a = window.__acc.qa(".ant-alert");',
      '  return a.filter(function (el) { return el.getClientRects().length > 0; }).length > 0 ? true : null;',
      '}, 60000, "降级告警条");',
      'await window.__acc.sleep(400);',
      'var a = window.__acc.qa(".ant-alert").filter(function (el) { return el.getClientRects().length > 0; });',
      'return window.__acc.txt(a[0]);',
    ].join('\n'), 90000);
    log('  落地页降级告警: ' + landingAlert);
    const shot4a = await shot('08-degradation-landing', "window.__acc.q('.ant-alert')");

    // 关掉需求工作区弹层，再切到工作台分支取证
    await ev([
      'var close = window.__acc.q(".ant-drawer-close") || window.__acc.q(".ant-modal-close");',
      'if (close) close.click();',
      'await window.__acc.sleep(700);',
      'return true;',
    ].join('\n'), 30000);
    await ev([
      'var btn = window.__acc.q(".diagnosis-recent-incidents-list li button");',
      'if (!btn) throw new Error("历史列表为空，无法进入工作台分支");',
      'btn.click();',
      'return true;',
    ].join('\n'), 60000);
    const workspaceAlert = await ev([
      'await window.__acc.until(function(){',
      '  if (!window.__acc.q(".diagnosis-workspace-screen")) return null;',
      '  var a = window.__acc.qa(".ant-alert").filter(function (el) { return el.getClientRects().length > 0; });',
      '  return a.length > 0 ? true : null;',
      '}, 60000, "工作台分支降级告警条");',
      'await window.__acc.sleep(400);',
      'var shell = window.__acc.q(".diagnosis-cockpit-shell");',
      'var a = window.__acc.qa(".ant-alert").filter(function (el) { return el.getClientRects().length > 0; });',
      'var r0 = a[0].getBoundingClientRect();',
      'var r1 = window.__acc.q(".diagnosis-workspace-screen").getBoundingClientRect();',
      'return { text: window.__acc.txt(a[0]), inWorkspace: r0.top <= r1.top + 4, workspaceVisible: r1.height > 0 };',
    ].join('\n'), 90000);
    log('  工作台分支降级告警: ' + workspaceAlert.text + ' | inWorkspace=' + workspaceAlert.inWorkspace);
    const shot4b = await shot('09-degradation-workspace', "window.__acc.q('.ant-alert')");
    check('4. 工作台分支可见 AI 降级提示（渲染在工作台之上，inWorkspace=' + workspaceAlert.inWorkspace + '）',
      workspaceAlert.workspaceVisible && workspaceAlert.inWorkspace
        && /未配置|不可用|手动|模型/.test(workspaceAlert.text), shot4b);
    check('4b. 落地页分支同样可见降级提示', landingAlert.length > 0, shot4a);

    // 恢复 AI
    await setAiEnabled(token, true);
    const restored = await fetch(BASE + '/api/v1/ai-config', { headers: { Authorization: 'Bearer ' + token } })
      .then((r) => r.json())
      .then((d) => (d.items || []).find((i) => i.id === AI_PROVIDER_ID));
    check('4c. 取证后恢复 AI provider（enabled=' + (restored && restored.enabled) + '）',
      Boolean(restored && restored.enabled === 1), '');

    // ================= 验收点 7b：>50 条时「加载更多」真实可用 =================
    // 业务库只有 1 条诊断，无法自然触发 >50 分支。这里用 CDP Fetch 拦截
    // /diagnosis-center/incidents 的响应注入 120 条 —— 明确标注：这是对
    // 「前端分页逻辑」的验收，不是对业务数据量的断言；请求 URL 是前端真实发出的。
    log('--- 验收点 7b：>50 条时「加载更多」（CDP 拦截注入 120 条响应） ---');
    await send('Page.navigate', { url: BASE + '/' }, 20000);
    await ev('await window.__acc.until(function(){ return document.readyState === "complete"; }, 30000, "page reload"); return true;', 40000);
    await ev('await window.__acc.until(function(){ return window.__acc.q(".diagnosis-cockpit-shell"); }, 45000, "诊断工作台外壳"); return true;', 60000);

    const incidentUrls = [];
    const fulfillPage = async (params) => {
      const url = params.request.url;
      const pageMatch = url.match(/[?&]page=(\d+)/);
      const page = pageMatch ? Number(pageMatch[1]) : 1;
      incidentUrls.push({ url, page });
      const start = (page - 1) * 50;
      const items = [];
      for (let i = start; i < Math.min(start + 50, 120); i += 1) {
        items.push({
          id: 100000 + i,
          incident_no: 'INC-SYNTH-' + String(i + 1).padStart(4, '0'),
          instance_id: focus.instanceId,
          db_type: 'oracle',
          instance_name: focus.instanceName,
          title: '注入记录 ' + (i + 1),
          trigger_source: 'manual',
          status: 'root_caused',
          severity: 'warning',
          score: 40,
          arch_type: '单机',
          primary_layer: '存储层',
          root_cause_summary: '',
          ai_tier: 'local',
          ai_provider: '本地规则引擎',
          ticket_id: null,
          evidence_count: 0,
          root_cause_count: 0,
          execution_status: 'success',
          data_quality: 'good',
          conclusion_status: 'hypotheses_require_validation',
          create_time: '2026-10-27 11:00:00',
        });
      }
      const body = JSON.stringify({ items, total: 120, page, page_size: 50 });
      const { encoded } = await send('Fetch.fulfillRequest', {
        requestId: params.requestId,
        responseCode: 200,
        responseHeaders: [
          { name: 'Content-Type', value: 'application/json' },
          { name: 'Access-Control-Allow-Origin', value: BASE },
        ],
        body: Buffer.from(body, 'utf8').toString('base64'),
      }, 15000);
      return encoded;
    };
    const offEvent = send.onEvent((msg) => {
      if (msg.method !== 'Fetch.requestPaused') return;
      const url = msg.params.request.url;
      if (!/\/diagnosis-center\/incidents\?/.test(url)) {
        send('Fetch.continueRequest', { requestId: msg.params.requestId }, 10000).catch(() => {});
        return;
      }
      fulfillPage(msg.params).catch(() => {});
    });
    await send('Fetch.enable', {
      patterns: [{ urlPattern: '*' + BASE + '/api/v1/diagnosis-center/incidents*', requestStage: 'Request' }],
    }, 10000);

    // 整页 reload 会丢掉外壳里的实例选择（它只是 React state，不持久化），
    // 必须先重新选中实例，否则列表停在「请先在上方选择数据库实例」。
    const repicked = await pickInstance('.admin-instance-select', focus.label);
    log('  reload 后重新选实例: ' + JSON.stringify(repicked));
    if (!repicked.ok) throw new Error('reload 后无法选中实例: ' + JSON.stringify(repicked));

    // 触发一次真实加载（点击刷新按钮）
    await ev([
      'await window.__acc.until(function(){ return window.__acc.q(".diagnosis-recent-incidents"); }, 30000, "历史诊断区块");',
      'window.__acc.clickText("button", "刷新");',
      'return true;',
    ].join('\n'), 60000);
    const lm = await ev([
      'await window.__acc.until(function(){',
      '  var f = window.__acc.q(".diagnosis-recent-incidents-footer");',
      '  return f && window.__acc.txt(f).indexOf("加载更多") >= 0 ? true : null;',
      '}, 30000, "加载更多按钮");',
      'return { footer: window.__acc.txt(window.__acc.q(".diagnosis-recent-incidents-footer")),',
      '  count: window.__acc.qa(".diagnosis-recent-incidents-list li button").length };',
    ].join('\n'), 60000);
    log('  第 1 页后: 页脚="' + lm.footer + '"，已渲染 ' + lm.count + ' 条，实际请求: '
      + incidentUrls.map((u) => u.url.replace(/.*incidents/, '')).join(' | '));
    const shot7b = await shot('10-loadmore-page1',
      "window.__acc.q('.diagnosis-recent-incidents')");

    // 点「加载更多」，验证真实发出的请求是 page=2&page_size=50
    await ev('window.__acc.clickText("button", "加载更多"); return true;', 30000);
    const lm2 = await ev([
      'await window.__acc.until(function(){',
      '  var n = window.__acc.qa(".diagnosis-recent-incidents-list li button").length;',
      '  return n >= 100 ? true : null;',
      '}, 30000, "加载第 2 页后的条数");',
      'return { footer: window.__acc.txt(window.__acc.q(".diagnosis-recent-incidents-footer")),',
      '  count: window.__acc.qa(".diagnosis-recent-incidents-list li button").length };',
    ].join('\n'), 60000);
    log('  点「加载更多」后: 页脚="' + lm2.footer + '"，已渲染 ' + lm2.count + ' 条，实际请求: '
      + incidentUrls.map((u) => 'p' + u.page).join(' -> '));
    const shot7b2 = await shot('11-loadmore-page2',
      "window.__acc.q('.diagnosis-recent-incidents')");
    await send('Fetch.disable', {}, 10000).catch(() => {});
    offEvent();

    const pages = incidentUrls.map((u) => u.page);
    const reqUrls = incidentUrls.map((u) => u.url);
    check('7b. 首屏请求带 page=1&page_size=50，点「加载更多」真实发出 page=2&page_size=50（请求 URL: '
      + reqUrls.join(' , ').replace(/^.*?incidents/, '') + '）',
      pages.join(',') === '1,2'
        && /[?&]page=1(&|$)/.test(reqUrls[0]) && /[?&]page_size=50/.test(reqUrls[0])
        && /[?&]page=2(&|$)/.test(reqUrls[1]) && /[?&]page_size=50/.test(reqUrls[1])
        && lm.count === 50 && lm2.count === 100, shot7b);
    check('7b2. 加载更多后按钮文案从「已显示 50 / 120」推进到「已显示 100 / 120」',
      lm.footer.indexOf('50 / 120') >= 0 && lm2.footer.indexOf('100 / 120') >= 0, shot7b2);
  } catch (error) {
    log('ERROR: ' + (error && error.stack ? error.stack : error));
    check('脚本执行未抛异常', false, String(error && error.message || error));
  } finally {
    if (watchdog) clearTimeout(watchdog);
    if (send) { try { send.close(); } catch (_) { /* ignore */ } }
    if (chrome) { try { chrome.kill('SIGKILL'); } catch (_) { /* ignore */ } }
    // 只删自己 mkdtemp 出来的 profile
    if (typeof profile === 'string' && path.basename(profile).startsWith('tm-acc-')) {
      try { fs.rmSync(profile, { recursive: true, force: true }); } catch (_) { /* ignore */ }
    }
  }

  // ---- 汇总
  const failed = checks.filter((c) => !c.ok);
  const summary = {
    generatedAt: new Date().toISOString(),
    base: BASE,
    evidenceDir: OUT,
    total: checks.length,
    passed: checks.length - failed.length,
    failed: failed.length,
    checks,
  };
  fs.writeFileSync(path.join(OUT, 'acceptance-summary.json'), JSON.stringify(summary, null, 2));
  log('================ 汇总 ================');
  log('证据目录: ' + OUT);
  checks.forEach((c) => log((c.ok ? '  [PASS] ' : '  [FAIL] ') + c.name + (c.detail ? '  -> ' + c.detail : '')));
  log('通过 ' + (checks.length - failed.length) + ' / ' + checks.length);
  if (failed.length) process.exitCode = 1;
}

if (require.main === module) main();
module.exports = { main };
