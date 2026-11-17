import json
import os
import sys
import tempfile
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp(prefix='claim-tests-')
os.environ['DATABASE_URL'] = f'sqlite:///{TMP}/test.db'
os.environ['JWT_SECRET'] = 'test-secret'
os.environ['REDIS_URL'] = 'redis://127.0.0.1:6399/0'
os.environ['SEED_DEMO'] = 'false'

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from src.config.database import Base  # noqa: E402
from src.models.entities import Claim, PaymentBatch, ReconSnapshot  # noqa: E402
import src.routes.api as api_module  # noqa: E402
from src.services.reconciliation import (  # noqa: E402
    BatchFailure,
    apply_batch,
    execute_batch,
    ledger_payments,
    normalize_batch,
    publish_snapshot,
    reconcile,
)


@pytest.fixture(scope='session', autouse=True)
def schema():
    from src.config.database import Base, engine
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as s:
        from src.models.entities import User
        from src.utils.security import hash_password
        for role in ('admin', 'analyst', 'viewer'):
            s.add(User(email=f'{role}@demo.local', role=role, password=hash_password('Demo123!')))
        s.commit()
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture(autouse=True)
def clean_business_tables():
    from src.config.database import Session
    from src.models.entities import Payment, PaymentAllocation, PaymentBatch, ReconSnapshot
    from src.models.entities import Claim, Training, Review
    with Session() as s:
        s.query(Review).delete()
        s.query(Training).delete()
        s.query(PaymentAllocation).delete()
        s.query(Payment).delete()
        s.query(ReconSnapshot).delete()
        s.query(PaymentBatch).delete()
        s.query(Claim).delete()
        s.commit()
    yield


class FakeRedis:
    def __init__(self):
        self.items = []

    def rpush(self, key, value):
        self.items.append(value)

    def blpop(self, key, timeout=0):
        return None

    def ping(self):
        return True


@pytest.fixture()
def client(monkeypatch):
    import importlib
    import redis as redis_module
    import src.main as main_module
    importlib.reload(main_module)
    fake = FakeRedis()
    monkeypatch.setattr(redis_module.Redis, 'from_url', classmethod(lambda cls, *a, **k: fake))
    with TestClient(main_module.app) as c:
        yield c


def login(client, email='analyst@demo.local'):
    r = client.post('/auth/login', json={'email': email, 'password': 'Demo123!'})
    assert r.status_code == 200, r.text
    return {'Authorization': 'Bearer ' + r.json()['token']}


def make_claim(client, headers, code, amount, label=None):
    r = client.post('/claims', headers=headers, json={
        'code': code, 'amount': amount, 'distance': 10, 'holiday': False, 'label': label, 'note': '',
    })
    assert r.status_code == 200, r.text
    return r.json()


class BatchResult:
    """导入请求返回接收状态，后台完成后测试读取最终批次视图。"""

    def __init__(self, view, post_status):
        self.view = view
        self.post_status = post_status

    @property
    def status_code(self):
        if self.post_status in (403, 503):
            return self.post_status
        if self.view['status'] == 'completed':
            return 200
        codes = {e.get('code') for e in self.view.get('errors') or []}
        return 409 if 'duplicate_conflict' in codes else 422

    def json(self):
        return self.view


def import_batch(client, headers, payments):
    r = client.post('/payments/batches', headers=headers, json={'source': 'test', 'payments': payments})
    assert r.status_code in (202, 403, 503), r.text
    if r.status_code == 403:
        return BatchResult({'status': 'forbidden', 'errors': []}, 403)
    if r.status_code == 503:
        return BatchResult({'status': 'failed', 'errors': []}, 503)
    batch_id = r.json()['id']
    from src.services.jobs import run_payment_batch
    run_payment_batch(batch_id)  # 模拟 worker 消费
    view = client.get(f'/payments/batches/{batch_id}', headers=headers).json()
    return BatchResult(view, r.status_code)


