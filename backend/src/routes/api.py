import json
import os

import redis
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from src.config.database import db
from src.controllers.business import ensure_training
from src.middlewares.auth import current, editor, admin
from src.models.entities import Audit, Claim, PaymentBatch, Review, Training, User
from src.schemas.business import ClaimInput, Login, PaymentBatchInput, ReviewInput, TrainingInput
from src.services.business import audit, dump, trainable_rows
from src.services.reconciliation import (
    ledger_payments,
    latest_snapshot,
    now,
    publish_snapshot,
    recon_lock,
    reconciliation_view,
)
from src.utils.security import token, verify

router = APIRouter()


@router.post('/auth/login')
def login(data: Login, s=Depends(db)):
    u = s.query(User).filter_by(email=data.email).first()
    if not u or not verify(data.password, u.password):
        raise HTTPException(401, '账号或密码错误')
    return {'token': token(u), 'user': dump(u)}


@router.get('/me')
def me(u=Depends(current)):
    return dump(u)


@router.get('/claims')
def claims(u=Depends(current), s=Depends(db)):
    return [dump(r) for r in s.query(Claim).order_by(Claim.id).all()]


@router.post('/claims')
def create_claim(data: ClaimInput, u=Depends(editor), s=Depends(db)):
    r = Claim(**data.model_dump())
    s.add(r)
    try:
        s.commit()
    except IntegrityError:
        s.rollback()
        raise HTTPException(409, '报销编号重复')
    _publish_if_ledger(s, f'新增票据 {r.code}')
    audit(s, u, '录入报销 ' + r.code)
    return dump(r)


@router.put('/claims/{i}')
def edit_claim(i: int, data: ClaimInput, u=Depends(editor), s=Depends(db)):
    r = s.get(Claim, i)
    if not r:
        raise HTTPException(404, '报销样本不存在')
    for k, v in data.model_dump().items():
        setattr(r, k, v)
    try:
        s.commit()
    except IntegrityError:
        s.rollback()
        raise HTTPException(409, '报销编号重复')
    _publish_if_ledger(s, f'修订票据 {r.code}')
    audit(s, u, '修订报销 ' + str(i))
    return dump(r)


def _publish_if_ledger(s, reason):
    """票据变化会影响资格判定；已有流水时发布新依据版本，保持整批生效。"""
    if not latest_snapshot(s):
        return
    with recon_lock(s):
        s.commit()
        publish_snapshot(s, None, reason)
        s.commit()


def batch_view(b):
    return {
        'id': b.id,
        'status': b.status,
        'source': b.source,
        'summary': b.summary or {},
        'errors': b.errors or [],
        'recon_snapshot_id': b.snapshot_id,
        'created': b.created,
        'completed': b.completed,
        'row_count': len(b.payload or []),
    }


@router.post('/payments/batches')
def import_payments(data: PaymentBatchInput, u=Depends(editor), s=Depends(db)):
    raw = [p.model_dump() for p in data.payments]
    batch = PaymentBatch(status='received', source=data.source, payload=raw, created=now())
    s.add(batch)
    s.commit()
    try:
        redis.Redis.from_url(os.environ['REDIS_URL']).rpush('claim:payments', batch.id)
    except redis.RedisError:
        batch.status = 'failed'
        batch.errors = [{'index': None, 'payment_no': None, 'code': 'queue',
                         'message': '队列暂不可用，原始行已保留，可稍后按原批恢复'}]
        batch.completed = now()
        s.commit()
        audit(s, u, f'支付流水导入 #{batch.id} 入队失败')
        return JSONResponse(status_code=503, content={'detail': '队列暂不可用，原始行已保留，请稍后按原批恢复',
                                                      'batch': batch_view(batch)})
    audit(s, u, f'支付流水导入 #{batch.id} 已接收')
    # 接收状态立即可见；后台 worker 负责完成或失败
    return JSONResponse(status_code=202, content=batch_view(batch))


@router.get('/payments/batches')
def batches(u=Depends(current), s=Depends(db)):
    return [batch_view(b) for b in s.query(PaymentBatch).order_by(PaymentBatch.id.desc()).all()]


@router.get('/payments/batches/{i}')
def batch_detail(i: int, u=Depends(current), s=Depends(db)):
    b = s.get(PaymentBatch, i)
    if not b:
        raise HTTPException(404, '导入批次不存在')
    return batch_view(b)


