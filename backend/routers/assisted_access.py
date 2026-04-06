from __future__ import annotations

import os
import secrets
import time
import urllib.parse
from fastapi import APIRouter, Depends, HTTPException, Request
from jose import jwt
from sqlalchemy.orm import Session

from backend.models.database import get_db, User, SharedAccess, Credential
from backend.models.assisted_access import AssistedAccessRequest
from backend.routers.auth import get_current_user
from backend.utils.delegation import hash_token
from backend.schemas.assisted_access import (
    AssistedApproveOut,
    AssistedCompleteRequest,
    AssistedCreateRequest,
    AssistedRequestOut,
    AssistedStatusOut,
)

router = APIRouter(prefix="/sharing/assisted", tags=["Assisted Access"])

REQUEST_TTL_SECONDS = 10 * 60
HANDOFF_TTL_SECONDS = 10 * 60

JWT_SECRET = os.getenv("JWT_SECRET", "CHANGE_ME_IN_PRODUCTION_USE_256BIT_KEY")
JWT_ALGORITHM = "HS256"

# Simple shared secret for MVP (put in .env)
ASSIST_COMPLETE_SECRET = os.getenv("ASSIST_COMPLETE_SECRET", "CHANGE_ME")


def _sign_handoff(payload: dict) -> str:
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def resolve_share_token_or_404(db: Session, token: str, requester_email: str) -> tuple[int, int, str]:
    token_hash = hash_token(token)

    shared = (
        db.query(SharedAccess)
        .filter(
            SharedAccess.token_hash == token_hash,
            SharedAccess.is_revoked == False,
        )
        .first()
    )
    if not shared:
        raise HTTPException(status_code=404, detail="Share not found")

    if time.time() > shared.expires_at:
        raise HTTPException(status_code=403, detail="Share expired")

    if (shared.recipient_email or "").strip().lower() != (requester_email or "").strip().lower():
        raise HTTPException(status_code=403, detail="Recipient email not authorized for this share")

    cred = db.query(Credential).filter(Credential.id == shared.credential_id).first()
    if not cred or not (cred.service_url or "").strip():
        raise HTTPException(status_code=404, detail="Credential/service_url not found")

    recipient_user = db.query(User).filter(User.email == requester_email).first()
    if not recipient_user:
        raise HTTPException(status_code=404, detail="Recipient user not found")

    service_url = cred.service_url.strip()
    return (shared.owner_id, recipient_user.id, service_url)


