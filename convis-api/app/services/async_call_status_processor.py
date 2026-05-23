"""
Async Call Status Processor
Non-blocking version using Motor async MongoDB driver
"""
from datetime import datetime
from typing import Optional

import logging

from app.config.async_database import AsyncDatabase
from app.services.async_campaign_dialer import async_campaign_dialer

logger = logging.getLogger(__name__)


async def process_call_status_async(
    call_sid: Optional[str],
    call_status: Optional[str],
    call_duration: Optional[str],
    lead_id: Optional[str],
    campaign_id: Optional[str],
    price: Optional[str] = None,
    price_unit: Optional[str] = None,
    answered_by: Optional[str] = None,
):
    """
    Update call attempt + call_log records and notify the dialer about completion.

    This is the non-blocking version that:
    1. Uses Motor async MongoDB driver
    2. Uses async campaign dialer
    3. Doesn't block the FastAPI event loop
    4. Updates BOTH call_attempts (campaign tracking) AND call_logs (dashboard).
       Without the call_logs update, the dashboard shows duration=0 and
       cost=$0 for every outbound call because the agent's _mark_call_completed
       only writes status=completed, not duration/cost.
    """
    if not call_sid or not call_status:
        raise ValueError("call_sid and call_status are required")

    logger.info(f"[ASYNC_PROCESSOR] Processing call status - SID: {call_sid}, Status: {call_status}, Lead: {lead_id}, Campaign: {campaign_id}")

    db = await AsyncDatabase.get_db()
    call_attempts_collection = db["call_attempts"]
    call_logs_collection = db["call_logs"]

    update_data = {
        "status": call_status,
        "updated_at": datetime.utcnow()
    }

    if call_duration:
        try:
            update_data["duration"] = int(call_duration)
        except ValueError:
            logger.warning("Invalid CallDuration '%s' for CallSid %s", call_duration, call_sid)

    # Twilio sends Price as a negative number (it's a debit on the account).
    # Store the absolute value as cost; keep PriceUnit (usually "USD") for clarity.
    if price:
        try:
            update_data["cost"] = abs(float(price))
            update_data["price"] = float(price)  # raw twilio value for audit
        except ValueError:
            logger.warning("Invalid Price '%s' for CallSid %s", price, call_sid)
    if price_unit:
        update_data["price_unit"] = price_unit
    if answered_by:
        update_data["answered_by"] = answered_by  # human / machine_start / fax / etc

    if call_status in ["completed", "busy", "no-answer", "failed", "canceled"]:
        update_data["ended_at"] = datetime.utcnow()
        logger.info(f"[ASYNC_PROCESSOR] Call ended - Status: {call_status}, SID: {call_sid}")

    # 1. Update call_attempts (existing — campaign tracking).
    await call_attempts_collection.update_one(
        {"call_sid": call_sid},
        {"$set": update_data},
        upsert=True
    )
    logger.info(f"[ASYNC_PROCESSOR] Updated call_attempts for SID: {call_sid}")

    # 2. Update call_logs (NEW — fills duration/cost/answered_by/etc for the
    # dashboard). No upsert: only update if the log row already exists (which
    # it does for any call placed via /api/outbound-calls).
    log_res = await call_logs_collection.update_one(
        {"call_sid": call_sid},
        {"$set": update_data},
    )
    logger.info(
        f"[ASYNC_PROCESSOR] Updated call_logs for SID: {call_sid} "
        f"(matched={log_res.matched_count}, modified={log_res.modified_count})"
    )

    # Handle call completion
    if call_status in ["completed", "busy", "no-answer", "failed", "canceled"]:
        # Lookup campaign_id from lead if missing
        if not campaign_id and lead_id:
            logger.warning(f"[ASYNC_PROCESSOR] campaignId missing, looking up from lead {lead_id}")
            try:
                from bson import ObjectId
                leads_collection = db["leads"]
                lead = await leads_collection.find_one({"_id": ObjectId(lead_id)})
                if lead and lead.get("campaign_id"):
                    campaign_id = str(lead["campaign_id"])
                    logger.info(f"[ASYNC_PROCESSOR] Found campaign_id: {campaign_id} from lead")
            except Exception as e:
                logger.error(f"[ASYNC_PROCESSOR] Failed to lookup campaign from lead: {e}")

        if lead_id and campaign_id:
            logger.info(f"[ASYNC_PROCESSOR] Triggering async handle_call_completed for Lead: {lead_id}, Campaign: {campaign_id}")
            await async_campaign_dialer.handle_call_completed(campaign_id, lead_id, call_status)
        else:
            logger.warning(f"[ASYNC_PROCESSOR] Skipping handle_call_completed - Status: {call_status}, Lead: {lead_id}, Campaign: {campaign_id}")