def recover_batch(client, headers, batch_id):
    r = client.post(f'/payments/batches/{batch_id}/retry', headers=headers)
    assert r.status_code == 202, r.text
    from src.services.jobs import run_payment_batch
    run_payment_batch(batch_id)
    return client.get(f'/payments/batches/{batch_id}', headers=headers).json()


def pay(no, amount, allocations, kind='payment', original_no=None):
    body = {'payment_no': no, 'kind': kind, 'amount': amount,
            'allocations': [{'claim_code': c, 'amount': a} for c, a in allocations]}
    if original_no:
        body['original_no'] = original_no
    return body


def db_session(tmp_path=None):
    from src.config.database import Session
    return Session()


def seed_small(claims):
    """直接建库并写入票据，返回票据 id 到 code 的映射。"""
    from src.models.entities import Payment, PaymentAllocation, ReconSnapshot
    with db_session() as s:
        s.query(PaymentAllocation).delete()
        s.query(Payment).delete()
        s.query(ReconSnapshot).delete()
        s.query(PaymentBatch).delete()
        s.query(Claim).delete()
        s.commit()
        created = {}
        for code, amount, label in claims:
            c = Claim(code=code, amount=amount, distance=10, holiday=False, label=label, note='')
            s.add(c)
            s.flush()
            created[code] = c.id
        s.commit()
        return created


def batch(s, rows):
    """走生产路径：接收 -> 校验入账 -> 发布只追加依据版本。"""
    b = PaymentBatch(status='received', source='unit', payload=rows, created='t')
    s.add(b)
    s.commit()
    execute_batch(s, b)
    s.refresh(b)
    snap = s.get(ReconSnapshot, b.snapshot_id)
    return b, snap, b.summary


def test_reversal_before_payment_is_held_then_resolved():
    """先到冲正：挂起、不计入到账净额；原支付到达后自动入账并重算。"""
    seed_small([('C-1', 100.0, 0), ('C-2', 50.0, 0)])
    with db_session() as s:
        b1, snap1, summary1 = batch(s, [
            pay('R-1', 40.0, [('C-1', -40.0)], kind='reversal', original_no='P-1'),
        ])
        assert summary1['held_created'] == 1
        assert snap1.payload['totals']['net_cents'] == 0
        assert snap1.payload['totals']['held_payments'] == 1
        recon = snap1.payload['claims']
        assert recon['C-1']['status'] == 'held'
        assert recon['C-1']['paid_cents'] == 0
        assert recon['C-1']['held_cents'] == -4000

        b2, snap2, summary2 = batch(s, [
            pay('P-1', 100.0, [('C-1', 100.0)]),
        ])
        assert summary2['held_resolved'] == 1
        assert snap2.payload['totals']['net_cents'] == 6000  # 支付 100 − 冲正 40
        assert snap2.payload['totals']['allocated_cents'] == 6000
        recon2 = snap2.payload['claims']
        assert recon2['C-1']['status'] == 'unsettled'
        assert recon2['C-1']['paid_cents'] == 6000  # 100 − 40 冲正
        assert recon2['C-1']['held_cents'] == 0
        assert recon2['C-2']['status'] == 'unsettled'

        # 冲正在后续依据版本中保持已入账，不会重复扣减
        b3, snap3, _ = batch(s, [pay('P-2', 50.0, [('C-2', 50.0)])])
        assert snap3.payload['claims']['C-1']['paid_cents'] == 6000
        assert snap3.payload['totals']['net_cents'] == 11000


def test_cross_claim_allocation_sums_to_payment_in_cents():
    seed_small([('C-1', 30.0, 0), ('C-2', 70.0, 0)])
    with db_session() as s:
        _, snap, _ = batch(s, [pay('P-1', 100.0, [('C-1', 30.0), ('C-2', 70.0)])])
        assert snap.payload['totals']['net_cents'] == 10000
        assert snap.payload['totals']['allocated_cents'] == 10000
        assert snap.payload['claims']['C-1']['status'] == 'settled'
        assert snap.payload['claims']['C-2']['status'] == 'settled'
        assert len(snap.payload['payments'][0]['allocations']) == 2


