from typing import Literal, Optional
from pydantic import BaseModel, Field


class Login(BaseModel):
    email: str
    password: str


class ClaimInput(BaseModel):
    code: str = Field(min_length=1, max_length=60)
    amount: float = Field(ge=0, le=100000, allow_inf_nan=False)
    distance: float = Field(ge=0, le=10000, allow_inf_nan=False)
    holiday: bool = False
    label: Optional[Literal[0, 1]] = None
    note: str = Field(default='', max_length=1000)


class TrainingInput(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    threshold: float = Field(default=.5, ge=.1, le=.9, allow_inf_nan=False)


class ReviewInput(BaseModel):
    decision: Literal['valid', 'flagged']
    note: str = Field(min_length=6, max_length=2000)


class PaymentAllocationInput(BaseModel):
    claim_code: str = Field(min_length=1, max_length=60)
    amount: float = Field(allow_inf_nan=False)


class PaymentInput(BaseModel):
    payment_no: str = Field(min_length=1, max_length=80)
    kind: Literal['payment', 'reversal'] = 'payment'
    original_no: Optional[str] = Field(default=None, max_length=80)
    amount: float = Field(ge=0, allow_inf_nan=False)
    allocations: list[PaymentAllocationInput] = Field(default_factory=list)


class PaymentBatchInput(BaseModel):
    source: str = Field(default='手工 / 文件导入', max_length=200)
    payments: list[PaymentInput] = Field(min_length=1, max_length=5000)
