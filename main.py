
from fastapi import FastAPI, APIRouter, Depends, HTTPException, Query, Path
from contextlib import asynccontextmanager
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import date, timedelta, datetime

# Direct import from your local database.py file sitting next to main.py
from database import db_manager, get_db

# ── 1. Lifespan Configuration for Neon DB Connection Pool ───────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Connect to your Neon DB
    await db_manager.connect()
    app.state.db_pool = db_manager.pool
    
    yield
    
    # Shutdown: Clean up connections
    await db_manager.disconnect()


app = FastAPI(
    title="Project HalfBill — Revenue Operations Engine",
    version="1.0.0",
    lifespan=lifespan
)

router = APIRouter(prefix="/billing")

# ── 2. Pydantic Schemas ────────────────────────────────────────

class InvoiceLineItemOut(BaseModel):
    id: int
    product_id: Optional[int] = None
    product_name: Optional[str] = None
    description: str
    quantity: int
    unit_price: float
    subtotal: float
    line_type: str

class InvoiceOut(BaseModel):
    id: int
    invoice_number: str
    customer_id: int
    customer_name: str
    quote_id: Optional[int] = None
    status: str
    billing_period_start: Optional[str] = None
    billing_period_end: Optional[str] = None
    due_date: Optional[str] = None
    subtotal: float
    tax_rate: float
    tax_amount: float
    total_amount: float
    paid_amount: float
    balance_due: float
    currency: str = "USD"
    line_items: Optional[List[InvoiceLineItemOut]] = None

class CreateInvoiceRequest(BaseModel):
    customer_id: int
    quote_id: Optional[int] = None
    billing_period_start: Optional[date] = None
    billing_period_end: Optional[date] = None
    notes: Optional[str] = None

class UpdateInvoiceStatusRequest(BaseModel):
    status: str

class CreatePaymentRequest(BaseModel):
    invoice_id: int
    amount: float = Field(..., ge=0.01)
    payment_method: str
    transaction_ref: Optional[str] = None

class PaymentOut(BaseModel):
    id: int
    invoice_id: int
    customer_id: int
    amount: float
    payment_method: str
    status: str
    transaction_ref: Optional[str] = None
    paid_at: Optional[str] = None

class CreateAnomalyRequest(BaseModel):
    invoice_id: int
    anomaly_type: str
    severity: str
    amount_affected: Optional[float] = None
    description: str

class UpdateAnomalyStatusRequest(BaseModel):
    status: str
    resolution: Optional[str] = None

class AnomalyOut(BaseModel):
    id: int
    invoice_id: int
    invoice_number: str
    customer_id: int
    customer_name: str
    anomaly_type: str
    severity: str
    amount_affected: Optional[float] = None
    description: str
    status: str
    detected_at: str
    resolved_at: Optional[str] = None

class BillingMetrics(BaseModel):
    invoices_issued_today: int
    invoices_paid_today: int
    invoices_overdue: int
    revenue_collected_mtd: float
    anomalies_open: int
    anomalies_critical: int
    leakage_amount_flagged: float
# [9/14] Create Anomaly
@router.post("/anomalies", response_model=AnomalyOut, status_code=201, tags=["Anomalies"])
async def create_anomaly(req: CreateAnomalyRequest, db=Depends(get_db)):
    inv = await db.fetchrow("SELECT i.invoice_number, i.customer_id, c.name FROM billing_invoices i JOIN customers c ON i.customer_id = c.id WHERE i.id = $1", req.invoice_id)
    if not inv:
        raise HTTPException(404, f"Invoice ID {req.invoice_id} not found")

    row = await db.fetchrow("""
        INSERT INTO billing_anomalies (invoice_id, customer_id, anomaly_type, severity, amount_affected, description, status, detected_at)
        VALUES ($1, $2, $3, $4, $5, $6, 'open', NOW()) RETURNING *
    """, req.invoice_id, inv["customer_id"], req.anomaly_type, req.severity, req.amount_affected, req.description)
    
    return {**dict(row), "invoice_number": inv["invoice_number"], "customer_name": inv["name"], "detected_at": str(row["detected_at"]), "resolved_at": str(row["resolved_at"]) if row["resolved_at"] else None}

