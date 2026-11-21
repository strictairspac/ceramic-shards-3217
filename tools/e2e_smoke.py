"""真实 HTTP 服务端到端冒烟（urllib，不依赖第三方客户端）。"""
import json
import os
import sys
import urllib.request

BASE = 'http://127.0.0.1:18999'
os.environ.update({'AUTH_KEY': 'e2e-key', 'DB_DRIVER': 'sqlite', 'DB_NAME': '/tmp/e2e/w.db',
                   'DB_USER': '', 'DB_PASSWORD': '', 'DB_HOST': '',
                   'REDIS_URL': 'redis://127.0.0.1:6399/0'})
sys.path.insert(0, '/workspace/backend')

P = F = 0
def check(name, cond, detail=''):
    global P, F
    if cond: P += 1; print('  PASS', name)
    else: F += 1; print('  FAIL', name, detail)

def call(method, path, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    if token:
        req.add_header('Authorization', 'Bearer ' + token)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b'null')
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b'{}')

def login(u):
    return call('POST', '/login', body={'username': u, 'password': 'pw'})[1]['token']

op, rv, vw = login('operator'), login('reviewer'), login('viewer')
from src.services import domain  # noqa: E402

cid = call('POST', '/cases', op, {'title': 'E2E盘点', 'payload': domain.SPEC['sample']})[1]['id']
call('POST', f'/cases/{cid}/runs', op)
# worker 未运行，直接驱动一次 solve 落证据（与生产 worker 同一入口）。
from src.services import workflow  # noqa: E402
rid, tok, pay = workflow.claim()
workflow.finish(rid, tok, result=domain.solve(pay, None))
eid = call('GET', '/evidence', op)[1][0]['id']
code, reviewed = call('POST', f'/evidence/{eid}/review', rv, {'note': '交接'})
check('复核交接自动建账', code == 200 and 'ledger_id' in reviewed, str(reviewed))
lid = call('GET', '/ledgers', op)[1][0]['id']
detail = call('GET', f'/ledgers/{lid}', op)[1]
i1, i2 = detail['items'][0]['id'], detail['items'][1]['id']

def receipt(iid, step, outcome, content):
    return call('POST', f'/ledgers/{lid}/items/{iid}/receipts', op,
                {'step': step, 'outcome': outcome, 'content': content, 'note': ''})

# 条目1：乱序拒收 -> 正常三步 -> 重复拒收
check('跳步 409', receipt(i1, 'ownership_switched', 'success', {})[0] == 409)
check('复制成功 201', receipt(i1, 'copy_verified', 'success', {'checksum': 'a1'})[0] == 201)
d = call('GET', f'/ledgers/{lid}', op)[1]
check('复制后目标 NORTH=135M、源 OLD=140M',
      d['live_used_bytes']['NORTH'] == 135_000_000 and d['live_used_bytes']['OLD'] == 140_000_000,
      str(d['live_used_bytes']))
check('切换成功', receipt(i1, 'ownership_switched', 'success', {'after': 'NORTH'})[0] == 201)
check('清理成功', receipt(i1, 'source_cleaned', 'success', {'freed': 70_000_000})[0] == 201)
d = call('GET', f'/ledgers/{lid}', op)[1]
check('清理后 OLD 释放到 70M', d['live_used_bytes']['OLD'] == 70_000_000)
check('完成步重复登记 409', receipt(i1, 'copy_verified', 'success', {'checksum': 'a1'})[0] == 409)

# 条目2：失败->重试一次->冲突/重复判定->新事实放行
check('首次失败 201 进入重试', receipt(i2, 'copy_verified', 'failure', {'checksum': 'bad'})[0] == 201)
check('同内容同结果重复 409', receipt(i2, 'copy_verified', 'failure', {'checksum': 'bad'})[0] == 409)
check('同内容换结果冲突 409', receipt(i2, 'copy_verified', 'success', {'checksum': 'bad'})[0] == 409)
check('新事实重试放行 201', receipt(i2, 'copy_verified', 'success', {'checksum': 'ok2'})[0] == 201)
check('切换失败', receipt(i2, 'ownership_switched', 'failure', {'reason': 't'})[0] == 201)
check('切换再失败阻塞', receipt(i2, 'ownership_switched', 'failure', {'reason': 't2'})[0] == 201)
check('阻塞后拒收 409', receipt(i2, 'ownership_switched', 'success', {})[0] == 409)
d = call('GET', f'/ledgers/{lid}', op)[1]
check('台账 blocked', d['status'] == 'blocked')
check('可见卡在归属切换', any(s['label'] == '归属切换' and s['state'] == 'failed'
                               for it in d['items'] for s in it['steps']))

# 重排：在途占用纳入；权限
code, replan = call('POST', f'/ledgers/{lid}/replan', op, {'budget': 150_000_000})
check('重排 200 三段齐全', code == 200 and len(replan['sections']) == 3)
check('重排含在途占用口径', 'inflight_reserved_bytes' in replan['replan_basis'])
check('viewer 重排 403', call('POST', f'/ledgers/{lid}/replan', vw, {'budget': 1})[0] == 403)
check('viewer 回执 403', call('POST', f'/ledgers/{lid}/items/{i2}/receipts', vw,
                              {'step': 'copy_verified', 'outcome': 'success', 'content': {}})[0] == 403)
print(f'\n{P} passed, {F} failed')
sys.exit(1 if F else 0)
