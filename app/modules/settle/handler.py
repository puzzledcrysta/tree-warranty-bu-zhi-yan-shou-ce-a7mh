from fastapi import APIRouter
from pydantic import Field
from app.inputs import Note
from .service import plan, sign, signoffs_for_run, history

router = APIRouter()


class Signing(Note):
    period: str | None = Field(default=None, max_length=7)


@router.get('/api/settle/{run_id}')
def preview(run_id: str, period: str | None = None):
    return plan(run_id, period)


@router.post('/api/settle/{run_id}')
def commit(run_id: str, signing: Signing):
    return sign(run_id, signing.period, signing.note)


@router.get('/api/settle/{run_id}/signoffs')
def run_signoffs(run_id: str):
    return signoffs_for_run(run_id)


@router.get('/api/settle')
def ledger(period: str | None = None):
    return history(period)
