"""支付流水对账：金额一律以整数分记账，逐票分摊，冲正可先到并挂起。

对账结果是纯函数 reconcile(claims, ledger_payments)：同一份流水快照 + 当前票据
必然得到同一版结论。每个完成的导入批次生成一份只追加不修改的 ReconSnapshot，
训练提交时固定引用其中一版，因此并发导入不会改变正在训练的依据。
"""
import hashlib
import json
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import text

from src.models.entities import Claim, Payment, PaymentAllocation, PaymentBatch, ReconSnapshot

CENT = Decimal('0.01')


class RowError(Exception):
    def __init__(self, message, code='invalid'):
        super().__init__(message)
        self.code = code


class BatchFailure(Exception):
    def __init__(self, errors, conflict=False):
        super().__init__('；'.join(e['message'] for e in errors))
        self.errors = errors
        self.conflict = conflict


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def recon_lock(s):
    """对账写入串行化：并发的批次不会各自基于残缺流水生成依据版本。

    锁占用独立的数据库连接，避免与业务事务的提交/回滚互相影响。
    """
    bind = s.get_bind()
    if bind.dialect.name != 'mysql':
        yield
        return
    conn = bind.connect()
    try:
        acquired = conn.execute(text("SELECT GET_LOCK('claim_recon_write', 15)")).scalar()
        if acquired != 1:
            raise BatchFailure([{'index': None, 'payment_no': None, 'code': 'busy', 'message': '另一批对账正在生效，请稍后重试'}])
        try:
            yield
        finally:
            conn.execute(text("SELECT RELEASE_LOCK('claim_recon_write')"))
            conn.commit()
    finally:
        conn.close()


def cents(value, field='金额'):
    """把元转为整数分；最多两位小数，拒绝静默四舍五入。"""
    try:
        d = Decimal(str(value))
    except Exception:
        raise RowError(f'{field}格式无效')
    if not d.is_finite():
        raise RowError(f'{field}格式无效')
    scaled = d * 100
    if scaled != scaled.to_integral_value():
        raise RowError(f'{field}最多两位小数，请按分录入')
    return int(scaled)


def claim_cents(amount):
    d = Decimal(str(amount)).quantize(CENT, rounding=ROUND_HALF_UP)
    return int(d * 100)


def yuan(c):
    return f'{c / 100:.2f}'


