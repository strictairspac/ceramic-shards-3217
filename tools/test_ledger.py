"""搬迁回执台账集成验证：SQLite 临时库，覆盖顺序、重试、冲突、占用重放与重排。"""
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix='ledger-it-')
os.environ.update({
    'AUTH_KEY': 'test-key',
    'DB_DRIVER': 'sqlite',
    'DB_NAME': os.path.join(_tmp, 'workflow.db'),
    'DB_USER': '',
    'DB_PASSWORD': '',
    'DB_HOST': '',
    'REDIS_URL': 'redis://127.0.0.1:6399/0',
})
sys.path.insert(0, '/workspace/backend')

from fastapi.testclient import TestClient  # noqa: E402

from sqlalchemy import event  # noqa: E402
from src.config.settings import engine  # noqa: E402
from src.constants.workflow import LedgerStatus, MoveStep, ReceiptOutcome  # noqa: E402
from src.models.tables import schema, users  # noqa: E402
from src.services import domain, workflow  # noqa: E402
from src.services.security import hash_password  # noqa: E402
from src.main import app  # noqa: E402


@event.listens_for(engine, 'connect')
def _sqlite_pragmas(dbapi_connection, _record):  # 文件型 SQLite 写锁等待
    cursor = dbapi_connection.cursor()
    cursor.execute('PRAGMA busy_timeout=5000')
    cursor.execute('PRAGMA journal_mode=WAL')
    cursor.close()

PASS, FAIL = 0, 0


def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  PASS {name}')
    else:
        FAIL += 1
        print(f'  FAIL {name} {detail}')


schema.drop_all(engine)
schema.create_all(engine)
with engine.begin() as conn:
    conn.execute(users.insert().values(username='operator', role='operator',
                                       password_hash=hash_password('pw')))
    conn.execute(users.insert().values(username='reviewer', role='reviewer',
                                       password_hash=hash_password('pw')))

client = TestClient(app)
op = client.post('/login', json={'username': 'operator', 'password': 'pw'}).json()['token']
rv = client.post('/login', json={'username': 'reviewer', 'password': 'pw'}).json()['token']
h_op = {'Authorization': f'Bearer {op}'}
h_rv = {'Authorization': f'Bearer {rv}'}

# 1) 建档 -> 试算 -> 完成 -> 复核交接 -> 自动建账
case = client.post('/cases', headers=h_op,
                   json={'title': '演示盘点', 'payload': domain.SPEC['sample']}).json()
client.post(f"/cases/{case['id']}/runs", headers=h_op)
run_id, token, payload = workflow.claim()
workflow.finish(run_id, token, result=domain.solve(payload, engine))
ev = client.get('/evidence', headers=h_op).json()[0]
check('复核前台账为空', client.get('/ledgers', headers=h_op).json() == [])
reviewed = client.post(f"/evidence/{ev['id']}/review", headers=h_rv,
                       json={'note': '同意交接'}).json()
check('复核返回台账号', 'ledger_id' in reviewed)
ledger = client.get('/ledgers', headers=h_op).json()[0]
check('台账初始进行中', ledger['status'] == LedgerStatus.IN_PROGRESS)
detail = client.get(f"/ledgers/{ledger['id']}", headers=h_op).json()
check('台账条目数=清单2条', len(detail['items']) == 2)
item1, item2 = detail['items']
check('条目初始卡在复制校验', item1['steps'][0]['state'] == 'waiting'
      and item1['step'] == MoveStep.COPY)


def receipt(lid, iid, step, outcome, content, headers=h_op, note=''):
    return client.post(f'/ledgers/{lid}/items/{iid}/receipts', headers=headers,
                       json={'step': step, 'outcome': outcome, 'content': content, 'note': note})


# 2) 乱序回执不能跳步
r = receipt(ledger['id'], item1['id'], MoveStep.SWITCH, ReceiptOutcome.SUCCESS, {'x': 1})
check('跳步被拒(409)', r.status_code == 409, r.text)

# 3) 正常三步：复制校验 -> 归属切换 -> 源副本清理
content_ok = {'checksum': 'abc', 'bytes': item1['size_bytes'], 'target': item1['target_node']}
r = receipt(ledger['id'], item1['id'], MoveStep.COPY, ReceiptOutcome.SUCCESS, content_ok)
check('复制校验成功', r.status_code == 201 and r.json()['steps'][0]['state'] == 'done', r.text)
d = client.get(f"/ledgers/{ledger['id']}", headers=h_op).json()
check('复制后目标先计占用、源未释放 (NORTH=135M, OLD=140M)',
      d['live_used_bytes']['NORTH'] == 135_000_000 and d['live_used_bytes']['OLD'] == 140_000_000,
      str(d['live_used_bytes']))