def test_same_batch_duplicate_identical_collapses_and_conflict_rejected():
    from src.models.entities import Payment
    seed_small([('C-1', 100.0, 0)])
    with db_session() as s:
        b = PaymentBatch(status='received', payload=[
            pay('P-1', 100.0, [('C-1', 100.0)]),
            pay('P-1', 100.0, [('C-1', 100.0)]),
        ], created='t')
        s.add(b)
        s.commit()
        execute_batch(s, b)
        s.refresh(b)
        assert b.status == 'completed' and b.summary['applied'] == 1
        assert len(s.query(Payment).all()) == 1

        bad = PaymentBatch(status='received', payload=[
            pay('P-2', 100.0, [('C-1', 100.0)]),
            pay('P-2', 100.0, [('C-1', 100.0)]),
            pay('P-2', 90.0, [('C-1', 90.0)]),
        ], created='t')
        s.add(bad)
        s.commit()
        with pytest.raises(BatchFailure) as err:
            execute_batch(s, bad)
        assert any(e['code'] == 'duplicate_conflict' for e in err.value.errors)
        s.refresh(bad)
        assert bad.status == 'failed' and (bad.payload)  # 原始行保留


def test_allocation_must_preserve_cents():
    seed_small([('C-1', 30.0, 0)])
    with db_session() as s:
        b = PaymentBatch(status='received', payload=[pay('P-1', 100.0, [('C-1', 99.99)])], created='t')
        s.add(b)
        s.commit()
        with pytest.raises(BatchFailure):
            normalize_batch(s, b.payload)
        b = PaymentBatch(status='received', payload=[pay('P-1', 0.005, [('C-1', 0.005)])], created='t')
        s.add(b)
        s.commit()
        with pytest.raises(BatchFailure):
            normalize_batch(s, b.payload)


def test_duplicate_number_same_content_idempotent_different_content_rejected(client):
    h = login(client)
    make_claim(client, h, 'D-1', 100)
    first = import_batch(client, h, [pay('P-1', 100.0, [('D-1', 100.0)])])
    assert first.status_code == 200 and first.json()['status'] == 'completed'

    again = import_batch(client, h, [pay('P-1', 100.0, [('D-1', 100.0)])])
    assert again.status_code == 200
    assert again.json()['summary']['skipped'] == 1
    assert client.get('/payments', headers=h).json()[0]['payment_no'] == 'P-1'

    conflict = import_batch(client, h, [pay('P-1', 90.0, [('D-1', 90.0)])])
    assert conflict.status_code == 409
    assert conflict.json()['errors'][0]['code'] == 'duplicate_conflict'

    view = client.get('/reconciliation/claims/D-1', headers=h).json()
    assert view['paid_cents'] == 10000  # 重复导入没有重复计款
    assert view['status'] == 'settled'


def test_failed_batch_recovers_from_original_without_double_counting(client):
    h = login(client)
    # 首次导入因票据尚未录入而整批失败，原始行留在原批
    failed = import_batch(client, h, [pay('P-1', 100.0, [('R-1', 100.0)])])
    assert failed.status_code == 422
    batches = client.get('/payments/batches', headers=h).json()
    assert batches[0]['status'] == 'failed'
    assert batches[0]['errors'][0]['code'] == 'unknown_claim'
    failed_id = batches[0]['id']

    make_claim(client, h, 'R-1', 100)
    retry = recover_batch(client, h, failed_id)
    assert retry['id'] == failed_id  # 接着原批恢复，不新建批次
    assert retry['status'] == 'completed'
    assert retry['summary']['applied'] == 1

    again = client.post(f'/payments/batches/{failed_id}/retry', headers=h)
    assert again.status_code == 200
    assert again.json()['status'] == 'completed'
    assert again.json()['summary']['applied'] == 1  # 已完成批次返回原结果
    assert len(client.get('/payments', headers=h).json()) == 1
    view = client.get('/reconciliation/claims/R-1', headers=h).json()
    assert view['paid_cents'] == 10000


