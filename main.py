
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


# ── 3. Internal Database Helper Utility ─────────────────────────

async def _fetch_invoice_with_lines(param: str, is_id: bool, db) -> dict:
    condition = "i.id = $1" if is_id else "i.invoice_number = $1"
    bind_value = int(param) if is_id else param

    row = await db.fetchrow(f"""
        SELECT i.*, c.name AS customer_name 
        FROM billing_invoices i
        JOIN customers c ON i.customer_id = c.id 
        WHERE {condition}
    """, bind_value)
    
    if not row:
        raise HTTPException(404, f"Invoice {param} not found")
        
    result = dict(row)
    lines = await db.fetch("""
        SELECT li.*, p.name AS product_name 
        FROM billing_invoice_line_items li
        LEFT JOIN products p ON li.product_id = p.id 
        WHERE li.invoice_id = $1
    """, result["id"])
    
    result["billing_period_start"] = str(result["billing_period_start"]) if result["billing_period_start"] else None
    result["billing_period_end"] = str(result["billing_period_end"]) if result["billing_period_end"] else None
    result["due_date"] = str(result["due_date"]) if result["due_date"] else None
    result["line_items"] = [dict(l) for l in lines]
    return result


# ── 4. The 14 API Routes Implementation ──────────────────────────

# [1/14] Create Invoice
@router.post("/invoices", response_model=InvoiceOut, status_code=201, tags=["Invoices"])
async def create_invoice(req: CreateInvoiceRequest, db=Depends(get_db)):
    customer = await db.fetchrow("SELECT name FROM customers WHERE id = $1", req.customer_id)
    if not customer:
        raise HTTPException(404, f"Customer {req.customer_id} not found")

    count = await db.fetchval("SELECT COUNT(*) FROM billing_invoices")
    invoice_number = f"INV-{date.today().year}-{str(count + 1).zfill(5)}"

    period_start = req.billing_period_start or date.today().replace(day=1)
    if not req.billing_period_end:
        if period_start.month == 12:
            next_month = period_start.replace(year=period_start.year + 1, month=1, day=1)
        else:
            next_month = period_start.replace(month=period_start.month + 1, day=1)
        period_end = next_month - timedelta(days=1)
    else:
        period_end = req.billing_period_end
    due_date = period_end + timedelta(days=14)

    subtotal = 0.0
    quote_items = []

    if req.quote_id:
        quote = await db.fetchrow("SELECT status, total_mrr, total_otc FROM cpq_quotes WHERE id = $1", req.quote_id)
        if not quote:
            raise HTTPException(404, f"Quote {req.quote_id} not found")
        if quote["status"] != "accepted":
            raise HTTPException(400, f"Quote {req.quote_id} is '{quote['status']}', must be 'accepted'")
        
        quote_items = await db.fetch("SELECT * FROM cpq_quote_line_items WHERE quote_id = $1", req.quote_id)
        subtotal = float(quote["total_mrr"] or 0.0) + float(quote["total_otc"] or 0.0)

    tax_rate = 8.5
    tax_amount = round(subtotal * (tax_rate / 100.0), 2)
    total_amount = round(subtotal + tax_amount, 2)

    invoice_row = await db.fetchrow("""
        INSERT INTO billing_invoices
            (invoice_number, customer_id, quote_id, status, billing_period_start, 
             billing_period_end, due_date, subtotal, tax_rate, tax_amount, 
             total_amount, paid_amount, currency, notes, issued_at, updated_at)
        VALUES ($1, $2, $3, 'draft', $4, $5, $6, $7, $8, $9, $10, 0.0, 'USD', $11, NOW(), NOW())
        RETURNING *
    """, invoice_number, req.customer_id, req.quote_id, period_start, period_end, due_date, 
         subtotal, tax_rate, tax_amount, total_amount, req.notes)

    inserted_lines = []
    for item in quote_items:
        line_row = await db.fetchrow("""
            INSERT INTO billing_invoice_line_items
                (invoice_id, product_id, description, quantity, unit_price, subtotal, line_type)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING *
        """, invoice_row["id"], item.get("product_id"), item.get("description", ""), 
             item.get("quantity", 1), item.get("unit_price", 0.0), item.get("subtotal", 0.0), item.get("line_type", "recurring"))
        
        p_name = None
        if line_row["product_id"]:
            p_name = await db.fetchval("SELECT name FROM products WHERE id = $1", line_row["product_id"])
        
        ld = dict(line_row)
        ld["product_name"] = p_name
        inserted_lines.append(ld)

    result = dict(invoice_row)
    result["customer_name"] = customer["name"]
    result["billing_period_start"] = str(result["billing_period_start"])
    result["billing_period_end"] = str(result["billing_period_end"])
    result["due_date"] = str(result["due_date"])
    result["line_items"] = inserted_lines
    return result