r = receipt(ledger['id'], item1['id'], MoveStep.SWITCH, ReceiptOutcome.SUCCESS,
            {'owner_before': 'OLD', 'owner_after': 'NORTH'})
check('归属切换成功', r.status_code == 201)
d = client.get(f"/ledgers/{ledger['id']}", headers=h_op).json()
check('切换后归属已变、占用不变',
      d['live_owners']['TY-1121-02'] == 'NORTH' and d['live_used_bytes']['OLD'] == 140_000_000)
check('切换未清理时源侧在途占用 70M', d['inflight_reserved_bytes']['OLD'] == 70_000_000)
r = receipt(ledger['id'], item1['id'], MoveStep.COPY, ReceiptOutcome.SUCCESS, content_ok)
check('已完成步骤重复登记被拒(409)', r.status_code == 409)
r = receipt(ledger['id'], item1['id'], MoveStep.CLEANUP, ReceiptOutcome.SUCCESS,
            {'freed_bytes': item1['size_bytes']})
check('清理确认成功', r.status_code == 201)
d = client.get(f"/ledgers/{ledger['id']}", headers=h_op).json()
check('清理后源库才释放: OLD=70M(仅剩item2两批+其他)',
      d['live_used_bytes']['OLD'] == 70_000_000, str(d['live_used_bytes']))

# 4) 重试只生效一次 + 内容冲突
bad = {'checksum': 'bad', 'bytes': item2['size_bytes']}
r = receipt(ledger['id'], item2['id'], MoveStep.COPY, ReceiptOutcome.FAILURE, bad)
check('初次失败进入重试态', r.status_code == 201 and r.json()['steps'][0]['state'] == 'retrying', r.text)
r = receipt(ledger['id'], item2['id'], MoveStep.COPY, ReceiptOutcome.SUCCESS, bad)
check('同内容换结果=回执冲突(409)', r.status_code == 409, r.text)
r = receipt(ledger['id'], item2['id'], MoveStep.COPY, ReceiptOutcome.FAILURE, bad)
check('同内容同结果=重复回执(409)', r.status_code == 409, r.text)
r = receipt(ledger['id'], item2['id'], MoveStep.COPY, ReceiptOutcome.SUCCESS,
            {'checksum': 'fixed', 'bytes': item2['size_bytes']})
check('新事实的重试放行', r.status_code == 201 and r.json()['steps'][0]['state'] == 'done', r.text)
r = receipt(ledger['id'], item2['id'], MoveStep.SWITCH, ReceiptOutcome.FAILURE, {'owner_after': 'WEST'})
check('切换初次失败', r.status_code == 201 and r.json()['steps'][1]['state'] == 'retrying')
r = receipt(ledger['id'], item2['id'], MoveStep.SWITCH, ReceiptOutcome.FAILURE,
            {'owner_after': 'WEST', 'reason': 'peer-reject'})
check('重试仍失败则阻塞(failed)', r.status_code == 201 and r.json()['steps'][1]['state'] == 'failed', r.text)
r = receipt(ledger['id'], item2['id'], MoveStep.SWITCH, ReceiptOutcome.SUCCESS, {'owner_after': 'WEST'})
check('阻塞后不再接收回执(409)', r.status_code == 409)
d = client.get(f"/ledgers/{ledger['id']}", headers=h_op).json()
check('台账状态 blocked', d['status'] == LedgerStatus.BLOCKED)
blocked_item = next(x for x in d['items'] if x['id'] == item2['id'])
check('页面可看出卡在归属切换',
      any(s['state'] in ('failed', 'blocked') and s['label'] == '归属切换'
          for s in blocked_item['steps']))

# 5) 重排：同一时点归属与占用；试算期间并发回执不影响已返回结果
replan = client.post(f"/ledgers/{ledger['id']}/replan", headers=h_op,
                     json={'budget': 150_000_000}).json()
check('重排输入通过校验且产出清单', 'sections' in replan and replan['replan_basis']['ledger_id'])
check('重排把在途占用纳入临时占用', 'inflight_reserved_bytes' in replan['replan_basis'])