def test_normal_unsettled_overpaid_and_held_excluded_anomaly_kept(client):
    h = login(client)
    make_claim(client, h, 'OK-1', 100, 0)
    make_claim(client, h, 'SHORT-1', 100, 0)
    make_claim(client, h, 'OVER-1', 100, 0)
    make_claim(client, h, 'HELD-1', 100, 0)
    make_claim(client, h, 'BAD-1', 100, 1)
    r = import_batch(client, h, [
        pay('P-OK', 100.0, [('OK-1', 100.0)]),
        pay('P-SHORT', 60.0, [('SHORT-1', 60.0)]),
        pay('P-OVER', 120.0, [('OVER-1', 120.0)]),
        pay('R-HELD', 30.0, [('HELD-1', -30.0)], kind='reversal', original_no='P-MISSING'),
    ])
    assert r.status_code == 200, r.text
    view = {c['code']: c for c in client.get('/reconciliation', headers=h).json()['claims']}
    assert view['OK-1']['status'] == 'settled'
    assert view['SHORT-1']['status'] == 'unsettled' and view['SHORT-1']['difference_cents'] == -4000
    assert view['OVER-1']['status'] == 'overpaid' and view['OVER-1']['difference_cents'] == 2000
    assert view['HELD-1']['status'] == 'held'
    assert view['BAD-1']['status'] == 'unsettled'

    # 已结清的正常样本与异常样本入训；未结清、超付、挂起的正常样本排除
    train = client.post('/trainings', headers=h, json={'name': '对账资格', 'threshold': 0.5})
    assert train.status_code == 200, train.text
    frozen = train.json()['result']
    assert [r['code'] for r in frozen['snapshot']] == ['OK-1', 'BAD-1']
    excluded = {e['code']: e for e in frozen['excluded']}
    assert set(excluded) == {'SHORT-1', 'OVER-1', 'HELD-1'}
    assert excluded['SHORT-1']['status'] == 'unsettled'
    assert excluded['OVER-1']['status'] == 'overpaid'
    assert excluded['HELD-1']['status'] == 'held'
    reasons = {x['status'] for x in frozen['recon']['exclusion_summary']}
    assert reasons == {'unsettled', 'overpaid', 'held'}

    # 入训样本不足 40 时后台训练必须失败并给出原因
    from src.services.analysis import train as run_train
    with pytest.raises(ValueError, match='未结清、超付或冲正挂起'):
        run_train(frozen['snapshot'], 0.5, frozen['excluded'], frozen['recon']['exclusion_summary'])


def test_training_freezes_single_basis_and_ignores_later_imports(client):
    h = login(client)
    # 60 正常 + 60 异常，全部先结清，保证可训练
    for i in range(60):
        make_claim(client, h, f'N-{i}', 100, 0)
        make_claim(client, h, f'A-{i}', 100, 1)
    n_rows = [pay(f'PN-{i}', 100.0, [(f'N-{i}', 100.0)]) for i in range(60)]
    a_rows = [pay(f'PA-{i}', 100.0, [(f'A-{i}', 100.0)]) for i in range(60)]
    base = import_batch(client, h, n_rows + a_rows)
    assert base.status_code == 200, base.text
    snapshot_id = base.json()['recon_snapshot_id']

    train = client.post('/trainings', headers=h, json={'name': '竞争前的训练', 'threshold': 0.5})
    assert train.status_code == 200, train.text
    body = train.json()
    assert body['recon_snapshot_id'] == snapshot_id
    frozen = body['result']['snapshot']
    assert len(frozen) == 120
    assert body['result']['recon']['recon_snapshot_id'] == snapshot_id

    # 训练提交后再导入冲正，使若干正常票据变为挂起/未结清
    later = import_batch(client, h, [
        pay('R-N-0', 100.0, [('N-0', -100.0)], kind='reversal', original_no='PN-0'),
    ])
    assert later.status_code == 200
    assert later.json()['recon_snapshot_id'] != snapshot_id

    # 已提交训练的依据不变，仍固定旧版本
    detail = client.get(f"/trainings/{body['id']}", headers=h).json()
    assert detail['result']['snapshot'] == frozen
    assert detail['recon_snapshot_id'] == snapshot_id
    assert detail['result']['recon']['recon_snapshot_id'] == snapshot_id

    # 新训练采用新依据，且排除受影响的正常样本
    newer = client.post('/trainings', headers=h, json={'name': '竞争后的训练', 'threshold': 0.5})
    assert newer.status_code == 200
    assert newer.json()['recon_snapshot_id'] == later.json()['recon_snapshot_id']
    excluded = {e['code']: e for e in newer.json()['result']['excluded']}
    assert excluded['N-0']['status'] == 'unsettled'


