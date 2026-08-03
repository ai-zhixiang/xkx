"""Scheduler - start background tasks"""
import asyncio
import logging

log = logging.getLogger("scheduler")

def start_scheduler():
    """Start background tasks"""
    try:
        from app.services.order_scheduler import start_scheduler as ss
        import asyncio
        asyncio.create_task(ss())
        log.info("Order scheduler started")
    except Exception as e:
        log.error("Scheduler failed: %s", e)
    print("[scheduler] started")
