from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import text

from src.config.settings import cache, engine
from src.controllers.workflow import download
from src.models.tables import audit, cases, evidence, runs
from src.schemas.workflow import CaseForm, LoginForm, ReceiptForm, ReviewForm
from src.services import domain, ledger, workflow
from src.services.security import current_user, login, operator, reviewer

router = APIRouter()


class ReplanForm(BaseModel):
    budget: int = Field(ge=0)


@router.get('/health')
def health():
    with engine.connect() as conn:
        conn.execute(text('SELECT 1')).scalar_one()
    cache.ping()
    return {'ok': True}


@router.post('/login')
def sign_in(form: LoginForm):
    return login(form.username, form.password)


@router.get('/spec')
def spec(_=Depends(current_user)):
    return domain.SPEC


@router.get('/cases')
def list_cases(_=Depends(current_user)):
    return workflow.recent(cases)


@router.post('/cases', status_code=201)
def create_case(form: CaseForm, actor=Depends(operator)):
    return workflow.make_case(form.title, form.payload, actor)


@router.get('/runs')
def list_runs(_=Depends(current_user)):
    return workflow.recent(runs)


@router.post('/cases/{case_id}/runs', status_code=202)
def start_run(case_id: int, actor=Depends(operator)):
    return workflow.enqueue(case_id, actor)


@router.get('/evidence')
def list_evidence(_=Depends(current_user)):
    return workflow.recent(evidence)


@router.get('/evidence/{identity}/download')
def export_evidence(identity: int, _=Depends(current_user)):
    return download(identity)


@router.post('/evidence/{identity}/review')
def review_evidence(identity: int, form: ReviewForm, actor=Depends(reviewer)):
    return workflow.review(identity, form.note, actor)


@router.get('/ledgers')
def list_move_ledgers(_=Depends(current_user)):
    with engine.connect() as conn:
        rows = ledger.list_ledgers(conn)
        for row in rows:
            evidence_row = conn.execute(
                evidence.select().where(evidence.c.id == row['evidence_id'])).mappings().first()
            row['evidence'] = dict(evidence_row) if evidence_row else None
        return rows


@router.get('/ledgers/{identity}')
def get_move_ledger(identity: int, _=Depends(current_user)):
    with engine.connect() as conn:
        return ledger.ledger_detail(conn, identity)


@router.post('/ledgers/{identity}/items/{item_id}/receipts', status_code=201)
def post_receipt(identity: int, item_id: int, form: ReceiptForm, actor=Depends(operator)):
    return ledger.register_receipt(identity, item_id, form, actor)


@router.post('/ledgers/{identity}/replan')
def replan_ledger(identity: int, form: ReplanForm, actor=Depends(operator)):
    """试算（重排）期间继续接收并发回执：只读取已提交时点的账本快照生成新一轮输入。"""
    with engine.connect() as conn:
        payload, used, reservations = ledger.replan_payload(conn, identity, form.budget)
    domain.validate(payload)
    result = domain.solve(payload, engine)
    result['replan_basis'] = {
        'ledger_id': identity,
        'snapshot_id': payload['snapshot_id'],
        'live_used_bytes': used,
        'inflight_reserved_bytes': reservations,
        'note': '重排清单基于读取时点的归属与占用；在途副本已计入临时占用，并发回执在下次重排时才纳入。',
    }
    workflow.log_replan(identity, actor)
    return result


@router.get('/audit')
def list_audit(_=Depends(current_user)):
    return workflow.recent(audit)