case2 = client.post('/cases', headers=h_op,
                    json={'title': '第二盘点', 'payload': domain.SPEC['sample']}).json()
client.post(f"/cases/{case2['id']}/runs", headers=h_op)
rid2, tok2, pay2 = workflow.claim()
workflow.finish(rid2, tok2, result=domain.solve(pay2, engine))
ev2 = client.get('/evidence', headers=h_op).json()[0]
client.post(f"/evidence/{ev2['id']}/review", headers=h_rv, json={'note': '交接2'})
led2 = client.get('/ledgers', headers=h_op).json()[0]
det2 = client.get(f"/ledgers/{led2['id']}", headers=h_op).json()
it_a, it_b = det2['items']
rp1 = client.post(f"/ledgers/{led2['id']}/replan", headers=h_op, json={'budget': 0}).json()
check('零预算重排无搬迁任务',
      all(row['判定'] != '本轮搬迁' for section in rp1['sections']
          if section['title'] == '批次归属比较' for row in section['rows']))
check('重排起点 NORTH 仅其他占用 65M', rp1['replan_basis']['live_used_bytes']['NORTH'] == 65_000_000)
# 模拟试算期间并发回执到达：已返回的 rp1 不被篡改
receipt(led2['id'], it_a['id'], MoveStep.COPY, ReceiptOutcome.SUCCESS,
        {'checksum': 'x', 'bytes': it_a['size_bytes']})
check('并发回执不篡改已返回重排', rp1['replan_basis']['live_used_bytes']['NORTH'] == 65_000_000)
rp2 = client.post(f"/ledgers/{led2['id']}/replan", headers=h_op, json={'budget': 150_000_000}).json()
check('下次重排纳入新回执: NORTH=135M 且在途 70M',
      rp2['replan_basis']['live_used_bytes']['NORTH'] == 135_000_000
      and rp2['replan_basis']['inflight_reserved_bytes']['NORTH'] == 70_000_000,
      str(rp2['replan_basis']))

# 6) 权限：复核员不能登记回执
r = receipt(led2['id'], it_b['id'], MoveStep.COPY, ReceiptOutcome.SUCCESS, {'k': 1}, headers=h_rv)
check('复核员无权登记回执(403)', r.status_code == 403)

# 7) 全量完成后的账面：两条都成功时 OLD 批次占用归零，其他占用 20M
receipt(led2['id'], it_a['id'], MoveStep.SWITCH, ReceiptOutcome.SUCCESS, {'o': 1})
receipt(led2['id'], it_a['id'], MoveStep.CLEANUP, ReceiptOutcome.SUCCESS, {'f': 1})
pb = {'checksum': 'y', 'bytes': it_b['size_bytes']}
receipt(led2['id'], it_b['id'], MoveStep.COPY, ReceiptOutcome.SUCCESS, pb)
receipt(led2['id'], it_b['id'], MoveStep.SWITCH, ReceiptOutcome.SUCCESS, {'o': 2})
receipt(led2['id'], it_b['id'], MoveStep.CLEANUP, ReceiptOutcome.SUCCESS, {'f': 2})
d2 = client.get(f"/ledgers/{led2['id']}", headers=h_op).json()
check('全部成功台账 complete', d2['status'] == LedgerStatus.COMPLETE)
check('OLD 批次占用归零仅余其他占用20M', d2['live_used_bytes']['OLD'] == 20_000_000, str(d2['live_used_bytes']))
check('NORTH 135M / WEST 95M',
      d2['live_used_bytes']['NORTH'] == 135_000_000 and d2['live_used_bytes']['WEST'] == 95_000_000)
check('完成后无在途占用', all(v == 0 for v in d2['inflight_reserved_bytes'].values()))
r = receipt(led2['id'], it_b['id'], MoveStep.COPY, ReceiptOutcome.SUCCESS, pb)
check('完成台账拒收新回执(409)', r.status_code == 409)

# 8) 审计事件落账
audit_rows = client.get('/audit', headers=h_op).json()
actions = {row['action'] for row in audit_rows}
check('审计含建账/回执/重排事件',
      {'create_ledger', f'receipt_{MoveStep.COPY}', 'replan'} <= actions, str(sorted(actions)))

print(f'\n{PASS} passed, {FAIL} failed')
sys.exit(1 if FAIL else 0)
