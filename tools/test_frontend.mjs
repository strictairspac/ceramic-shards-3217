// 前端台账页渲染与交互验证（jsdom + 原生 ESM）
import {JSDOM} from 'jsdom';

const dom = new JSDOM('<!doctype html><html><body></body></html>', {url: 'http://localhost/'});
// 先固定全局 document/window，使应用各 ESM 模块共享同一 DOM 实例（含 submit 钩子）。
globalThis.document = dom.window.document;
globalThis.window = dom.window;
globalThis.HTMLElement = dom.window.HTMLElement;
globalThis.Event = dom.window.Event;
globalThis.FormData = dom.window.FormData;

const {ledgersPage, bindLedgers} = await import('/workspace/frontend/src/pages/ledgers.js');
const {evidencePage} = await import('/workspace/frontend/src/pages/evidence.js');

let pass = 0, fail = 0;
const check = (name, cond, detail = '') => { cond ? (pass++, console.log('  PASS', name)) : (fail++, console.log('  FAIL', name, detail)); };

const step = (step, label, state, attempts = 0, receipt = null) => ({step, label, state, attempts, last_receipt: receipt});
const state = {
  session: {role: 'operator'},
  ledgers: [{id: 7, evidence_id: 3, snapshot_id: 'TY-20261121-0800', status: 'blocked', updated_at: '2026-11-21T09:00:00+00:00'}],
  ledgerDetail: {
    id: 7, snapshot_id: 'TY-20261121-0800', status: 'blocked',
    live_used_bytes: {OLD: 70000000, NORTH: 135000000, WEST: 95000000, EAST: 177000000},
    inflight_reserved_bytes: {OLD: 0, NORTH: 0, WEST: 50000000, EAST: 0},
    live_owners: {'TY-1121-02': 'NORTH', 'TY-1121-03': 'OLD'},
    items: [
      {id: 11, seq: 1, batch_id: 'TY-1121-02', source_node: 'OLD', target_node: 'NORTH', size_bytes: 70000000, blocked: false,
       step: 'source_cleaned',
       steps: [step('copy_verified', '复制校验', 'done', 1, {attempt: 1, outcome: 'success', actor: 'op', created_at: '2026-11-21T09:01:00+00:00', note: ''}),
               step('ownership_switched', '归属切换', 'done', 1),
               step('source_cleaned', '源副本清理', 'waiting', 0)]},
      {id: 12, seq: 2, batch_id: 'TY-1121-03', source_node: 'OLD', target_node: 'WEST', size_bytes: 50000000, blocked: true,
       step: 'ownership_switched',
       steps: [step('copy_verified', '复制校验', 'done', 2),
               step('ownership_switched', '归属切换', 'failed', 2, {attempt: 2, outcome: 'failure', actor: 'op', created_at: '2026-11-21T09:05:00+00:00', note: '对端超时'}),
               step('source_cleaned', '源副本清理', 'waiting', 0)]},
    ],
  },
};

// 1) 渲染：能看出卡在哪一步
const html = ledgersPage(state, 7);
check('台账标题含盘点编号', html.includes('TY-20261121-08'));
check('渲染三步管道', html.includes('复制校验') && html.includes('归属切换') && html.includes('源副本清理'));
check('阻塞条目显示失败态', html.includes('重试失败'));
check('阻塞条目提示停止交接', html.includes('停止交接'));
check('显示在途占用列', html.includes('在途副本占用'));
check('WEST 在途 50,000,000 已展示', html.includes('50,000,000'));
check('经办员可见回执表单', html.includes('receipt-form'));
check('经办员可见重排面板', html.includes('replan-form'));

// 2) viewer 角色不出现表单
const viewerState = {...state, session: {role: 'viewer'}};
const htmlViewer = ledgersPage(viewerState, 7);
check('观察员无回执表单', !htmlViewer.includes('receipt-form'));
check('观察员无重排面板', !htmlViewer.includes('replan-form'));

// 3) 空台账引导
const emptyHtml = ledgersPage({session: {role: 'operator'}, ledgers: [], ledgerDetail: null}, 0);
check('空台账有引导文案', emptyHtml.includes('还没有交接中的搬迁清单'));

// 4) bindLedgers：提交回执会按当前步骤 POST 并刷新
document.body.innerHTML = html;
const calls = [];
const api = async (path, options) => { calls.push({path, options}); return {}; };
let refreshed = 0;
await bindLedgers(state, api, async () => { refreshed++; }, () => {});
// 只有未阻塞条目 item 11 渲染了表单
const form = document.querySelector('form.receipt-form');
check('仅未阻塞条目有表单', document.querySelectorAll('form.receipt-form').length === 1);
check('表单携带台账与条目号', form.dataset.ledger === '7' && form.dataset.item === '11');
form.querySelector('select[name=step]').value = 'source_cleaned';
form.querySelector('select[name=outcome]').value = 'success';
form.querySelector('textarea[name=content]').value = '{"freed_bytes":70000000}';
form.querySelector('input[name=note]').value = '清理完成';
form.noValidate = true;
form.dispatchEvent(new dom.window.Event('submit', {cancelable: true, bubbles: true}));
await new Promise(r => setTimeout(r, 30));
check('回执 POST 路径正确', calls.length === 1 && calls[0].path === '/ledgers/7/items/11/receipts', JSON.stringify(calls));
check('回执载荷含步骤/结果/JSON内容', calls[0]?.options.data.step === 'source_cleaned'
      && calls[0].options.data.outcome === 'success'
      && calls[0].options.data.content.freed_bytes === 70000000
      && calls[0].options.data.note === '清理完成', JSON.stringify(calls[0]));
check('提交后触发刷新', refreshed === 1);

// 5) 重排表单提交
const replanForm = document.querySelector('#replan-form');
replanForm.noValidate = true;
replanForm.querySelector('input[name=budget]').value = '150000000';
replanForm.dispatchEvent(new dom.window.Event('submit', {cancelable: true, bubbles: true}));
await new Promise(r => setTimeout(r, 30));
const replanCall = calls.find(c => c.path === '/ledgers/7/replan');
check('重排 POST 预算正确', replanCall && replanCall.options.data.budget === 150000000, JSON.stringify(calls));
check('重排结果渲染到页面', document.querySelector('#replan-result').innerHTML.includes('本轮搬迁清单')
      || document.querySelector('#replan-result').innerHTML.includes('metrics'));

// 6) 证据页：复核后出现台账入口
const evState = {
  ...state,
  spec: {field_labels: {}},
  evidence: [{id: 3, run_id: 1, digest: 'abc', reviewer: 'rv', reviewed_at: '2026-11-21T08:30:00+00:00',
    review_note: 'ok', report: {summary: '盘点结论', metrics: {a: 1}, sections: []}}],
};
const evHtml = evidencePage(evState, 3);
check('证据页出现打开台账入口', evHtml.includes('打开搬迁回执台账') && evHtml.includes('#ledgers/7'));

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