# [2/14] List Invoices
@router.get("/invoices", response_model=List[InvoiceOut], tags=["Invoices"])
async def list_invoices(status: Optional[str] = None, limit: int = Query(20, ge=1, le=100), db=Depends(get_db)):
    if status:
        rows = await db.fetch("""
            SELECT i.*, c.name AS customer_name FROM billing_invoices i
            JOIN customers c ON i.customer_id = c.id WHERE i.status = $1
            ORDER BY i.issued_at DESC LIMIT $2
        """, status, limit)
    else:
        rows = await db.fetch("""
            SELECT i.*, c.name AS customer_name FROM billing_invoices i
            JOIN customers c ON i.customer_id = c.id
            ORDER BY i.issued_at DESC LIMIT $1
        """, limit)
    return [{**dict(r), "billing_period_start": str(r["billing_period_start"]), "billing_period_end": str(r["billing_period_end"]), "due_date": str(r["due_date"])} for r in rows]

# [3/14] Overdue Invoices
@router.get("/invoices/overdue", response_model=List[InvoiceOut], tags=["Invoices"])
async def get_overdue_invoices(db=Depends(get_db)):
    rows = await db.fetch("""
        SELECT i.*, c.name AS customer_name FROM billing_invoices i
        JOIN customers c ON i.customer_id = c.id
        WHERE i.status = 'overdue' ORDER BY i.due_date ASC
    """)
    return [{**dict(r), "billing_period_start": str(r["billing_period_start"]), "billing_period_end": str(r["billing_period_end"]), "due_date": str(r["due_date"])} for r in rows]

# [4/14] Get Invoice by Number
@router.get("/invoices/{invoice_number}", response_model=InvoiceOut, tags=["Invoices"])
async def get_invoice(invoice_number: str = Path(..., example="INV-2024-00001"), db=Depends(get_db)):
    return await _fetch_invoice_with_lines(invoice_number, is_id=False, db=db)

# [5/14] Update Invoice Status
@router.put("/invoices/{invoice_number}/status", response_model=InvoiceOut, tags=["Invoices"])
async def update_invoice_status(invoice_number: str, req: UpdateInvoiceStatusRequest, db=Depends(get_db)):
    exists = await db.fetchval("SELECT id FROM billing_invoices WHERE invoice_number = $1", invoice_number)
    if not exists:
        raise HTTPException(404, f"Invoice {invoice_number} not found")

    await db.execute("UPDATE billing_invoices SET status = $1, updated_at = NOW() WHERE invoice_number = $2", req.status, invoice_number)
    return await _fetch_invoice_with_lines(invoice_number, is_id=False, db=db)

# [6/14] Get Customer Invoices
@router.get("/invoices/customer/{customer_id}", response_model=List[InvoiceOut], tags=["Invoices"])
async def get_customer_invoices(customer_id: int, db=Depends(get_db)):
    rows = await db.fetch("""
        SELECT i.*, c.name AS customer_name FROM billing_invoices i
        JOIN customers c ON i.customer_id = c.id WHERE i.customer_id = $1
        ORDER BY i.issued_at DESC
    """, customer_id)
    return [{**dict(r), "billing_period_start": str(r["billing_period_start"]), "billing_period_end": str(r["billing_period_end"]), "due_date": str(r["due_date"])} for r in rows]
