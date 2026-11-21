import {heading, panel} from '../components/common/heading.js';
import {records} from '../components/common/records.js';
import {
  ledgerStatusLabels, receiptOutcomeLabels, stepStateLabels,
} from '../constants/workflow.js';
import {topic} from '../constants/project.js';
import {date, esc} from '../utils/dom.js';
import {submit} from '../hooks/submit.js';

const STEP_SEQUENCE = ['copy_verified', 'ownership_switched', 'source_cleaned'];
const format = new Intl.NumberFormat('zh-CN');

function stepBadge(stepState) {
  return `<span class="step-state" data-state="${esc(stepState)}">${esc(stepStateLabels[stepState] || stepState)}</span>`;
}

function ledgerBadge(status) {
  return `<span class="status" data-state="${status === 'complete' ? 'complete' : (status === 'blocked' ? 'failed' : 'running')}">${esc(ledgerStatusLabels[status] || status)}</span>`;
}

function itemCard(item, isOperator, ledgerId) {
  const bodyLedgerId = ledgerId;
  const pipeline = item.steps.map((step, index) => {
    const arrow = index < item.steps.length - 1 ? '<span class="step-arrow">→</span>' : '';
    const receipt = step.last_receipt;
    const detail = receipt
      ? `<small>${esc(receipt.actor)} · 第 ${receipt.attempt} 次 · ${esc(date(receipt.created_at))}${receipt.outcome ? ' · ' + esc(receiptOutcomeLabels[receipt.outcome] || receipt.outcome) : ''}</small>${receipt.note ? `<small>${esc(receipt.note)}</small>` : ''}`
      : '<small>尚无回执</small>';
    return `<div class="step-cell ${step.state}"><strong>${esc(step.label)}</strong>${stepBadge(step.state)}${detail}</div>${arrow}`;
  }).join('');
  const current = item.steps.find(s => s.state === 'waiting' || s.state === 'retrying' || s.state === 'blocked' || s.state === 'failed');
  const canAct = isOperator && !item.blocked && current && ['waiting', 'retrying'].includes(current.state);
  const form = canAct ? `
    <form class="receipt-form" id="receipt-form-${item.id}" data-ledger="${bodyLedgerId}" data-item="${item.id}" data-step="${esc(current.step)}">
      <div class="field-grid">
        <label>回执步骤<select name="step">
          ${STEP_SEQUENCE.map(s => `<option value="${s}" ${s === current.step ? 'selected' : ''}>${esc(item.steps.find(x => x.step === s).label)}</option>`).join('')}
        </select></label>
        <label>回执结果<select name="outcome">
          <option value="success">成功</option>
          <option value="failure">失败（保留一次重试机会）</option>
        </select></label>
      </div>
      <label>回执内容（结构化 JSON，同一步骤重复提交时内容必须不同，否则按冲突拒收）
        <textarea name="content" rows="3" required>${esc(JSON.stringify({checksum: '', bytes: item.size_bytes, node: item.target_node}))}</textarea>
      </label>
      <label>备注（可选）<input name="note" maxlength="500" placeholder="例如校验码不符、归属切换对端超时"></label>
      <div class="form-actions"><button>登记回执</button></div>
    </form>` : (item.blocked ? '<p class="error">该条目已停止交接：重试机会用尽，需重新盘点后试算重排。</p>' : '');
  return `<article class="ledger-item" data-item="${item.id}">
    <header><h3>#${item.seq} ${esc(item.batch_id)} <small>${esc(item.source_node)} → ${esc(item.target_node)} · ${format.format(item.size_bytes)} 字节</small></h3></header>
    <div class="step-pipeline">${pipeline}</div>
    ${form}
  </article>`;
}

function usageTable(detail) {
  const nodes = Object.keys(detail.live_used_bytes);
  const rows = nodes.map(node => [
    esc(node),
    format.format(detail.live_used_bytes[node]),
    format.format(detail.inflight_reserved_bytes[node] || 0),
    esc(detail.live_owners && Object.entries(detail.live_owners).filter(([, owner]) => owner === node).map(([b]) => b).join('、') || '—'),
  ]);
  return records(['节点', '当前实占（字节）', '在途副本占用（字节）', '当前归属批次'], rows);
}

