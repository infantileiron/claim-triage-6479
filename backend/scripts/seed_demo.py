import random, math
from decimal import Decimal, ROUND_HALF_UP

from src.config.database import Base, engine, Session
from src.models.entities import User, Claim, Payment, PaymentBatch
from src.services.reconciliation import execute_batch, latest_snapshot, recon_lock


def _yuan(value):
    return float(Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def seed_payments(s):
    """确定性模拟流水，分两批导入：

    批一先到冲正（原支付尚未到达，挂起），批二到达原支付后自动入账；
    另有少量未结清与超付，供页面查看差额和流水依据。
    """
    if latest_snapshot(s) or s.query(Payment).count():
        return None
    claims = s.query(Claim).order_by(Claim.id).all()
    if not claims:
        return None
    with recon_lock(s):
        s.commit()
        s.expire_all()
        early = []
        late = []
        for i, c in enumerate(claims):
            no = f'HX-PAY-{i + 1:03}'
            amount = _yuan(c.amount)
            if i % 53 == 7:
                share = _yuan(amount * .2)
                early.append({'payment_no': f'HX-REV-{i + 1:03}', 'kind': 'reversal', 'original_no': no,
                              'amount': share, 'allocations': [{'claim_code': c.code, 'amount': -share}]})
                late.append({'payment_no': no, 'kind': 'payment', 'amount': amount,
                             'allocations': [{'claim_code': c.code, 'amount': amount}]})
            elif i % 89 == 17:
                # 冲正引用的原支付始终未到，保持挂起，供页面演示
                share = _yuan(amount * .1) or 0.01
                early.append({'payment_no': f'HX-REV-{i + 1:03}', 'kind': 'reversal',
                              'original_no': f'HX-PAY-MISSING-{i + 1:03}',
                              'amount': share, 'allocations': [{'claim_code': c.code, 'amount': -share}]})
                late.append({'payment_no': no, 'kind': 'payment', 'amount': amount,
                             'allocations': [{'claim_code': c.code, 'amount': amount}]})
            elif i % 37 == 5:
                share = _yuan(amount * .6)
                late.append({'payment_no': no, 'kind': 'payment', 'amount': share,
                             'allocations': [{'claim_code': c.code, 'amount': share}]})
            elif i % 37 == 11:
                over = _yuan(amount + 80)
                late.append({'payment_no': no, 'kind': 'payment', 'amount': over,
                             'allocations': [{'claim_code': c.code, 'amount': over}]})
            else:
                late.append({'payment_no': no, 'kind': 'payment', 'amount': amount,
                             'allocations': [{'claim_code': c.code, 'amount': amount}]})
        batch_ids = []
        for label, rows in (('seed 模拟流水（冲正先到）', early), ('seed 模拟流水（支付到账）', late)):
            if not rows:
                continue
            batch = PaymentBatch(status='received', source=label, payload=rows, created='seed')
            s.add(batch)
            s.commit()
            execute_batch(s, batch)
            batch_ids.append(batch.id)
        return batch_ids


def seed():
    from src.utils.security import hash_password
    Base.metadata.create_all(engine);rng=random.Random(3106)
    with Session() as s:
        for role in ('admin','analyst','viewer'):
            email=role+'@demo.local'
            if not s.query(User).filter_by(email=email).first():s.add(User(email=email,role=role,password=hash_password('Demo123!')))
        for i in range(300):
            code=f'HX-1115-{i+1:03}';distance=round(rng.uniform(5,800),1);holiday=rng.random()<.25;amount=round(distance*rng.uniform(1.2,5.5)+rng.uniform(50,900),2)
            risk=-4.7+amount/700+1.1*holiday+amount/(distance+1)/8;label=int(rng.random()<1/(1+math.exp(-risk))) if i<260 else None
            if not s.query(Claim).filter_by(code=code).first():s.add(Claim(code=code,amount=amount,distance=distance,holiday=holiday,label=label,note='历史人工复核标签；仅用于模拟训练，不连接真实报销'))
        s.commit()
        batch_ids=seed_payments(s)
        print({'users':s.query(User).count(),'claims':s.query(Claim).count(),'payments':s.query(Payment).count(),'recon_batches':batch_ids})
if __name__=='__main__':seed()