@router.post("/request", response_model=AssistedRequestOut)
def create_request(
    payload: AssistedCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    requester_email = (current_user.email or "").strip().lower()
    if not requester_email:
        raise HTTPException(status_code=401, detail="Authenticated user email missing")

    owner_id, recipient_id, service_url = resolve_share_token_or_404(db, payload.token, requester_email)

    now = time.time()
    r = AssistedAccessRequest(
        owner_id=owner_id,
        recipient_id=recipient_id,
        share_token_hash=hash_token(payload.token),
        service_url=service_url,
        status="pending",
        created_at=now,
        expires_at=now + REQUEST_TTL_SECONDS,
        delegation_token=None,
        handoff_session_id=None,
    )
    db.add(r)
    db.commit()
    db.refresh(r)

    return AssistedRequestOut(
        request_id=r.id,
        status=r.status,
        service_url=r.service_url,
        expires_at=r.expires_at,
    )


@router.get("/pending", response_model=list[AssistedRequestOut])
def list_pending(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    now = time.time()
    reqs = (
        db.query(AssistedAccessRequest)
        .filter(
            AssistedAccessRequest.owner_id == current_user.id,
            AssistedAccessRequest.status == "pending",
            AssistedAccessRequest.expires_at > now,
        )
        .order_by(AssistedAccessRequest.created_at.desc())
        .all()
    )
    return [
        AssistedRequestOut(
            request_id=r.id,
            status=r.status,
            service_url=r.service_url,
            expires_at=r.expires_at,
        )
        for r in reqs
    ]


@router.post("/{request_id}/approve", response_model=AssistedApproveOut)
def approve(
    request_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    r = db.query(AssistedAccessRequest).filter(AssistedAccessRequest.id == request_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Request not found")
    if r.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not allowed")
    if r.expires_at <= time.time():
        r.status = "expired"
        db.commit()
        raise HTTPException(status_code=400, detail="Request expired")
    if r.status != "pending":
        raise HTTPException(status_code=400, detail=f"Bad status: {r.status}")

    # Owner now goes to a page on YOUR SITE to perform CAPTCHA/2FA.
    # For MVP we reuse service_url and attach assist_request_id.
    r.status = "approved"
    db.commit()

    assist_login_url = r.service_url
    try:
        u = urllib.parse.urlparse(r.service_url)
        q = dict(urllib.parse.parse_qsl(u.query))
        q["assist_request_id"] = str(r.id)
        assist_login_url = urllib.parse.urlunparse(u._replace(query=urllib.parse.urlencode(q)))
    except Exception:
        # fallback: append as query
        sep = "&" if "?" in r.service_url else "?"
        assist_login_url = f"{r.service_url}{sep}assist_request_id={r.id}"

    return AssistedApproveOut(
        request_id=r.id,
        status=r.status,
        delegation_token="",  # not used anymore in approve
        handoff_url="",       # not ready yet
        expires_at=r.expires_at,
    ) | {"assist_login_url": assist_login_url}  # if your schema doesn't include it, add it to schema!


@router.post("/{request_id}/complete", response_model=AssistedStatusOut)
def complete(
    request_id: int,
    payload: AssistedCompleteRequest,
    db: Session = Depends(get_db),
):
    """
    Called by YOUR SITE after owner completes login + CAPTCHA/2FA successfully.
    This should NOT be called from the recipient.
    """
    r = db.query(AssistedAccessRequest).filter(AssistedAccessRequest.id == request_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Request not found")
    if r.expires_at <= time.time():
        r.status = "expired"
        db.commit()
        raise HTTPException(status_code=400, detail="Request expired")
    if r.status != "approved":
        raise HTTPException(status_code=400, detail=f"Bad status: {r.status}")

    if (payload.proof or "") != ASSIST_COMPLETE_SECRET:
        raise HTTPException(status_code=403, detail="Invalid proof")

    # Issue a one-time handoff_token for /handoff
    handoff_payload = {
        "typ": "handoff",
        "rid": r.id,
        "recipient_id": r.recipient_id,
        "service_url": r.service_url,
        "jti": secrets.token_urlsafe(16),
        "iat": int(time.time()),
        "exp": int(time.time() + HANDOFF_TTL_SECONDS),
    }
    handoff_token = _sign_handoff(handoff_payload)

    r.status = "completed"
    r.delegation_token = handoff_token  # reuse column
    db.commit()

    return AssistedStatusOut(
        request_id=r.id,
        status=r.status,
        handoff_url=f"/handoff?handoff_token={handoff_token}&redirect_to=/app",
        expires_at=r.expires_at,
    )


@router.get("/{request_id}/status", response_model=AssistedStatusOut)
def status(
    request_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    r = db.query(AssistedAccessRequest).filter(AssistedAccessRequest.id == request_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Request not found")

    if current_user.id not in (r.owner_id, r.recipient_id):
        raise HTTPException(status_code=403, detail="Not allowed")

    if r.expires_at <= time.time() and r.status in ("pending", "approved"):
        r.status = "expired"
        db.commit()

    if r.status == "completed" and r.delegation_token:
        return AssistedStatusOut(
            request_id=r.id,
            status=r.status,
            handoff_url=f"/handoff?handoff_token={r.delegation_token}&redirect_to=/app",
            expires_at=r.expires_at,
        )

    return AssistedStatusOut(
        request_id=r.id,
        status=r.status,
        handoff_url=None,
        expires_at=r.expires_at,
    )
































# from __future__ import annotations

# import os
# import secrets
# import time
# from fastapi import APIRouter, Depends, HTTPException
# from jose import jwt
# from sqlalchemy.orm import Session

# from backend.models.database import get_db, User, SharedAccess, Credential
# from backend.models.assisted_access import AssistedAccessRequest
# from backend.routers.auth import get_current_user
# from backend.utils.delegation import hash_token

# from backend.schemas.assisted_access import (
#     AssistedApproveOut,
#     AssistedCreateRequest,
#     AssistedRequestOut,
#     AssistedStatusOut,
# )



# router = APIRouter(prefix="/sharing/assisted", tags=["Assisted Access"])

# REQUEST_TTL_SECONDS = 10 * 60
# HANDOFF_TTL_SECONDS = 10 * 60

# JWT_SECRET = os.getenv("JWT_SECRET", "CHANGE_ME_IN_PRODUCTION_USE_256BIT_KEY")
# JWT_ALGORITHM = "HS256"






# def _sign_handoff(payload: dict) -> str:
#     return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


# def resolve_share_token_or_404(db: Session, token: str, requester_email: str) -> tuple[int, int, str]:
#     """
#     Resolve token -> (owner_id, recipient_user_id, service_url)
#     using YOUR existing SharedAccess + Credential tables.

#     requester_email must match SharedAccess.recipient_email.
#     """
#     token_hash = hash_token(token)

#     shared = (
#         db.query(SharedAccess)
#         .filter(
#             SharedAccess.token_hash == token_hash,
#             SharedAccess.is_revoked == False,
#         )
#         .first()
#     )
#     if not shared:
#         raise HTTPException(status_code=404, detail="Share not found")

#     if time.time() > shared.expires_at:
#         raise HTTPException(status_code=403, detail="Share expired")

#     if (shared.recipient_email or "").strip().lower() != (requester_email or "").strip().lower():
#         raise HTTPException(status_code=403, detail="Recipient email not authorized for this share")

#     cred = db.query(Credential).filter(Credential.id == shared.credential_id).first()
#     if not cred or not (cred.service_url or "").strip():
#         raise HTTPException(status_code=404, detail="Credential/service_url not found")

#     recipient_user = db.query(User).filter(User.email == requester_email).first()
#     if not recipient_user:
#         raise HTTPException(status_code=404, detail="Recipient user not found")

#     service_url = cred.service_url.strip()
#     return (shared.owner_id, recipient_user.id, service_url)


# @router.post("/request", response_model=AssistedRequestOut)
# def create_request(
#     payload: AssistedCreateRequest,
#     current_user: User = Depends(get_current_user),
#     db: Session = Depends(get_db),
# ):
#     requester_email = (current_user.email or "").strip().lower()
#     if not requester_email:
#         raise HTTPException(status_code=401, detail="Authenticated user email missing")

#     owner_id, recipient_id, service_url = resolve_share_token_or_404(db, payload.token, requester_email)

#     now = time.time()
#     r = AssistedAccessRequest(
#         owner_id=owner_id,
#         recipient_id=recipient_id,
#         share_token_hash=hash_token(payload.token),
#         service_url=service_url,
#         status="pending",
#         created_at=now,
#         expires_at=now + REQUEST_TTL_SECONDS,
#         delegation_token=None,
#         handoff_session_id=None,
#     )
#     db.add(r)
#     db.commit()
#     db.refresh(r)

#     return AssistedRequestOut(
#         request_id=r.id,
#         status=r.status,
#         service_url=r.service_url,
#         expires_at=r.expires_at,
#     )


# @router.get("/pending", response_model=list[AssistedRequestOut])
# def list_pending(
#     current_user: User = Depends(get_current_user),
#     db: Session = Depends(get_db),
# ):
#     now = time.time()
#     reqs = (
#         db.query(AssistedAccessRequest)
#         .filter(
#             AssistedAccessRequest.owner_id == current_user.id,
#             AssistedAccessRequest.status == "pending",
#             AssistedAccessRequest.expires_at > now,
#         )
#         .order_by(AssistedAccessRequest.created_at.desc())
#         .all()
#     )
#     return [
#         AssistedRequestOut(
#             request_id=r.id,
#             status=r.status,
#             service_url=r.service_url,
#             expires_at=r.expires_at,
#         )
#         for r in reqs
#     ]


# @router.post("/{request_id}/approve", response_model=AssistedApproveOut)
# def approve(
#     request_id: int,
#     current_user: User = Depends(get_current_user),
#     db: Session = Depends(get_db),
# ):
#     r = db.query(AssistedAccessRequest).filter(AssistedAccessRequest.id == request_id).first()
#     if not r:
#         raise HTTPException(status_code=404, detail="Request not found")
#     if r.owner_id != current_user.id:
#         raise HTTPException(status_code=403, detail="Not allowed")
#     if r.expires_at <= time.time():
#         r.status = "expired"
#         db.commit()
#         raise HTTPException(status_code=400, detail="Request expired")
#     if r.status != "pending":
#         raise HTTPException(status_code=400, detail=f"Bad status: {r.status}")

#     # Create handoff token (delegation for YOUR site)
#     handoff_payload = {
#         "typ": "handoff",
#         "rid": r.id,
#         "recipient_id": r.recipient_id,
#         "service_url": r.service_url,
#         "jti": secrets.token_urlsafe(16),
#         "iat": int(time.time()),
#         "exp": int(time.time() + HANDOFF_TTL_SECONDS),
#     }
#     handoff_token = _sign_handoff(handoff_payload)

#     r.status = "completed"
#     r.delegation_token = handoff_token  # reuse column for simplicity
#     db.commit()

#     return AssistedApproveOut(
#         request_id=r.id,
#         status=r.status,
#         delegation_token=handoff_token,
#         handoff_url=f"/handoff?handoff_token={handoff_token}&redirect_to=/app",
#         expires_at=r.expires_at,
#     )


# @router.get("/{request_id}/status", response_model=AssistedStatusOut)
# def status(
#     request_id: int,
#     current_user: User = Depends(get_current_user),
#     db: Session = Depends(get_db),
# ):
#     r = db.query(AssistedAccessRequest).filter(AssistedAccessRequest.id == request_id).first()
#     if not r:
#         raise HTTPException(status_code=404, detail="Request not found")

#     if current_user.id not in (r.owner_id, r.recipient_id):
#         raise HTTPException(status_code=403, detail="Not allowed")

#     if r.expires_at <= time.time() and r.status in ("pending",):
#         r.status = "expired"
#         db.commit()

#     # We expose handoff token only when completed
#     handoff_token = r.delegation_token if r.status == "completed" else None

#     return AssistedStatusOut(
#         request_id=r.id,
#         status=r.status,
#         handoff_url=(f"/handoff?handoff_token={handoff_token}&redirect_to=/app" if handoff_token else None),
#         expires_at=r.expires_at,
#     )
































# # from __future__ import annotations

# # import os
# # import time
# # from fastapi import APIRouter, Depends, HTTPException
# # import jwt
# # from sqlalchemy.orm import Session
# # import secrets

# # from backend.models.database import get_db
# # from backend.models.assisted_access import AssistedAccessRequest
# # from backend.models.database import User
# # from backend.routers.auth import get_current_user

# # from backend.schemas.assisted_access import (
# #     AssistedApproveOut,
# #     AssistedCreateRequest,
# #     AssistedRequestOut,
# #     AssistedStatusOut,
# # )

# # from backend.utils.delegation import hash_token, create_delegation_token

# # router = APIRouter(prefix="/assisted", tags=["Assisted Access"])

# # REQUEST_TTL_SECONDS = 10 * 60
# # DELEGATION_TTL_SECONDS = 5 * 60


# # JWT_SECRET = os.getenv("JWT_SECRET", "CHANGE_ME_IN_PRODUCTION_USE_256BIT_KEY")
# # JWT_ALGORITHM = "HS256"

# # def _sign_handoff(payload: dict) -> str:
# #     return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


# # def resolve_share_token_or_404(db: Session, token: str):
# #     """
# #     TODO: IMPORTANT: plug into your existing share token model.

# #     Must return:
# #       (owner_id, recipient_id, service_url)

# #     - owner_id: the credential owner
# #     - recipient_id: current user id (recipient)
# #     - service_url: target site/app URL
# #     """
# #     raise HTTPException(status_code=501, detail="Wire resolve_share_token_or_404 to your share model")


# # @router.post("/request", response_model=AssistedRequestOut)
# # def create_request(
# #     payload: AssistedCreateRequest,
# #     current_user: User = Depends(get_current_user),
# #     db: Session = Depends(get_db),
# # ):
# #     owner_id, recipient_id, service_url = resolve_share_token_or_404(db, payload.token)

# #     if recipient_id != current_user.id:
# #         raise HTTPException(status_code=403, detail="This token is not for the current user")

# #     now = time.time()
# #     r = AssistedAccessRequest(
# #         owner_id=owner_id,
# #         recipient_id=recipient_id,
# #         share_token_hash=hash_token(payload.token),
# #         service_url=service_url,
# #         status="pending",
# #         created_at=now,
# #         expires_at=now + REQUEST_TTL_SECONDS,
# #     )
# #     db.add(r)
# #     db.commit()
# #     db.refresh(r)

# #     return AssistedRequestOut(
# #         request_id=r.id,
# #         status=r.status,
# #         service_url=r.service_url,
# #         expires_at=r.expires_at,
# #     )


# # @router.get("/pending", response_model=list[AssistedRequestOut])
# # def list_pending(
# #     current_user: User = Depends(get_current_user),
# #     db: Session = Depends(get_db),
# # ):
# #     now = time.time()
# #     reqs = (
# #         db.query(AssistedAccessRequest)
# #         .filter(
# #             AssistedAccessRequest.owner_id == current_user.id,
# #             AssistedAccessRequest.status == "pending",
# #             AssistedAccessRequest.expires_at > now,
# #         )
# #         .order_by(AssistedAccessRequest.created_at.desc())
# #         .all()
# #     )
# #     return [
# #         AssistedRequestOut(
# #             request_id=r.id,
# #             status=r.status,
# #             service_url=r.service_url,
# #             expires_at=r.expires_at,
# #         )
# #         for r in reqs
# #     ]


# # @router.post("/{request_id}/approve", response_model=AssistedApproveOut)
# # def approve(
# #     request_id: int,
# #     current_user: User = Depends(get_current_user),
# #     db: Session = Depends(get_db),
# # ):
# #     r = db.query(AssistedAccessRequest).filter(AssistedAccessRequest.id == request_id).first()
# #     if not r:
# #         raise HTTPException(status_code=404, detail="Request not found")
# #     if r.owner_id != current_user.id:
# #         raise HTTPException(status_code=403, detail="Not allowed")
# #     if r.expires_at <= time.time():
# #         r.status = "expired"
# #         db.commit()
# #         raise HTTPException(status_code=400, detail="Request expired")
# #     if r.status != "pending":
# #         raise HTTPException(status_code=400, detail=f"Bad status: {r.status}")

# #     secret = os.getenv("DELEGATION_JWT_SECRET", "")
# #     if not secret:
# #         raise HTTPException(status_code=500, detail="DELEGATION_JWT_SECRET not configured")

# #     delegation_token = create_delegation_token(
# #         secret=secret,
# #         payload={
# #             "owner_id": r.owner_id,
# #             "recipient_id": r.recipient_id,
# #             "service_url": r.service_url,
# #             "request_id": r.id,
# #         },
# #         ttl_seconds=DELEGATION_TTL_SECONDS,
# #     )

# #     # TODO: create a proper handoff session using your existing handoff store
# #     # For now we store a placeholder id
# #     r.status = "approved"
# #     r.delegation_token = delegation_token
# #     r.handoff_session_id = f"assisted-{r.id}-{int(time.time())}"
# #     db.commit()

# #     return AssistedApproveOut(
# #         request_id=r.id,
# #         status=r.status,
# #         delegation_token=delegation_token,
# #         handoff_url=f"/sharing/handoff/{r.handoff_session_id}",
# #         expires_at=r.expires_at,
# #     )


# # @router.get("/{request_id}/status", response_model=AssistedStatusOut)
# # def status(
# #     request_id: int,
# #     current_user: User = Depends(get_current_user),
# #     db: Session = Depends(get_db),
# # ):
# #     r = db.query(AssistedAccessRequest).filter(AssistedAccessRequest.id == request_id).first()
# #     if not r:
# #         raise HTTPException(status_code=404, detail="Request not found")

# #     if current_user.id not in (r.owner_id, r.recipient_id):
# #         raise HTTPException(status_code=403, detail="Not allowed")

# #     if r.expires_at <= time.time() and r.status in ("pending", "approved"):
# #         r.status = "expired"
# #         db.commit()

# #     handoff_payload = {
# #     "typ": "handoff",
# #     "rid": request_id,
# #     "recipient_id": r["recipient_id"],
# #     "service_url": r["service_url"],
# #     "jti": secrets.token_urlsafe(16),  # ✅ one-time id
# #     "exp": int(_now() + HANDOFF_TTL_SECONDS),
# #     "iat": int(_now()),
# # }
# # handoff_token = _sign_handoff(handoff_payload)