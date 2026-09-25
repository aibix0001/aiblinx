"""APScheduler setup. Jobs are coroutine functions run on the asyncio loop."""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import Settings, get_settings
from .pipeline.cycle import run_cycle, run_poll


def build_scheduler(settings: Settings | None = None) -> AsyncIOScheduler:
    settings = settings or get_settings()
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        run_poll,
        CronTrigger.from_crontab(settings.poll_cron),
        id="poll",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        run_cycle,
        CronTrigger.from_crontab(settings.cycle_cron),
        id="cycle",
        max_instances=1,
        coalesce=True,
    )
    return scheduler
