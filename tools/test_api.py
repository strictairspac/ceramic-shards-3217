"""HTTP 接口级验证：路由注册、权限、Pydantic 校验、错误码与并发读取。"""
import os, sys, tempfile, threading, json
_tmp = tempfile.mkdtemp(prefix='ledger-api-')
os.environ.update({'AUTH_KEY': 'k', 'DB_DRIVER': 'sqlite',
                   'DB_NAME': f'{_tmp}/w.db', 'DB_USER': '', 'DB_PASSWORD': '', 'DB_HOST': '',
                   'REDIS_URL': 'redis://127.0.0.1:6399/0'})
sys.path.insert(0, '/workspace/backend')
from fastapi.testclient import TestClient
from sqlalchemy import event
from src.config.settings import engine
from src.models.tables import schema, users
from src.services import domain, workflow
from src.services.security import hash_password
from src.main import app

# 文件型 SQLite 的写锁串行化：等待而非立即报 locked（生产 MySQL 由 FOR UPDATE 行锁处理）。
@event.listens_for(engine, 'connect')
def _sqlite_pragmas(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute('PRAGMA busy_timeout=5000')
    cursor.execute('PRAGMA journal_mode=WAL')
    cursor.close()

P = F = 0
def check(name, cond, detail=''):
    global P, F
    if cond: P += 1; print('  PASS', name)
    else: F += 1; print('  FAIL', name, detail)

schema.drop_all(engine); schema.create_all(engine)
with engine.begin() as conn:
    for role in ('operator', 'reviewer', 'viewer'):
        conn.execute(users.insert().values(username=role, role=role, password_hash=hash_password('pw')))
c = TestClient(app)
def token(u): return {'Authorization': 'Bearer ' + c.post('/login', json={'username': u, 'password': 'pw'}).json()['token']}
ho, hr, hv = token('operator'), token('reviewer'), token('viewer')

# 未认证 401
check('未认证访问台账 401', c.get('/ledgers').status_code == 401)
# 建档试算复核
case = c.post('/cases', headers=ho, json={'title': 'p', 'payload': domain.SPEC['sample']}).json()
c.post(f"/cases/{case['id']}/runs", headers=ho)
rid, tok, pay = workflow.claim()
workflow.finish(rid, tok, result=domain.solve(pay, engine))
ev = c.get('/evidence', headers=ho).json()[0]
# viewer 不能复核
check('viewer 复核 403', c.post(f"/evidence/{ev['id']}/review", headers=hv, json={'note': 'x'}).status_code == 403)
c.post(f"/evidence/{ev['id']}/review", headers=hr, json={'note': 'ok'})
led = c.get('/ledgers', headers=hv).json()[0]
det = c.get(f"/ledgers/{led['id']}", headers=hv).json()
iid = det['items'][0]['id']
# viewer/reviewer 不能登记回执
check('viewer 登记回执 403', c.post(f"/ledgers/{led['id']}/items/{iid}/receipts",
      headers=hv, json={'step': 'copy_verified', 'outcome': 'success', 'content': {}}).status_code == 403)
check('reviewer 登记回执 403', c.post(f"/ledgers/{led['id']}/items/{iid}/receipts",
      headers=hr, json={'step': 'copy_verified', 'outcome': 'success', 'content': {}}).status_code == 403)
# 非法步骤/结果 422
r = c.post(f"/ledgers/{led['id']}/items/{iid}/receipts", headers=ho,
           json={'step': 'nope', 'outcome': 'success', 'content': {}})
check('非法步骤 422', r.status_code == 422, r.text)
r = c.post(f"/ledgers/{led['id']}/items/{iid}/receipts", headers=ho,
           json={'step': 'copy_verified', 'outcome': 'boom', 'content': {}})
check('非法结果 422', r.status_code == 422)
# 不存在条目 404
check('不存在条目 404', c.post(f"/ledgers/{led['id']}/items/9999/receipts", headers=ho,
      json={'step': 'copy_verified', 'outcome': 'success', 'content': {}}).status_code == 404)
# 并发：两个线程同时提交「相同内容」的同步骤首步回执。
# 不变量：恰好一条被接受，另一条被拒，且库里同一步骤只有一条回执（不出现重复落账）。
results = []
barrier = threading.Barrier(2)
def worker():
    barrier.wait()  # 尽量让两个请求同时进入，制造真实竞争
    code = c.post(f"/ledgers/{led['id']}/items/{iid}/receipts", headers=ho,
                  json={'step': 'copy_verified', 'outcome': 'success',
                        'content': {'checksum': 'same-content'}}).status_code
    results.append(code)
ts = [threading.Thread(target=worker) for _ in range(2)]
[t.start() for t in ts]; [t.join() for t in ts]
check('并发相同回执：恰好一条 201、一条拒绝(409/冲突)',
      sorted(results) == [201, 409], str(results))
from src.models.tables import receipts as receipts_table, ledger_items as items_table
from sqlalchemy import select as _select, func
with engine.connect() as conn:
    count = conn.execute(_select(func.count()).select_from(receipts_table).where(
        receipts_table.c.item_id == iid,
        receipts_table.c.step == 'copy_verified')).scalar_one()
check('并发后同步骤仅一条回执落账（无重复）', count == 1, f'count={count}')
# 重排权限与非法预算
check('viewer 重排 403', c.post(f"/ledgers/{led['id']}/replan", headers=hv, json={'budget': 1}).status_code == 403)
check('负预算 422', c.post(f"/ledgers/{led['id']}/replan", headers=ho, json={'budget': -1}).status_code == 422)
r = c.post(f"/ledgers/{led['id']}/replan", headers=ho, json={'budget': 150_000_000})
check('重排 200 且含完整报告', r.status_code == 200 and 'sections' in r.json() and 'replan_basis' in r.json())
# 重排输出仍是标准 solve 结构，可直接复用证据渲染
rp = r.json()
check('重排报告含清单/归属/占用三段', {s['title'] for s in rp['sections']} ==
      {'本轮搬迁清单', '批次归属比较', '节点物理占用'})
# 审计可查
actions = {x['action'] for x in c.get('/audit', headers=ho).json()}
check('审计可查回执与重排', 'receipt_copy_verified' in actions and 'replan' in actions and 'create_ledger' in actions)
print(f'\n{P} passed, {F} failed')
sys.exit(1 if F else 0)
