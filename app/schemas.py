"""Request/response shapes. Single source of truth: the frontend generates TS types from /openapi.json."""
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

Email = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_lower=True, max_length=255, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
]
Password = Annotated[str, StringConstraints(min_length=8, max_length=128)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
OpType = Literal["receive", "transfer", "delivery", "adjustment"]
OpStatus = Literal["draft", "waiting", "ready", "done", "canceled"]
Role = Literal["staff", "manager"]
Qty = Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=3)]
Money = Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=2)]
# No '/': short codes are joined with '/' into references like WH/IN/0001.
ShortCode = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20, pattern=r"^[A-Za-z0-9_.-]+$")]
Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]


# --- auth ---

class SignupIn(BaseModel):
    login_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=50, pattern=r"^[A-Za-z0-9_.-]+$")]
    email: Email
    password: Password


class LoginIn(BaseModel):
    login_id: Annotated[str, StringConstraints(strip_whitespace=True, max_length=50)]
    password: Annotated[str, StringConstraints(max_length=128)]


class ForgotPasswordIn(BaseModel):
    email: Email


class ResetPasswordIn(BaseModel):
    email: Email
    otp: Annotated[str, StringConstraints(pattern=r"^\d{6}$")]
    new_password: Password


class UserOut(BaseModel):
    id: int
    login_id: str
    email: str
    role: Role


class MessageOut(BaseModel):
    message: str


# --- master data ---

class WarehouseIn(BaseModel):
    name: Name
    short_code: ShortCode
    address: Text | None = None


class WarehouseOut(BaseModel):
    id: int
    name: str
    short_code: str
    address: str | None


class LocationIn(BaseModel):
    warehouse_id: int
    name: Name
    short_code: ShortCode


class LocationOut(BaseModel):
    id: int
    warehouse_id: int
    name: str
    short_code: str


Sku = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
Uom = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]


class ProductIn(BaseModel):
    sku: Sku
    name: Name
    category: Name
    uom: Uom = "Units"
    min_qty: Qty = Decimal(0)
    unit_cost: Money = Decimal(0)
    initial_qty: Qty = Decimal(0)
    initial_location_id: int | None = None


class ProductPatch(BaseModel):
    sku: Sku | None = None
    name: Name | None = None
    category: Name | None = None
    uom: Uom | None = None
    min_qty: Qty | None = None
    unit_cost: Money | None = None


class ProductOut(BaseModel):
    id: int
    sku: str
    name: str
    category: str
    uom: str
    min_qty: float
    unit_cost: float


class QuantOut(BaseModel):
    product_id: int
    sku: str
    product_name: str
    category: str
    uom: str
    location_id: int
    location_name: str
    warehouse_id: int
    unit_cost: float
    qty: float
    free_qty: float  # qty minus what open deliveries/transfers from this location will take; < 0 means over-promised


# --- operations ---

class LineIn(BaseModel):
    product_id: int
    qty: Qty  # adjustment: the physically counted quantity (0 allowed); otherwise the quantity moved


class OperationIn(BaseModel):
    type: OpType
    lines: Annotated[list[LineIn], Field(min_length=1, max_length=200)]
    source_location_id: int | None = None
    dest_location_id: int | None = None
    scheduled_date: date | None = None
    partner: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    delivery_address: Text | None = None
    note: Text | None = None

    @model_validator(mode="after")
    def _check_lines(self):
        if self.type != "adjustment" and any(line.qty == 0 for line in self.lines):
            raise ValueError("qty must be greater than 0")
        return self


class LineOut(BaseModel):
    product_id: int
    sku: str
    product_name: str
    uom: str
    qty: float


class OperationOut(BaseModel):
    id: int
    reference: str
    type: OpType
    status: OpStatus
    lines: list[LineOut]
    source_location_id: int | None
    dest_location_id: int | None
    warehouse_id: int
    scheduled_date: date
    partner: str | None
    delivery_address: str | None
    note: str | None
    created_by: int
    responsible: str  # login_id of created_by
    created_at: datetime


class MoveOut(BaseModel):
    """One product line of an operation, flattened for Move History."""
    operation_id: int
    reference: str
    type: OpType  # frontend: receive = in (green), delivery = out (red)
    status: OpStatus
    scheduled_date: date
    partner: str | None
    product_id: int
    sku: str
    product_name: str
    uom: str
    qty: float  # adjustment: the counted quantity; the ledger holds the resulting delta
    from_location: str | None  # receipts: the supplier
    to_location: str | None    # deliveries: the customer


class TransitionIn(BaseModel):
    from_: OpStatus = Field(alias="from")
    to: OpStatus


class TransitionOut(BaseModel):
    from_status: OpStatus | None
    to_status: OpStatus
    actor_id: int
    at: datetime


class LedgerOut(BaseModel):
    id: int
    operation_id: int
    product_id: int
    location_id: int
    delta: float
    actor_id: int
    created_at: datetime


class OperationDetail(OperationOut):
    transitions: list[TransitionOut]
    ledger: list[LedgerOut]


# --- dashboard ---

class StockAlert(BaseModel):
    product_id: int
    sku: str
    name: str
    uom: str
    on_hand: float
    min_qty: float


class OpCounts(BaseModel):
    open: int = 0      # not done or canceled
    ready: int = 0     # "4 to receive / to deliver"
    waiting: int = 0
    late: int = 0      # open and scheduled_date < today
    upcoming: int = 0  # open and scheduled_date > today


class DashboardOut(BaseModel):
    products_in_stock: int
    low_stock: int      # 0 < on_hand <= min_qty
    out_of_stock: int   # on_hand == 0
    receipts: OpCounts
    deliveries: OpCounts
    transfers: OpCounts
    alerts: list[StockAlert]  # every low or out-of-stock product
