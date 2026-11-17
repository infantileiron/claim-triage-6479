from datetime import datetime, timezone
from src.models.entities import Audit


def audit(s, u, action):
    s.add(Audit(actor=u.email, action=action, created=datetime.now(timezone.utc).isoformat()))
    s.commit()


def dump(obj):
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns if c.name != 'password'}


def trainable_rows(s, recon_snapshot_id=None):
    """把票据与某一版对账依据合成为训练行。只在训练提交/重试时调用。

    返回 (rows, excluded, meta)。资格判定来自依据版本里冻结的对账结果：
    未结清、超付、冲正挂起的正常样本排除，异常标签照常保留，待标注样本参与评分。
    读取后复核依据版本未变化，保证并发导入/票据修订时只取同一版依据。
    """
    from src.models.entities import Claim, ReconSnapshot
    from src.services.reconciliation import claim_cents, snapshot_checksum
    snap = None
    db_claims = []
    for _ in range(3):
        if recon_snapshot_id:
            snap = s.get(ReconSnapshot, recon_snapshot_id)
        else:
            snap = s.query(ReconSnapshot).order_by(ReconSnapshot.id.desc()).first()
        if not snap:
            raise ValueError('请先导入支付流水完成对账，再提交训练')
        db_claims = s.query(Claim).order_by(Claim.id).all()
        latest_id = s.query(ReconSnapshot.id).order_by(ReconSnapshot.id.desc()).limit(1).scalar()
        snap_matches = all(
            c.code in snap.payload['claims'] and snap.payload['claims'][c.code]['amount_cents'] == claim_cents(c.amount)
            for c in db_claims
        )
        if (recon_snapshot_id or latest_id) == snap.id and snap_matches:
            break
        s.expire_all()
    else:
        raise ValueError('对账依据正在更新，请稍后重新提交训练')
    frozen = snap.payload['claims']
    rows, excluded = [], []
    for c in db_claims:
        recon = frozen.get(c.code) or {
            'status': 'unsettled', 'amount_cents': 0, 'paid_cents': 0,
            'difference_cents': 0, 'reason': '该票据在依据版本生成后新增，尚未对账',
        }
        if c.label == 1 or c.label is None or recon['status'] == 'settled':
            rows.append(dump(c))
        else:
            excluded.append({
                'id': c.id, 'code': c.code, 'label': c.label, 'status': recon['status'],
                'amount_cents': recon['amount_cents'], 'paid_cents': recon['paid_cents'],
                'difference_cents': recon['difference_cents'], 'reason': recon['reason'],
            })
    summary = {}
    for e in excluded:
        summary.setdefault(e['status'], {'status': e['status'], 'count': 0, 'reason': e['reason']})['count'] += 1
    meta = {
        'recon_snapshot_id': snap.id,
        'recon_checksum': snapshot_checksum(snap),
        'recon_created': snap.created,
        'basis': f'对账依据固定为版本 #{snap.id}，后续导入不会改变本版本训练输入',
        'exclusion_summary': sorted(summary.values(), key=lambda x: x['status']),
        'excluded_count': len(excluded),
    }
    return rows, excluded, meta
