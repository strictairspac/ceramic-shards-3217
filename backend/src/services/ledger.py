"""搬迁回执台账。

台账在搬迁清单经复核交接后建立，按「复制校验 → 归属原子切换 → 源副本清理」
三步顺序登记下游回执。规则：

* 复制校验成功后目标库先计占用；归属切换是原子动作；源库等清理确认才释放空间。
* 乱序回执不能跳步：前置步骤未成功时后续步骤一律拒绝。
* 每一步只允许一次重试（初次 + 重试共两条回执）；同一回执内容与既有记录冲突时报错。
"""
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.config.settings import engine
from src.constants.workflow import LedgerStatus, MoveStep, ReceiptOutcome
from src.models.tables import evidence, ledger_items, ledgers, receipts
from src.services.workflow import log
from src.utils.encoding import canonical
from src.utils.time import utc_time

STEP_ORDER = [MoveStep.COPY, MoveStep.SWITCH, MoveStep.CLEANUP]
STEP_LABELS = {
    MoveStep.COPY: '复制校验',
    MoveStep.SWITCH: '归属切换',
    MoveStep.CLEANUP: '源副本清理',
}


def _step(value):
    try:
        return MoveStep(value)
    except ValueError:
        raise HTTPException(422, f'未知回执步骤：{value}') from None


def _outcome(value):
    try:
        return ReceiptOutcome(value)
    except ValueError:
        raise HTTPException(422, f'未知回执结果：{value}') from None


def ensure_ledger(conn, evidence_id, actor=None):
    """复核交接后建立台账；已存在则直接返回。台账冻结清单时点的盘点与计划。"""
    row = conn.execute(select(ledgers).where(ledgers.c.evidence_id == evidence_id)).mappings().first()
    if row:
        return dict(row)
    ev = conn.execute(select(evidence).where(evidence.c.id == evidence_id)).mappings().first()
    if ev is None:
        raise HTTPException(404, '找不到该证据')
    if ev['reviewed_at'] is None:
        raise HTTPException(422, '搬迁清单尚未复核交接，不能建立回执台账')
    report = ev['report']
    manifest = next(section['rows'] for section in report['sections']
                    if section['title'] == '本轮搬迁清单')
    node_rows = next(section['rows'] for section in report['sections']
                     if section['title'] == '节点物理占用')
    comparisons = next(section['rows'] for section in report['sections']
                       if section['title'] == '批次归属比较')
    snapshot_id = manifest[0]['盘点编号'] if manifest else report.get('snapshot_id', '')
    # 冻结清单时点：节点权重/容量/其他占用/盘点占用，以及每个批次的现有归属与字节。
    snapshot = {
        'snapshot_id': snapshot_id,
        'nodes': {
            row['节点编号']: {
                'weight': row['目标权重'],
                'capacity_bytes': row['物理容量字节'],
                'other_used_bytes': row['其他占用字节'],
                'initial_used_bytes': row['盘点占用字节'],
            }
            for row in node_rows
        },
        'batches': {
            row['批次编号']: {'owner': row['现有归属'], 'size_bytes': row['批次字节']}
            for row in comparisons
        },
        'manifest': [
            {'seq': row['序号'], 'batch_id': row['批次编号'], 'source_node': row['源节点'],
             'target_node': row['目标节点'], 'size_bytes': row['搬迁字节']}
            for row in manifest
        ],
    }
    now = utc_time()
    ledger_id = conn.execute(ledgers.insert().values(
        evidence_id=evidence_id, snapshot_id=snapshot_id,
        status=LedgerStatus.IN_PROGRESS if manifest else LedgerStatus.COMPLETE,
        snapshot=snapshot, created_at=now, updated_at=now)).inserted_primary_key[0]
    for entry in snapshot['manifest']:
        conn.execute(ledger_items.insert().values(
            ledger_id=ledger_id, seq=entry['seq'], batch_id=entry['batch_id'],
            source_node=entry['source_node'], target_node=entry['target_node'],
            size_bytes=entry['size_bytes'], step=MoveStep.COPY, blocked=False,
            updated_at=now))
    if actor:
        log(conn, actor, 'create_ledger', ledger_id)
    return dict(conn.execute(select(ledgers).where(ledgers.c.id == ledger_id)).mappings().first())


def list_ledgers(conn):
    return [dict(row) for row in conn.execute(
        select(ledgers).order_by(ledgers.c.id.desc())).mappings()]


def _get_ledger(conn, ledger_id):
    row = conn.execute(select(ledgers).where(ledgers.c.id == ledger_id)).mappings().first()
    if row is None:
        raise HTTPException(404, '找不到该搬迁台账')
    return dict(row)


