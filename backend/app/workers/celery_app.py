from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery = Celery("openvisor", broker=settings.redis_url, backend=settings.redis_url)
celery.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    task_track_started=True,
    imports=["app.workers.tasks", "app.workers.programs", "app.workers.hub"],
    beat_schedule={
        "demo-timeout-sweep": {
            "task": "app.workers.tasks.demo_timeout_sweep",
            "schedule": 60.0,  # every minute (§17)
        },
        "hub-heartbeat": {
            "task": "app.workers.hub.hub_heartbeat",
            "schedule": 60.0,  # every minute; instant no-op when no hub is configured
        },
        "hub-usage-report": {
            "task": "app.workers.hub.hub_usage_report",
            "schedule": 300.0,  # every 5 minutes; streams credit events to the hub
        },
        "hub-project-events-report": {
            "task": "app.workers.hub.hub_project_events_report",
            "schedule": 60.0,  # every minute (§pass-through P1): drain the project-event outbox
        },
        "program-schedule-sweep": {
            "task": "app.workers.programs.program_schedule_sweep",
            "schedule": 60.0,  # every minute (§28): due schedules + stale-run reaper
        },
        "routine-sweep": {
            "task": "app.workers.tasks.routine_sweep",
            "schedule": 60.0,  # every minute (§routines): fire due saved prompts
        },
        "auto-dev-issue-sweep": {
            "task": "app.workers.tasks.auto_dev_issue_sweep",
            "schedule": 60.0,
        },
        "dev-pr-sweep": {
            "task": "app.workers.tasks.dev_pr_sweep",
            # every minute: the §delivery reconciler tick - every in-progress
            # request's change observed on its repository and advanced (merge,
            # deploy, CI fix, park with the real cause)
            "schedule": 60.0,
        },
        "dev-run-reaper": {
            "task": "app.workers.tasks.dev_run_reaper",
            "schedule": 60.0,  # every minute (§14.x): recover dev runs a dead worker orphaned
        },
        "cve-refresh": {
            "task": "app.workers.tasks.cve_refresh",
            "schedule": crontab(hour=3, minute=0),  # daily (§14.7)
        },
        "knowledge-refresh": {
            "task": "app.workers.tasks.ingest_knowledge",
            # every 5 min - the tree fingerprint skips the re-embed when nothing
            # changed, so a quiet tick only costs the git-source refresh (§14.3)
            "schedule": 300.0,
        },
    },
)
