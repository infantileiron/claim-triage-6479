"""后台任务：流水批次入账与训练执行。worker 队列消费与测试共用。"""
import json
import logging
from pathlib import Path

from src.config.database import Session
from src.models.entities import PaymentBatch, Training
from src.services.analysis import train
from src.services.reconciliation import execute_batch


def run_payment_batch(batch_id):
    """完成或失败一个已接收批次；重复投递幂等，不会重复计款。"""
    with Session() as s:
        batch = s.get(PaymentBatch, batch_id)
        if not batch or batch.status == 'completed':
            return None
        try:
            execute_batch(s, batch)
        except Exception as e:
            logging.warning('payment batch %s failed: %s', batch_id, e)
        return s.get(PaymentBatch, batch_id)


def run_training(training_id):
    with Session() as s:
        row = s.get(Training, training_id)
        if not row or row.state != 'queued':
            return None
        frozen = dict(row.result or {})
        snapshot = frozen['snapshot']
        row.state = 'running'
        s.commit()
        try:
            recon = frozen.get('recon', {})
            row.result = {
                **train(snapshot, row.threshold, frozen.get('excluded', []), recon.get('exclusion_summary', [])),
                'snapshot': snapshot,
                'excluded': frozen.get('excluded', []),
                'recon': recon,
            }
            row.state = 'done'
            try:
                path = Path('/artifacts/' + str(row.id) + '.json')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(row.result, ensure_ascii=False))
            except OSError:
                logging.warning('artifact write failed for training %s', row.id)
        except Exception as e:
            row.state = 'failed'
            row.error = str(e)
        s.commit()
        return s.get(Training, training_id)
