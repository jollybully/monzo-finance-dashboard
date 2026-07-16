from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ReportRun
from app.schemas import ReportRunOut, SafeSpendOut, SyncResult
from app.services.reports import generate_and_send_report
from app.services.safe_spend import calculate_safe_spend
from app.services.sheets_sync import sync_transactions

router = APIRouter(prefix="/api", tags=["api"])


@router.get("/safe-spend", response_model=SafeSpendOut)
def api_safe_spend(db: Session = Depends(get_db)) -> SafeSpendOut:
    return calculate_safe_spend(db)


@router.post("/sync", response_model=SyncResult)
def api_sync(db: Session = Depends(get_db)) -> SyncResult:
    try:
        return sync_transactions(db)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/reports", response_model=list[ReportRunOut])
def api_list_reports(
    db: Session = Depends(get_db), limit: int = Query(50, ge=1, le=200)
) -> list[ReportRunOut]:
    rows = (
        db.query(ReportRun)
        .order_by(ReportRun.sent_at.desc())
        .limit(limit)
        .all()
    )
    return [ReportRunOut.model_validate(r) for r in rows]


@router.post("/reports/{period}/send", response_model=ReportRunOut)
def api_send_report(
    period: str,
    send: bool = Query(True),
    db: Session = Depends(get_db),
) -> ReportRunOut:
    if period not in {"daily", "weekly", "monthly"}:
        raise HTTPException(status_code=400, detail="period must be daily, weekly, or monthly")
    try:
        run = generate_and_send_report(db, period, send=send)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ReportRunOut.model_validate(run)