@router.post('/payments/batches/{i}/retry')
def retry_batch(i: int, u=Depends(editor), s=Depends(db)):
    b = s.get(PaymentBatch, i)
    if not b:
        raise HTTPException(404, '导入批次不存在')
    if b.status == 'completed':
        return batch_view(b)  # 幂等：已完成批次重试不再计款
    if b.status == 'received' and not b.errors:
        return JSONResponse(status_code=202, content=batch_view(b))  # 已在队列中
    # failed / 中断的 received：原始行都保留在原批上，接着原批恢复
    b.status = 'received'
    b.errors = []
    s.commit()
    try:
        redis.Redis.from_url(os.environ['REDIS_URL']).rpush('claim:payments', b.id)
    except redis.RedisError:
        b.status = 'failed'
        b.errors = [{'index': None, 'payment_no': None, 'code': 'queue',
                     'message': '队列暂不可用，原始行已保留，可稍后按原批恢复'}]
        s.commit()
        return JSONResponse(status_code=503, content={'detail': '队列暂不可用', 'batch': batch_view(b)})
    audit(s, u, f'支付流水导入 #{b.id} 按原批恢复')
    return JSONResponse(status_code=202, content=batch_view(b))


@router.get('/payments')
def payments(u=Depends(current), s=Depends(db)):
    return ledger_payments(s)


@router.get('/reconciliation')
def reconciliation(claim_code: str | None = None, status: str | None = None, u=Depends(current), s=Depends(db)):
    return reconciliation_view(s, claim_code, status)


@router.get('/reconciliation/claims/{code}')
def reconciliation_claim(code: str, u=Depends(current), s=Depends(db)):
    view = reconciliation_view(s, code)
    if not view['claims']:
        raise HTTPException(404, '票据不存在或尚未对账')
    return view['claims'][0]


@router.get('/trainings')
def trainings(u=Depends(current), s=Depends(db)):
    return [dump(r) for r in s.query(Training).order_by(Training.id.desc()).all()]


def _submit_training(s, data, u):
    try:
        rows, excluded, meta = trainable_rows(s)
    except ValueError as e:
        raise HTTPException(409, str(e))
    if not rows:
        raise HTTPException(409, '没有可入训的样本')
    r = Training(**data.model_dump(), recon_snapshot_id=meta['recon_snapshot_id'],
                 result={'snapshot': rows, 'excluded': excluded, 'recon': meta})
    s.add(r)
    s.commit()
    try:
        redis.Redis.from_url(os.environ['REDIS_URL']).rpush('claim:trainings', r.id)
    except redis.RedisError:
        r.state = 'failed'
        r.error = '队列暂不可用，请在原批次上重试；不会重复计款'
        s.commit()
        raise HTTPException(503, r.error)
    return r


@router.post('/trainings')
def train(data: TrainingInput, u=Depends(editor), s=Depends(db)):
    r = _submit_training(s, data, u)
    audit(s, u, f'提交训练 {r.id}（对账依据 #{r.recon_snapshot_id}）')
    return dump(r)


@router.get('/trainings/{i}')
def detail(i: int, u=Depends(current), s=Depends(db)):
    return dump(ensure_training(s, i))


@router.get('/trainings/{i}/export')
def export(i: int, u=Depends(current), s=Depends(db)):
    r = ensure_training(s, i)
    if r.state != 'done':
        raise HTTPException(409, '仅成功训练可导出')
    return Response(json.dumps(dump(r), ensure_ascii=False, indent=2), media_type='application/json',
                    headers={'Content-Disposition': f'attachment; filename="claim-triage-{i}.json"'})


@router.post('/trainings/{i}/reviews')
def generate(i: int, u=Depends(editor), s=Depends(db)):
    r = ensure_training(s, i)
    if r.state != 'done':
        raise HTTPException(409, '训练完成后才能生成复核任务')

    created = 0
    for p in r.result['pending']:
        if p['score'] >= r.threshold and not s.query(Review).filter_by(training_id=i, claim_id=p['id']).first():
            s.add(Review(training_id=i, claim_id=p['id'], score=p['score']))
            created += 1
    s.commit()
    audit(s, u, '生成风险复核 ' + str(i))
    return {'created': created}


@router.get('/reviews')
def reviews(u=Depends(current), s=Depends(db)):
    return [dump(r) for r in s.query(Review).order_by(Review.id.desc()).all()]


@router.put('/reviews/{i}')
def review(i: int, data: ReviewInput, u=Depends(editor), s=Depends(db)):
    r = s.get(Review, i)
    if not r:
        raise HTTPException(404, '复核任务不存在')
    if r.status == 'approved':
        raise HTTPException(409, '已签核复核不可修改')
    r.decision = data.decision
    r.note = data.note
    s.commit()
    audit(s, u, '人工复核 ' + str(i))
    return dump(r)


@router.post('/reviews/{i}/approve')
def approve(i: int, u=Depends(admin), s=Depends(db)):
    r = s.get(Review, i)
    if not r:
        raise HTTPException(404, '复核任务不存在')
    if r.decision == 'pending' or r.status != 'draft':
        raise HTTPException(409, '先填写人工判断，且只签核一次')
    r.status = 'approved'
    s.commit()
    audit(s, u, '签核复核 ' + str(i))
    return dump(r)


@router.get('/audits')
def audits(u=Depends(admin), s=Depends(db)):
    return [dump(r) for r in s.query(Audit).order_by(Audit.id.desc()).limit(100)]
