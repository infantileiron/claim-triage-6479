from sqlalchemy import Column,Integer,String,Float,JSON,Text,ForeignKey,Boolean,UniqueConstraint
from sqlalchemy.orm import relationship
from src.config.database import Base
class User(Base):
 __tablename__='users'
 id=Column(Integer,primary_key=True);email=Column(String(100),unique=True);password=Column(String(200));role=Column(String(20))
class Claim(Base):
 __tablename__='claims'
 id=Column(Integer,primary_key=True);code=Column(String(60),unique=True,nullable=False);amount=Column(Float);distance=Column(Float);holiday=Column(Boolean);label=Column(Integer,nullable=True);note=Column(Text,default='')
class Training(Base):
 __tablename__='trainings'
 id=Column(Integer,primary_key=True);name=Column(String(100));threshold=Column(Float);state=Column(String(20),default='queued');result=Column(JSON);error=Column(Text,default='')
 recon_snapshot_id=Column(Integer,nullable=True)
class Review(Base):
 __tablename__='reviews'
 __table_args__=(UniqueConstraint('training_id','claim_id',name='uq_review_source'),)
 id=Column(Integer,primary_key=True);training_id=Column(Integer,ForeignKey('trainings.id'));claim_id=Column(Integer,ForeignKey('claims.id'));score=Column(Float);decision=Column(String(30),default='pending');note=Column(Text,default='');status=Column(String(20),default='draft')
class Audit(Base):
 __tablename__='audits'
 id=Column(Integer,primary_key=True);actor=Column(String(100));action=Column(String(200));created=Column(String(40))

# 支付流水对账。金额统一以整数分存储，避免浮点误差累积。
class PaymentBatch(Base):
 __tablename__='payment_batches'
 id=Column(Integer,primary_key=True)
 status=Column(String(20),default='received')       # received / completed / failed
 source=Column(String(200),default='')
 payload=Column(JSON,default=list)                  # 原始导入行，失败后按原批恢复
 errors=Column(JSON,default=list)
 summary=Column(JSON,default=dict)
 snapshot_id=Column(Integer,nullable=True)          # 本批完成时采用的/生成的依据版本
 created=Column(String(40),default='')
 completed=Column(String(40),default='')

class Payment(Base):
 __tablename__='payments'
 id=Column(Integer,primary_key=True)
 batch_id=Column(Integer,ForeignKey('payment_batches.id'),nullable=True)
 payment_no=Column(String(80),unique=True,nullable=False)
 kind=Column(String(20),default='payment')          # payment / reversal
 original_no=Column(String(80),nullable=True)       # 冲正引用的原支付编号
 amount_cents=Column(Integer,nullable=False)
 status=Column(String(20),default='posted')         # posted / held（冲正先到且原支付未到）
 fingerprint=Column(String(64),nullable=False)
 created=Column(String(40),default='')
 allocations=relationship('PaymentAllocation',back_populates='payment',cascade='all, delete-orphan')

class PaymentAllocation(Base):
 __tablename__='payment_allocations'
 __table_args__=(UniqueConstraint('payment_id','claim_id',name='uq_payment_claim'),)
 id=Column(Integer,primary_key=True)
 payment_id=Column(Integer,ForeignKey('payments.id'),nullable=False)
 claim_id=Column(Integer,ForeignKey('claims.id'),nullable=False)
 amount_cents=Column(Integer,nullable=False)
 payment=relationship('Payment',back_populates='allocations')
 claim=relationship('Claim')

class ReconSnapshot(Base):
 __tablename__='recon_snapshots'
 id=Column(Integer,primary_key=True)
 reason=Column(String(200),default='')
 batch_id=Column(Integer,ForeignKey('payment_batches.id'),nullable=True)
 payload=Column(JSON,nullable=False)                # 整批对账结果与训练资格，只追加不修改
 created=Column(String(40),default='')