# [10/14] List Anomalies
@router.get("/anomalies", response_model=List[AnomalyOut], tags=["Anomalies"])
async def list_anomalies(status: Optional[str] = None, severity: Optional[str] = None, db=Depends(get_db)):
    query = """
        SELECT a.*, i.invoice_number, c.name AS customer_name FROM billing_anomalies a
        JOIN billing_invoices i ON a.invoice_id = i.id JOIN customers c ON a.customer_id = c.id WHERE 1=1
    """
    params = []
    if status:
        params.append(status)
        query += f" AND a.status = ${len(params)}"
    if severity:
        params.append(severity)
        query += f" AND a.severity = ${len(params)}"
    query += " ORDER BY a.detected_at DESC"
    
    rows = await db.fetch(query, *params)
    return [{**dict(r), "detected_at": str(r["detected_at"]), "resolved_at": str(r["resolved_at"]) if r["resolved_at"] else None} for r in rows]

# [11/14] Get Open Anomalies
@router.get("/anomalies/open", response_model=List[AnomalyOut], tags=["Anomalies"])
async def get_open_anomalies(db=Depends(get_db)):
    rows = await db.fetch("""
        SELECT a.*, i.invoice_number, c.name AS customer_name FROM billing_anomalies a
        JOIN billing_invoices i ON a.invoice_id = i.id JOIN customers c ON a.customer_id = c.id
        WHERE a.status = 'open'
        ORDER BY CASE a.severity WHEN 'critical' THEN 1 WHEN 'high' THEN 2 WHEN 'medium' THEN 3 WHEN 'low' THEN 4 END, a.detected_at DESC
    """)
    return [{**dict(r), "detected_at": str(r["detected_at"]), "resolved_at": str(r["resolved_at"]) if r["resolved_at"] else None} for r in rows]

# [12/14] Get Anomaly by ID
@router.get("/anomalies/{anomaly_id}", response_model=AnomalyOut, tags=["Anomalies"])
async def get_anomaly(anomaly_id: int, db=Depends(get_db)):
    row = await db.fetchrow("""
        SELECT a.*, i.invoice_number, c.name AS customer_name FROM billing_anomalies a
        JOIN billing_invoices i ON a.invoice_id = i.id JOIN customers c ON a.customer_id = c.id WHERE a.id = $1
    """, anomaly_id)
    if not row:
        raise HTTPException(404, f"Anomaly trace ID {anomaly_id} missing")
    return {**dict(row), "detected_at": str(row["detected_at"]), "resolved_at": str(row["resolved_at"]) if row["resolved_at"] else None}

# [13/14] Update Anomaly Status
@router.put("/anomalies/{anomaly_id}/status", response_model=AnomalyOut, tags=["Anomalies"])
async def update_anomaly_status(anomaly_id: int, req: UpdateAnomalyStatusRequest, db=Depends(get_db)):
    exists = await db.fetchval("SELECT id FROM billing_anomalies WHERE id = $1", anomaly_id)
    if not exists:
        raise HTTPException(404, f"Anomaly trace ID {anomaly_id} missing")

    resolved_time = datetime.now() if req.status == "resolved" else None
    await db.execute("UPDATE billing_anomalies SET status = $1, resolution = $2, resolved_at = $3 WHERE id = $4", req.status, req.resolution, resolved_time, anomaly_id)
    return await get_anomaly(anomaly_id, db)

# [14/14] Access Aggregated Metrics
@router.get("/metrics", response_model=BillingMetrics, tags=["Metrics"])
async def get_metrics(db=Depends(get_db)):
    inv = await db.fetchrow("""
        SELECT COUNT(*) FILTER (WHERE DATE(issued_at) = CURRENT_DATE) AS issued,
               COUNT(*) FILTER (WHERE status='paid' AND DATE(updated_at) = CURRENT_DATE) AS paid,
               COUNT(*) FILTER (WHERE status='overdue') AS overdue,
               COALESCE(SUM(paid_amount) FILTER (WHERE DATE_TRUNC('month', updated_at) = DATE_TRUNC('month', NOW())), 0.0) AS mtd
        FROM billing_invoices
    """)
    anom = await db.fetchrow("""
        SELECT COUNT(*) FILTER (WHERE status='open') AS open,
               COUNT(*) FILTER (WHERE status='open' AND severity='critical') AS crit,
               COALESCE(SUM(amount_affected) FILTER (WHERE status='open'), 0.0) AS leaked
        FROM billing_anomalies
    """)
    return {
        "invoices_issued_today": inv["issued"],
        "invoices_paid_today": inv["paid"],
        "invoices_overdue": inv["overdue"],
        "revenue_collected_mtd": float(inv["mtd"]),
        "anomalies_open": anom["open"],
        "anomalies_critical": anom["crit"],
        "leakage_amount_flagged": float(anom["leaked"])
    }