export function ledgersPage(state, identity) {
  const isOperator = state.session.role === 'operator';
  const selected = state.ledgers.find(row => row.id === identity) || state.ledgers[0];
  const listRows = state.ledgers.map(row => [
    `<a href="#ledgers/${row.id}">#${row.id} ${esc(row.snapshot_id)}</a>`,
    ledgerBadge(row.status),
    esc(date(row.updated_at)),
  ]);
  if (!selected) {
    return `${heading(topic.modules[3], '搬迁清单复核交接后，在此逐批登记复制校验、归属切换与源副本清理回执。')}
      <p class="empty">还没有交接中的搬迁清单。请在「${topic.modules[3]}」证据页由复核员签署交接后自动建账。</p>`;
  }
  const detail = state.ledgerDetail || null;
  const body = detail && detail.id === selected.id ? detail : null;
  const replanPanel = isOperator && body ? `
    <section class="panel" id="replan-panel">
      <h2>按当前账本重排下一轮清单</h2>
      <p class="note">重排读取同一时点的归属与占用，在途副本计入临时占用；试算期间到达的并发回执不影响本次结果，下次重排才纳入。</p>
      <form id="replan-form" data-ledger="${body.id}" class="field-grid">
        <label>本轮搬迁预算（字节）<input name="budget" type="number" min="0" step="1" value="0" required></label>
        <div class="form-actions"><button>试算重排</button></div>
      </form>
      <div id="replan-result"></div>
    </section>` : '';
  const items = body ? body.items.map(item => itemCard(item, isOperator, body.id)).join('') : '';
  return `${heading(topic.modules[3], `台账 #${selected.id} · ${esc(selected.snapshot_id)} · ${ledgerStatusLabels[selected.status]}`,
    '<button class="secondary" type="button" id="refresh-ledger">刷新台账</button>')}
    <div class="spread">
      <section>${records(['台账（盘点编号）', '状态', '最近回执时间'], listRows, '暂无台账。')}</section>
      <aside class="brief"><h2>记账规则</h2>
        <p class="note">复制校验成功即计目标库占用；归属原子切换不改字节；源库等清理确认才释放。乱序回执拒收，每步仅一次重试，同内容冲突报错。</p>
      </aside>
    </div>
    ${body ? `<div class="ledger-head">${usageTable(body)}</div>
      <section class="panel"><h2>清单条目与回执进度（可看出每条卡在哪一步）</h2>${items}</section>
      ${replanPanel}` : '<p class="empty">台账读取中。</p>'}`;
}

function renderReplanResult(result) {
  const metrics = Object.entries(result.metrics || {})
    .map(([label, value]) => `<div><dt>${esc(label)}</dt><dd>${esc(typeof value === 'number' ? format.format(value) : value)}</dd></div>`).join('');
  const sections = (result.sections || []).map(section => {
    const keys = Array.from(new Set(section.rows.flatMap(Object.keys)));
    const head = keys.map(k => `<th scope="col">${esc(k)}</th>`).join('');
    const body = section.rows.map(row => `<tr>${keys.map(k => `<td>${esc(String(row[k] ?? ''))}</td>`).join('')}</tr>`).join('');
    return `<details open><summary>${esc(section.title)}</summary><div class="table-scroll"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div></details>`;
  }).join('');
  const basis = result.replan_basis || {};
  return `<dl class="metrics">${metrics}</dl>
    <p class="note">${esc(basis.note || '')}</p>${sections}`;
}

export function bindLedgers(state, api, refresh, fail) {
  document.getElementById('refresh-ledger')?.addEventListener('click', refresh);
  document.querySelectorAll('form.receipt-form').forEach(form => {
    submit(form.id, async data => {
      const itemId = form.dataset.item;
      const ledgerId = form.dataset.ledger;
      let content;
      try {
        content = JSON.parse(data.get('content'));
      } catch {
        fail(new Error('回执内容必须是合法 JSON，请检查后重新提交。'));
        return;
      }
      await api(`/ledgers/${ledgerId}/items/${itemId}/receipts`, {
        method: 'POST',
        data: {step: data.get('step'), outcome: data.get('outcome'), content, note: data.get('note') || ''},
      });
      await refresh();
    }, fail);
  });
  submit('replan-form', async data => {
    const ledgerId = document.getElementById('replan-form').dataset.ledger;
    const result = await api(`/ledgers/${ledgerId}/replan`, {
      method: 'POST', data: {budget: Number(data.get('budget'))},
    });
    document.getElementById('replan-result').innerHTML = renderReplanResult(result);
  }, fail);
}
