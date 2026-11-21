"""边界：零预算/无归属变化产生空清单时，复核建账为 complete 且无条目，重排仍可预览。"""
import os, sys, tempfile
_tmp = tempfile.mkdtemp(prefix='ledger-edge-')
os.environ.update({'AUTH_KEY': 'k', 'DB_DRIVER': 'sqlite',
                   'DB_NAME': f'{_tmp}/w.db', 'DB_USER': '', 'DB_PASSWORD': '', 'DB_HOST': '',
                   'REDIS_URL': 'redis://127.0.0.1:6399/0'})
sys.path.insert(0, '/workspace/backend')
from sqlalchemy import event
from fastapi.testclient import TestClient
from src.config.settings import engine
from src.models.tables import schema, users
from src.services import domain, workflow
from src.services.security import hash_password
from src.main import app

@event.listens_for(engine, 'connect')
def _pragmas(conn, _):
    cur = conn.cursor(); cur.execute('PRAGMA busy_timeout=5000'); cur.execute('PRAGMA journal_mode=WAL'); cur.close()

P = F = 0
def check(n, c, d=''):
    global P, F
    if c: P += 1; print('  PASS', n)
    else: F += 1; print('  FAIL', n, d)

schema.drop_all(engine); schema.create_all(engine)
with engine.begin() as conn:
    for role in ('operator', 'reviewer'):
        conn.execute(users.insert().values(username=role, role=role, password_hash=hash_password('pw')))
c = TestClient(app)
op = {'Authorization': 'Bearer ' + c.post('/login', json={'username': 'operator', 'password': 'pw'}).json()['token']}
rv = {'Authorization': 'Bearer ' + c.post('/login', json={'username': 'reviewer', 'password': 'pw'}).json()['token']}

# 零预算样例：仍有归属变化但本轮无任何可搬条目 -> 空清单
payload = dict(domain.SPEC['sample'], round_budget_bytes=0)
case = c.post('/cases', headers=op, json={'title': '零预算', 'payload': payload}).json()
c.post(f"/cases/{case['id']}/runs", headers=op)
rid, tok, pay = workflow.claim()
workflow.finish(rid, tok, result=domain.solve(pay, engine))
eid = c.get('/evidence', headers=op).json()[0]['id']
r = c.post(f'/evidence/{eid}/review', headers=rv, json={'note': '空清单交接'})
check('空清单复核建账 200', r.status_code == 200, r.text)
lid = r.json()['ledger_id']
det = c.get(f'/ledgers/{lid}', headers=op).json()
check('空清单台账直接 complete', det['status'] == 'complete')
check('空清单无条目', det['items'] == [])
check('账面仍重放节点占用', det['live_used_bytes']['OLD'] == 140_000_000)
# 重排零预算预览可用
rp = c.post(f'/ledgers/{lid}/replan', headers=op, json={'budget': 0})
check('空台账重排预览 200', rp.status_code == 200 and rp.json()['metrics']['计划搬迁批次'] == 0)
print(f'\n{P} passed, {F} failed')
sys.exit(1 if F else 0)
