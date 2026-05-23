from fastapi import APIRouter, Depends, Request, Form, HTTPException
from typing import Optional
import logging

from app.services.async_call_status_processor import process_call_status_async
from app.utils.twilio_signature import verify_twilio_signature

logger = logging.getLogger(__name__)

# Every endpoint here is a Twilio webhook — verify signature on all routes.
router = APIRouter(dependencies=[Depends(verify_twilio_signature)])


@router.api_route("/webhooks/twilio/calls", methods=["GET", "POST"])
async def universal_twilio_call_webhook(
    request: Request,
    CallSid: Optional[str] = Form(None),
    CallStatus: Optional[str] = Form(None),
    CallDuration: Optional[str] = Form(None),
    Price: Optional[str] = Form(None),
    PriceUnit: Optional[str] = Form(None),
    AnsweredBy: Optional[str] = Form(None),
    leadId: Optional[str] = Form(None),
    campaignId: Optional[str] = Form(None)
):
    """
    Public-facing webhook endpoint that Twilio can call directly.

    This mirrors /api/twilio-webhooks/call-status but exposes a simplified path
    that matches the product specification (`/webhooks/twilio/calls`).

    OPTIMIZED: Now uses async MongoDB operations for better latency.

    Receives Price/PriceUnit/AnsweredBy on terminal events so the dashboard
    can show real per-call cost + answered-by-human-vs-machine signal.
    """
    try:
        if not CallSid:
            CallSid = request.query_params.get("CallSid")
            CallStatus = request.query_params.get("CallStatus", CallStatus)
            CallDuration = request.query_params.get("CallDuration", CallDuration)
            Price = request.query_params.get("Price", Price)
            PriceUnit = request.query_params.get("PriceUnit", PriceUnit)
            AnsweredBy = request.query_params.get("AnsweredBy", AnsweredBy)
            leadId = request.query_params.get("leadId", leadId)
            campaignId = request.query_params.get("campaignId", campaignId)

        if not CallSid or not CallStatus:
            raise HTTPException(status_code=400, detail="CallSid and CallStatus are required")

        logger.info(
            "Webhook /webhooks/twilio/calls received status=%s sid=%s lead=%s campaign=%s price=%s",
            CallStatus,
            CallSid,
            leadId,
            campaignId,
            Price,
        )

        # Use async processor for non-blocking DB operations
        await process_call_status_async(
            CallSid, CallStatus, CallDuration, leadId, campaignId,
            price=Price, price_unit=PriceUnit, answered_by=AnsweredBy,
        )
        return {"message": "Status processed"}

    except HTTPException:
        raise
    except Exception as error:
        logger.error("Error handling /webhooks/twilio/calls: %s", error)
        raise HTTPException(status_code=500, detail="Failed to process call status")