def test_concurrent_imports_publish_consistent_versions():
    """两个线程并发导入：各自的依据版本内部守恒，且训练只读取一版。"""
    seed_small([(f'C-{i}', 10.0, 0) for i in range(20)])
    barrier = threading.Barrier(2)
    results, errors = [], []

    def worker(prefix):
        try:
            barrier.wait()
            with db_session() as s:
                b, snap, _ = batch(s, [
                    pay(f'{prefix}-{i}', 10.0, [(f'C-{i}', 10.0)]) for i in range(0, 20, 2 if prefix == 'L' else 1)
                ])
                results.append((b.id, snap.id, snap.payload['totals']))
        except Exception as e:  # pragma: no cover - surfaced below
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(p,)) for p in ('L', 'R')]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    for _, _, totals in results:
        assert totals['net_cents'] == totals['allocated_cents']
    with db_session() as s:
        latest = s.query(ReconSnapshot).order_by(ReconSnapshot.id.desc()).first()
        # 最新依据必须完整包含两批互不相同的流水
        numbers = {p['payment_no'] for p in latest.payload['payments']}
        assert len(numbers) == 30
        assert {'L-0', 'R-1'} <= numbers
        net = latest.payload['totals']['net_cents']
        assert net == latest.payload['totals']['allocated_cents'] == 30000


def test_seed_data_can_train_end_to_end_with_exclusions():
    """种子数据：冲正先到的票据挂起，正常样本排除后仍有足够样本训练。"""
    from scripts.seed_demo import seed
    seed()
    with db_session() as s:
        from src.services.business import trainable_rows
        rows, excluded, meta = trainable_rows(s)
        assert meta['recon_snapshot_id']
        codes = {r['code'] for r in rows}
        blocked = {e['code'] for e in excluded}
        assert codes and not (codes & blocked)
        assert any(e['status'] == 'held' for e in excluded)
        from src.services.analysis import train as run_train
        result = run_train(rows, 0.5, excluded, meta['exclusion_summary'])
        assert result['train_n'] + result['test_n'] == sum(1 for r in rows if r['label'] is not None)
        assert result['train_n'] >= 30
        assert result['excluded'] and result['exclusion_summary']
        assert [c for c in result['exclusion_summary'] if c['status'] == 'held']


def test_claim_edit_publishes_new_basis_but_old_training_frozen(client):
    h = login(client)
    make_claim(client, h, 'E-1', 100, 0)
    import_batch(client, h, [pay('P-E1', 100.0, [('E-1', 100.0)])])
    first = client.post('/trainings', headers=h, json={'name': '编辑前', 'threshold': 0.5}).json()
    detail = next(c for c in client.get('/claims', headers=h).json() if c['code'] == 'E-1')
    client.put(f"/claims/{detail['id']}", headers=h, json={
        'code': 'E-1', 'amount': 150.0, 'distance': 10, 'holiday': False, 'label': 0, 'note': '',
    })
    view = client.get('/reconciliation/claims/E-1', headers=h).json()
    assert view['amount_cents'] == 15000 and view['difference_cents'] == -5000
    assert view['status'] == 'unsettled'
    assert client.get(f"/trainings/{first['id']}", headers=h).json()['result']['snapshot'] == first['result']['snapshot']


