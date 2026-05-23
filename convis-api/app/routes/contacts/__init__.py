"""Contacts route package — per-tenant durable identities for the
conversation-memory feature, plus the right-to-be-forgotten opt-out."""
from .contacts import router as contacts_router

__all__ = ["contacts_router"]
