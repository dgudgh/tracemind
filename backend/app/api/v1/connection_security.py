from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.connection_security import (
    CertificateProfileAuditResponse,
    CertificateProfileCreate,
    CertificateProfileResponse,
    CertificateProfileUpdate,
)
from app.services import connection_security as service


router = APIRouter(prefix="/connection-security", tags=["连接安全与证书"])


def _actor(request: Request) -> str:
    auth = getattr(request.state, "auth", {}) or {}
    return str(auth.get("username") or auth.get("sub") or auth.get("user_id") or "system")


def _profile_or_404(db: Session, profile_id: int):
    row = service.get_profile(db, profile_id)
    if not row:
        raise HTTPException(status_code=404, detail="Certificate profile not found")
    return row


@router.get("/certificates", response_model=list[CertificateProfileResponse])
def list_certificate_profiles(db: Session = Depends(get_db)):
    return service.list_profiles(db)


@router.post("/certificates", response_model=CertificateProfileResponse, status_code=201)
def create_certificate_profile(
    data: CertificateProfileCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        return service.serialize_profile(service.create_profile(db, data, _actor(request)))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/certificates/{profile_id}", response_model=CertificateProfileResponse)
def update_certificate_profile(
    profile_id: int,
    data: CertificateProfileUpdate,
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        row = service.update_profile(db, _profile_or_404(db, profile_id), data, _actor(request))
        return service.serialize_profile(row)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/certificates/{profile_id}", status_code=204)
def delete_certificate_profile(
    profile_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        service.delete_profile(db, _profile_or_404(db, profile_id), _actor(request))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/certificates/{profile_id}/test")
def test_certificate_profile(
    profile_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        return service.test_profile(_profile_or_404(db, profile_id), db, _actor(request))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get(
    "/certificates/{profile_id}/audits",
    response_model=list[CertificateProfileAuditResponse],
)
def list_certificate_audits(
    profile_id: int,
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    _profile_or_404(db, profile_id)
    return service.audits(db, profile_id, limit)


@router.get("/minimum-privilege-templates")
def minimum_privilege_templates(db_type: str | None = Query(None, pattern="^(oracle|mysql|postgresql|sqlserver)$")):
    rows = service.minimum_privilege_catalog()
    return [row for row in rows if not db_type or row["db_type"] == db_type]


@router.get("/compatibility-matrix")
def connection_compatibility_matrix():
    return service.compatibility_matrix()