def fingerprint(kind, original_no, amount_cents, allocations):
    body = {
        'kind': kind,
        'original_no': original_no or '',
        'amount_cents': amount_cents,
        'allocations': sorted(
            ({'claim_code': code, 'amount_cents': amount} for code, amount in allocations),
            key=lambda x: (x['claim_code'], x['amount_cents']),
        ),
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def normalize_row(s, item, index, planned_kinds):
    no = str(item.get('payment_no') or '').strip()
    if not no:
        raise RowError('流水编号不能为空')
    kind = item.get('kind') or 'payment'
    if kind not in ('payment', 'reversal'):
        raise RowError('流水类型只能是 payment 或 reversal')
    original_no = (item.get('original_no') or '').strip() or None
    if kind == 'payment' and original_no:
        raise RowError(f'普通支付 {no} 不能引用原支付编号')
    if kind == 'reversal':
        if not original_no:
            raise RowError(f'冲正 {no} 必须填写原支付编号')
        if original_no == no:
            raise RowError(f'冲正 {no} 不能引用自身')
        target_kind = planned_kinds.get(original_no)
        if target_kind is None:
            existing = s.query(Payment).filter_by(payment_no=original_no).first()
            target_kind = existing.kind if existing else None
        if target_kind is not None and target_kind != 'payment':
            raise RowError(f'冲正 {no} 引用的 {original_no} 不是支付流水')
    try:
        amount_cents = cents(item.get('amount'), f'流水 {no} 金额')
    except RowError:
        raise
    if amount_cents < 0:
        raise RowError(f'流水 {no} 金额不能为负数')
    raw_allocations = item.get('allocations') or []
    if not isinstance(raw_allocations, list):
        raise RowError(f'流水 {no} 的分摊必须是列表')
    allocations = []
    seen_claims = set()
    for a in raw_allocations:
        code = str(a.get('claim_code') or '').strip()
        if not code:
            raise RowError(f'流水 {no} 的分摊缺少票据编号')
        if code in seen_claims:
            raise RowError(f'流水 {no} 对票据 {code} 重复分摊')
        seen_claims.add(code)
        try:
            share = cents(a.get('amount'), f'流水 {no} 对 {code} 的分摊')
        except RowError:
            raise
        if kind == 'payment' and share < 0:
            raise RowError(f'普通支付 {no} 对 {code} 的分摊不能为负数')
        if kind == 'reversal' and share > 0:
            raise RowError(f'冲正 {no} 对 {code} 的分摊必须为非正数')
        allocations.append((code, share))
    expected = amount_cents if kind == 'payment' else -amount_cents
    total = sum(a[1] for a in allocations)
    if total != expected:
        raise RowError(
            f'流水 {no} 分摊合计 {yuan(total)} 元与{"应分摊" if kind == "payment" else "应冲正"} '
            f'{yuan(expected)} 元不符（按分守恒）'
        )
    return {
        'payment_no': no,
        'kind': kind,
        'original_no': original_no,
        'amount_cents': amount_cents,
        'allocations': allocations,
        'fingerprint': fingerprint(kind, original_no, amount_cents, allocations),
    }


def normalize_batch(s, raw_payments):
    """先整体校验再写入；任何一行有错整批不生效。"""
    if not isinstance(raw_payments, list) or not raw_payments:
        raise BatchFailure([{'index': None, 'payment_no': None, 'code': 'empty', 'message': '导入内容不能为空'}])

    planned_kinds = {str(p.get('payment_no') or '').strip(): (p.get('kind') or 'payment') for p in raw_payments if isinstance(p, dict)}
    errors = []
    normalized = []
    seen = {}
    for index, item in enumerate(raw_payments):
        if not isinstance(item, dict):
            errors.append({'index': index, 'payment_no': None, 'code': 'invalid', 'message': f'第 {index + 1} 行不是对象'})
            continue
        try:
            row = normalize_row(s, item, index, planned_kinds)
        except RowError as e:
            errors.append({'index': index, 'payment_no': item.get('payment_no'), 'code': e.code, 'message': str(e)})
            continue
        no = row['payment_no']
        if no in seen:
            if seen[no] == row['fingerprint']:
                continue  # 同批完全重复的流水只记一次
            errors.append({'index': index, 'payment_no': no, 'code': 'duplicate_conflict', 'message': f'流水编号 {no} 在批内重复且内容不同'})
            continue
        seen[no] = row['fingerprint']
        existing = s.query(Payment).filter_by(payment_no=no).first()
        if existing and existing.fingerprint != row['fingerprint']:
            errors.append({'index': index, 'payment_no': no, 'code': 'duplicate_conflict', 'message': f'流水编号 {no} 已存在且内容不同，拒绝覆盖'})
            continue
        if existing:
            row['already_applied'] = True
        normalized.append(row)
    if errors:
        raise BatchFailure(errors, conflict=any(e['code'] == 'duplicate_conflict' for e in errors))
    if not normalized and not seen:
        raise BatchFailure([{'index': None, 'payment_no': None, 'code': 'empty', 'message': '没有可处理的流水'}])
    # 全部为同编号同内容时 normalized 为空：仍按成功处理，跳过计数即幂等证据
    return normalized


def apply_batch(s, batch, normalized):
    claims = {c.code: c for c in s.query(Claim).all()}
    for row in normalized:
        for code, _ in row['allocations']:
            if code not in claims:
                raise BatchFailure([{'index': None, 'payment_no': row['payment_no'], 'code': 'unknown_claim', 'message': f'流水 {row["payment_no"]} 分摊的票据 {code} 不存在'}])
    applied = skipped = held_created = 0
    touched = set()
    for row in normalized:
        if row.get('already_applied'):
            skipped += 1
            continue
        status = 'posted'
        if row['kind'] == 'reversal':
            original = s.query(Payment).filter_by(payment_no=row['original_no']).first()
            if not original:
                status = 'held'
                held_created += 1
        payment = Payment(
            batch_id=batch.id,
            payment_no=row['payment_no'],
            kind=row['kind'],
            original_no=row['original_no'],
            amount_cents=row['amount_cents'],
            status=status,
            fingerprint=row['fingerprint'],
            created=now(),
        )
        s.add(payment)
        s.flush()
        for code, share in row['allocations']:
            s.add(PaymentAllocation(payment_id=payment.id, claim_id=claims[code].id, amount_cents=share))
            touched.add(code)
        applied += 1
    s.flush()

    # 后到的原支付会让先前挂起的冲正自动入账。
    resolved = 0
    for held in s.query(Payment).filter_by(kind='reversal', status='held').all():
        original = s.query(Payment).filter_by(payment_no=held.original_no, kind='payment').first()
        if not original:
            continue
        held.status = 'posted'
        resolved += 1
        touched.update(a.claim.code for a in held.allocations)
        touched.update(a.claim.code for a in original.allocations)
    s.flush()
    return {
        'applied': applied,
        'skipped': skipped,
        'held_created': held_created,
        'held_resolved': resolved,
        'affected_claims': sorted(touched),
    }


def ledger_payments(s):
    result = []
    for p in s.query(Payment).order_by(Payment.id).all():
        result.append({
            'payment_no': p.payment_no,
            'kind': p.kind,
            'original_no': p.original_no,
            'amount_cents': p.amount_cents,
            'status': p.status,
            'created': p.created,
            'batch_id': p.batch_id,
            'allocations': [{'claim_code': a.claim.code, 'amount_cents': a.amount_cents} for a in p.allocations],
        })
    return result


def reconcile(claims, payments):
    """纯函数：按票据汇总已入账净额、挂起金额、差额与流水依据。"""
    by_code = {}
    for c in claims:
        due = claim_cents(c['amount'])
        by_code[c['code']] = {
            'id': c['id'],
            'code': c['code'],
            'label': c.get('label'),
            'amount_cents': due,
            'paid_cents': 0,
            'held_cents': 0,
            'difference_cents': -due,
            'status': 'unsettled',
            'reason': '',
            'eligible_normal': False,
            'basis': [],
        }
    for p in payments:
        for a in p['allocations']:
            entry = by_code.get(a['claim_code'])
            if not entry:
                continue
            amount = a['amount_cents']  # 冲正分摊本身为负，已含方向
            basis = {
                'payment_no': p['payment_no'],
                'kind': p['kind'],
                'original_no': p['original_no'],
                'status': p['status'],
                'amount_cents': p['amount_cents'],
                'allocated_cents': a['amount_cents'],
                'batch_id': p['batch_id'],
                'created': p['created'],
            }
            entry['basis'].append(basis)
            if p['status'] == 'held':
                entry['held_cents'] += a['amount_cents']
            else:
                entry['paid_cents'] += amount
    for entry in by_code.values():
        due = entry['amount_cents']
        entry['difference_cents'] = entry['paid_cents'] - due
        if any(b['status'] == 'held' for b in entry['basis']):
            entry['status'] = 'held'
            entry['reason'] = f'冲正挂起：引用的原支付尚未到达（挂起分摊 {yuan(entry["held_cents"])} 元，暂不计入到账净额）'
        elif entry['paid_cents'] > due:
            entry['status'] = 'overpaid'
            entry['reason'] = f'超付：多出 {yuan(entry["paid_cents"] - due)} 元'
        elif entry['paid_cents'] < due:
            entry['status'] = 'unsettled'
            entry['reason'] = f'未结清：尚差 {yuan(due - entry["paid_cents"])} 元'
        else:
            entry['status'] = 'settled'
            entry['reason'] = '已结清'
        entry['eligible_normal'] = entry['status'] == 'settled'
        entry['basis'].sort(key=lambda b: (b['created'] or '', b['payment_no']))
    return by_code


def latest_snapshot(s):
    return s.query(ReconSnapshot).order_by(ReconSnapshot.id.desc()).first()


def execute_batch(s, batch):
    """校验、入账并生成只追加的依据版本。整批成功或整批回滚。

    返回同一批次对象（status=completed）。校验失败时把原始行留在批次上、
    状态置 failed 后抛出 BatchFailure，调用方按原批恢复即可。
    """
    batch_id = batch.id
    try:
        with recon_lock(s):
            s.commit()
            s.expire_all()
            batch = s.get(PaymentBatch, batch_id)
            if batch.status == 'completed':
                return batch  # 重试幂等：已完成批次不再计款
            # 校验也在锁内完成，避免并发批次各自看到旧账本而重复入账
            normalized = normalize_batch(s, batch.payload)
            summary = apply_batch(s, batch, normalized)
            snap = publish_snapshot(s, batch_id, f'导入批次 #{batch_id}')
            batch.status = 'completed'
            batch.snapshot_id = snap.id
            batch.completed = now()
            batch.errors = []
            batch.summary = {**summary, 'recon_snapshot_id': snap.id,
                             'net_cents': snap.payload['totals']['net_cents']}
            s.commit()
            return batch
    except BatchFailure as e:
        s.rollback()
        batch = s.get(PaymentBatch, batch_id)
        batch.status = 'failed'
        batch.errors = e.errors
        batch.completed = now()
        s.commit()
        raise
    except Exception:
        s.rollback()
        batch = s.get(PaymentBatch, batch_id)
        batch.status = 'failed'
        batch.errors = [{'index': None, 'payment_no': None, 'code': 'error',
                         'message': '批处理异常，已保留原始行，可按原批恢复'}]
        batch.completed = now()
        s.commit()
        raise


def recover_incomplete_batches(s):
    """进程重启时恢复停在 received 的导入批次，复用原批原始行。

    失败批次保留原行由用户显式“按原批恢复”；入账幂等（同编号同内容跳过），
    因此恢复不会重复计款。
    """
    recovered = []
    for batch in s.query(PaymentBatch).filter_by(status='received').order_by(PaymentBatch.id).all():
        try:
            execute_batch(s, batch)
            recovered.append({'id': batch.id, 'status': batch.status})
        except BatchFailure as e:
            recovered.append({'id': batch.id, 'status': 'failed', 'errors': e.errors})
        except Exception:
            recovered.append({'id': batch.id, 'status': 'failed'})
    return recovered


def snapshot_checksum(snapshot):
    payload = snapshot.payload if hasattr(snapshot, 'payload') else snapshot
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def publish_snapshot(s, batch_id, reason):
    payments = ledger_payments(s)
    posted = [p for p in payments if p['status'] == 'posted']
    net = sum((1 if p['kind'] == 'payment' else -1) * p['amount_cents'] for p in posted)
    allocated = sum(a['amount_cents'] for p in posted for a in p['allocations'])
    if net != allocated:
        raise BatchFailure([{
            'index': None,
            'payment_no': None,
            'code': 'conservation',
            'message': f'到账净额 {yuan(net)} 元与逐票分摊合计 {yuan(allocated)} 元不守恒，拒绝生效',
        }])
    claims = [{'id': c.id, 'code': c.code, 'label': c.label, 'amount': c.amount} for c in s.query(Claim).order_by(Claim.id).all()]
    snapshot_claims = reconcile(claims, payments)
    counts = Counter(v['status'] for v in snapshot_claims.values())
    payload = {
        'payments': payments,
        'totals': {
            'net_cents': net,
            'allocated_cents': allocated,
            'payments': sum(1 for p in payments if p['kind'] == 'payment'),
            'reversals': sum(1 for p in payments if p['kind'] == 'reversal'),
            'held_payments': sum(1 for p in payments if p['status'] == 'held'),
            'settled': counts['settled'],
            'unsettled': counts['unsettled'],
            'overpaid': counts['overpaid'],
            'held_claims': counts['held'],
        },
        'claims': snapshot_claims,
    }
    snap = ReconSnapshot(reason=reason, batch_id=batch_id, payload={}, created=now())
    s.add(snap)
    s.flush()
    payload['version'] = snap.id
    payload['created'] = snap.created
    snap.payload = payload
    s.flush()
    return snap


def reconciliation_view(s, claim_code=None, status=None):
    snap = latest_snapshot(s)
    if not snap:
        return {
            'version': None,
            'created': None,
            'batch_id': None,
            'claims': [],
            'totals': {},
            'note': '尚未导入任何支付流水；所有正常标签暂不入训。',
        }
    claims = [{'id': c.id, 'code': c.code, 'label': c.label, 'amount': c.amount} for c in s.query(Claim).order_by(Claim.id).all()]
    rows = list(reconcile(claims, snap.payload['payments']).values())
    if claim_code:
        rows = [r for r in rows if r['code'] == claim_code]
    if status:
        rows = [r for r in rows if r['status'] == status]
    return {
        'version': snap.id,
        'created': snap.created,
        'batch_id': snap.batch_id,
        'reason': snap.reason,
        'checksum': snapshot_checksum(snap),
        'totals': snap.payload.get('totals', {}),
        'claims': rows,
        'note': '差额 = 已入账净额 − 报销金额，均按分计算。流水依据含挂起冲正。',
    }
