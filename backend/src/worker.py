import logging
import os
import time
from pathlib import Path

import redis

from src.config.database import Session
from src.models.entities import PaymentBatch, Training
from src.services.jobs import run_payment_batch, run_training

r = redis.Redis.from_url(os.environ['REDIS_URL'])


def recover():
    """启动恢复：训练回到队列，中断的流水导入按原批继续。

    已完成批次不会重新入队，重复投递也幂等，因此恢复不会重复计款。
    """
    with Session() as s:
        for pending in s.query(Training).filter(Training.state.in_(['queued', 'running'])).all():
            pending.state = 'queued'
            r.rpush('claim:trainings', pending.id)
        for batch in s.query(PaymentBatch).filter_by(status='received').all():
            r.rpush('claim:payments', batch.id)
        s.commit()


recover()
while True:
    Path('/tmp/heartbeat').touch()
    try:
        # 流水优先：让对账依据尽快整批生效
        item = r.blpop('claim:payments', timeout=1)
        if item:
            run_payment_batch(int(item[1]))
            continue
        item = r.blpop('claim:trainings', timeout=3)
        if item:
            run_training(int(item[1]))
    except Exception:
        logging.exception('worker')
        time.sleep(2)
