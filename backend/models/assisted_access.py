from __future__ import annotations

import time
from sqlalchemy import Column, Integer, String, Float, ForeignKey, Text
from sqlalchemy.orm import relationship

from backend.models.database import Base


class AssistedAccessRequest(Base):
    __tablename__ = "assisted_access_requests"

    id = Column(Integer, primary_key=True, index=True)

    owner_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    recipient_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)

    share_token_hash = Column(String(128), index=True, nullable=False)
    service_url = Column(Text, nullable=False)

    status = Column(String(32), index=True, nullable=False, default="pending")  # pending|approved|rejected|completed|expired
    created_at = Column(Float, nullable=False, default=lambda: time.time())
    expires_at = Column(Float, nullable=False)

    # set on approve
    delegation_token = Column(Text, nullable=True)
    handoff_session_id = Column(String(128), nullable=True)

    owner = relationship("User", foreign_keys=[owner_id])
    recipient = relationship("User", foreign_keys=[recipient_id])