def test_viewer_cannot_import_or_retry(client):
    h = login(client, 'viewer@demo.local')
    r = import_batch(client, h, [pay('V-1', 10.0, [])])
    assert r.status_code == 403
    assert client.post('/payments/batches/1/retry', headers=h).status_code == 403


def test_restart_recovers_incomplete_batches_from_original_rows(client):
    """进程中断后，received 批次按原批恢复；已入账流水不重复计款。"""
    h = login(client)
    make_claim(client, h, 'S-1', 100)
    with db_session() as s:
        from src.models.entities import PaymentBatch
        from src.services.reconciliation import recover_incomplete_batches
        batch = PaymentBatch(status='received', source='中断前', payload=[
            pay('P-S1', 60.0, [('S-1', 60.0)]),
        ], created='t')
        s.add(batch)
        s.commit()
        failed = PaymentBatch(status='failed', source='票据缺失时失败', payload=[
            pay('P-S2', 40.0, [('S-MISSING', 40.0)]),
        ], created='t', errors=[{'message': '票据不存在'}])
        s.add(failed)
        s.commit()
        result = recover_incomplete_batches(s)
        # 只有 received（中断）批次自动恢复；失败批次等人补齐票据后手动按原批恢复
        assert [r['id'] for r in result] == [batch.id]
        assert result[0]['status'] == 'completed'
        assert s.get(PaymentBatch, failed.id).status == 'failed'
        # 再恢复一次不会重复计款
        recover_incomplete_batches(s)
    view = client.get('/reconciliation/claims/S-1', headers=h).json()
    assert view['paid_cents'] == 6000
    assert view['status'] == 'unsettled'
    assert len(client.get('/payments', headers=h).json()) == 1


def test_concurrent_training_and_import_never_mix_basis(client):
    """导入与训练竞争：每个训练只取一版依据，排除清单与所引版本完全一致。"""
    h = login(client)
    for i in range(40):
        make_claim(client, h, f'R-N-{i}', 100, 0)
        make_claim(client, h, f'R-A-{i}', 100, 1)
    import_batch(client, h, [pay(f'RP-{i}', 100.0, [(f'R-N-{i}', 100.0)]) for i in range(40)]
                 + [pay(f'RA-{i}', 100.0, [(f'R-A-{i}', 100.0)]) for i in range(40)])

    barrier = threading.Barrier(3)
    outcomes, failures = [], []

    def importer():
        barrier.wait()
        try:
            r = import_batch(client, h, [
                pay('RCUT-0', 50.0, [('R-N-0', -50.0)], kind='reversal', original_no=f'RP-0'),
            ])
            outcomes.append(('import', r.status_code))
        except Exception as e:  # pragma: no cover
            failures.append(e)

    def trainer(name):
        barrier.wait()
        try:
            r = client.post('/trainings', headers=h, json={'name': name, 'threshold': 0.5})
            outcomes.append(('train', r.status_code, r.json()))
        except Exception as e:  # pragma: no cover
            failures.append(e)

    threads = [threading.Thread(target=importer), threading.Thread(target=trainer, args=('竞争A',)),
               threading.Thread(target=trainer, args=('竞争B',))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not failures
    trainings = [o[2] for o in outcomes if o[0] == 'train' and o[1] == 200]
    assert trainings
    for body in trainings:
        frozen = body['result']
        snap_id = body['recon_snapshot_id']
        assert frozen['recon']['recon_snapshot_id'] == snap_id
        with db_session() as s:
            from src.models.entities import ReconSnapshot
            snap = s.get(ReconSnapshot, snap_id)
            status_by_code = {c: v['status'] for c, v in snap.payload['claims'].items()}
        expected = {e['code'] for e in frozen['excluded']}
        # 排除清单必须完全由该版依据推出：所有正常且未结清的样本，且不含已结清样本
        blocked = {code for code, st in status_by_code.items()
                   if st != 'settled' and code.startswith('R-N-')}
        assert blocked <= expected
        assert not any(status_by_code[e['code']] == 'settled' for e in frozen['excluded'])
        assert all(e['code'] in status_by_code for e in frozen['excluded'])