def _get_item(conn, ledger_id, item_id, lock=False):
    statement = select(ledger_items).where(
        ledger_items.c.id == item_id, ledger_items.c.ledger_id == ledger_id)
    if lock and engine.dialect.name != 'sqlite':
        # 串行化同一条目的并发回执，避免唯一约束冲突变成 500。
        statement = statement.with_for_update()
    row = conn.execute(statement).mappings().first()
    if row is None:
        raise HTTPException(404, '找不到该台账条目')
    return dict(row)


def _receipts_of(conn, item_id):
    return [dict(row) for row in conn.execute(
        select(receipts).where(receipts.c.item_id == item_id).order_by(receipts.c.id)).mappings()]


def _item_view(conn, item):
    receipts_by_step = {step: [] for step in STEP_ORDER}
    for receipt in _receipts_of(conn, item['id']):
        receipts_by_step[MoveStep(receipt['step'])].append(receipt)
    current_index = STEP_ORDER.index(MoveStep(item['step']))
    steps = []
    for index, step in enumerate(STEP_ORDER):
        records = receipts_by_step[step]
        if not records:
            if index < current_index:
                state = 'done'
            elif item['blocked'] and index == current_index:
                state = 'blocked'
            else:
                state = 'waiting'
        else:
            last = records[-1]
            if last['outcome'] == ReceiptOutcome.SUCCESS:
                state = 'done'
            elif len(records) >= 2:
                state = 'failed'  # 重试仍失败，停止交接
            else:
                state = 'retrying'
        steps.append({
            'step': step,
            'label': STEP_LABELS[step],
            'state': state,
            'attempts': len(records),
            'last_receipt': records[-1] if records else None,
        })
    view = dict(item)
    view['steps'] = steps
    return view


def ledger_detail(conn, ledger_id):
    ledger = _get_ledger(conn, ledger_id)
    items = [dict(row) for row in conn.execute(
        select(ledger_items).where(ledger_items.c.ledger_id == ledger_id)
        .order_by(ledger_items.c.seq)).mappings()]
    ledger['items'] = [_item_view(conn, item) for item in items]
    ledger['step_labels'] = {step.value: STEP_LABELS[step] for step in STEP_ORDER}
    used, owners, reservations = current_occupation(conn, ledger)
    ledger['live_used_bytes'] = used
    ledger['live_owners'] = owners
    ledger['inflight_reserved_bytes'] = reservations
    return ledger


def register_receipt(ledger_id, item_id, form, actor):
    step = _step(form.step)
    outcome = _outcome(form.outcome)
    note = (form.note or '').strip()
    with engine.begin() as conn:
        ledger = _get_ledger(conn, ledger_id)
        if ledger['status'] == LedgerStatus.COMPLETE:
            raise HTTPException(409, '该台账已全部完成，不再接收回执')
        item = _get_item(conn, ledger_id, item_id, lock=True)
        current = MoveStep(item['step'])
        step_index = STEP_ORDER.index(step)
        current_index = STEP_ORDER.index(current)

        prior = _receipts_of(conn, item['id'])
        step_records = [r for r in prior if MoveStep(r['step']) == step]

        # 乱序回执不能跳步。
        if step_index < current_index:
            raise HTTPException(409, f'{STEP_LABELS[step]}已完成或已登记，不能补交/重复该步骤回执')
        if step_index > current_index:
            raise HTTPException(409, f'不能跳步：请先完成 {STEP_LABELS[current]} 的回执')
        if item['blocked']:
            raise HTTPException(409, f'条目已在{STEP_LABELS[current]}失败停止，需重新盘点后试算，不再接收回执')

        # 重试只生效一次：每步至多两条回执（初次 + 一次重试）。
        if len(step_records) >= 2:
            raise HTTPException(409, f'{STEP_LABELS[step]}重试机会已用完，该条目停止交接')
        attempt = len(step_records) + 1

        # 同一回执内容冲突：同一步骤内内容摘要与任一既有回执相同即视为重放/矛盾，
        # 无论结果是否一致都拒绝入账；新一轮重试必须携带新的回执事实。
        incoming = canonical(form.content)
        for record in step_records:
            if canonical(record['content']) == incoming:
                if record['outcome'] == outcome:
                    raise HTTPException(409, f'{STEP_LABELS[step]}相同回执已登记，请勿重复提交')
                raise HTTPException(409, f'{STEP_LABELS[step]}回执内容与已登记回执冲突，已拒绝入账')

        now = utc_time()
        try:
            conn.execute(receipts.insert().values(
                item_id=item_id, step=step, attempt=attempt, outcome=outcome,
                content=form.content, actor=actor, note=note or None, created_at=now))
        except IntegrityError:
            # 并发回执在唯一约束上相撞（行锁未覆盖的极端情况）：拒绝而非报 500。
            raise HTTPException(409, f'{STEP_LABELS[step]}回执并发冲突，该条目状态已变化，请刷新后重试') from None

        blocked = item['blocked']
        next_step = current
        if outcome == ReceiptOutcome.SUCCESS:
            next_index = step_index + 1
            next_step = STEP_ORDER[next_index] if next_index < len(STEP_ORDER) else MoveStep.CLEANUP
        else:
            # 初次失败等待唯一一次重试；重试（第二条回执）仍失败则阻塞条目。
            blocked = attempt >= 2
        conn.execute(ledger_items.update().where(ledger_items.c.id == item_id).values(
            step=next_step, blocked=blocked, updated_at=now))
        conn.execute(ledgers.update().where(ledgers.c.id == ledger_id).values(updated_at=now))
        log(conn, actor, f'receipt_{step.value}', item_id)
        view = _item_view(conn, _get_item(conn, ledger_id, item_id))

    _refresh_ledger_status(ledger_id)
    return view


def _refresh_ledger_status(ledger_id):
    with engine.begin() as conn:
        items = [dict(row) for row in conn.execute(
            select(ledger_items).where(ledger_items.c.ledger_id == ledger_id)).mappings()]
        if not items:
            return
        any_blocked = any(item['blocked'] for item in items)
        all_done = all(
            any(r['step'] == MoveStep.CLEANUP and r['outcome'] == ReceiptOutcome.SUCCESS
                for r in _receipts_of(conn, item['id']))
            for item in items)
        status = LedgerStatus.COMPLETE if all_done else (
            LedgerStatus.BLOCKED if any_blocked else LedgerStatus.IN_PROGRESS)
        conn.execute(ledgers.update().where(ledgers.c.id == ledger_id)
                     .values(status=status, updated_at=utc_time()))


def current_occupation(conn, ledger):
    """按回执账本重放同一时点的节点占用与批次归属。

    复制校验成功即把整批计入目标库；源库只在清理确认后释放；归属切换不改字节，
    仅决定批次的当前归属。同时返回在途副本占用：已复制未切换占目标、已切换未清理占源。
    """
    snap = ledger['snapshot']
    used = {node_id: node['initial_used_bytes'] for node_id, node in snap['nodes'].items()}
    owners = {batch_id: info['owner'] for batch_id, info in snap['batches'].items()}
    reservations = {node_id: 0 for node_id in snap['nodes']}
    for item in conn.execute(select(ledger_items).where(
            ledger_items.c.ledger_id == ledger['id'])).mappings():
        item = dict(item)
        item_receipts = _receipts_of(conn, item['id'])
        copy_ok = any(r['step'] == MoveStep.COPY and r['outcome'] == ReceiptOutcome.SUCCESS
                      for r in item_receipts)
        switched = any(r['step'] == MoveStep.SWITCH and r['outcome'] == ReceiptOutcome.SUCCESS
                       for r in item_receipts)
        cleaned = any(r['step'] == MoveStep.CLEANUP and r['outcome'] == ReceiptOutcome.SUCCESS
                      for r in item_receipts)
        if copy_ok:
            used[item['target_node']] += item['size_bytes']  # 目标副本先落盘计占用
        if switched:
            owners[item['batch_id']] = item['target_node']   # 原子归属切换
        if cleaned:
            used[item['source_node']] -= item['size_bytes']  # 清理确认后才腾源库
        # 在途副本：切换前双份占用压在目标侧；切换后未清理的源副本仍压在源侧。
        if copy_ok and not switched:
            reservations[item['target_node']] += item['size_bytes']
        elif switched and not cleaned:
            reservations[item['source_node']] += item['size_bytes']
    return used, owners, reservations


def replan_payload(conn, ledger_id, budget):
    """用台账同一时点的归属和占用生成下一轮试算输入。

    其他占用固定；在途副本（已复制未切换、已切换未清理）作为临时占用并入对应节点的
    其他占用口径，使重排清单基于读取时点真实可用空间，而不是低估目标库压力。
    """
    ledger = _get_ledger(conn, ledger_id)
    snap = ledger['snapshot']
    used, owners, reservations = current_occupation(conn, ledger)
    nodes = [
        {
            'node_id': node_id,
            'weight': info['weight'],
            'capacity_bytes': info['capacity_bytes'],
            'other_used_bytes': info['other_used_bytes'] + reservations[node_id],
        }
        for node_id, info in snap['nodes'].items()
    ]
    batches = [
        {'batch_id': bid, 'current_owner': owners[bid], 'size_bytes': info['size_bytes']}
        for bid, info in sorted(snap['batches'].items())
    ]
    return {
        'snapshot_id': f"{snap['snapshot_id']}-replan-{ledger_id}",
        'round_budget_bytes': budget,
        'nodes': nodes,
        'batches': batches,
    }, used, reservations
