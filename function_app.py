"""Azure Functions entry points."""

from __future__ import annotations

import json
import logging
import os
import sys

import azure.functions as func

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from copilot_transcript_sync.settings import Settings  # noqa: E402
from copilot_transcript_sync.sync import run_sync  # noqa: E402

logger = logging.getLogger("copilot_transcript_sync")

app = func.FunctionApp()

# Default: every 30 minutes. Transcripts land in Dataverse up to 30 minutes after
# a conversation ends, so a tighter schedule adds request pressure without adding
# freshness.
SCHEDULE = os.environ.get("SYNC_SCHEDULE", "0 */30 * * * *")


@app.function_name(name="TranscriptSyncTimer")
@app.timer_trigger(schedule=SCHEDULE, arg_name="timer", run_on_startup=False)
def transcript_sync_timer(timer: func.TimerRequest) -> None:
    """Scheduled tenant-wide transcript sync."""
    if timer.past_due:
        logger.warning("Timer is past due; starting sync immediately.")

    result = run_sync(Settings.from_environment())
    logger.info(
        "Sync finished: %d environments synced, %d failed, %d rows ingested.",
        result.environments_synced,
        result.environments_failed,
        result.rows_ingested,
    )


@app.function_name(name="TranscriptSyncManual")
@app.route(route="sync", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def transcript_sync_manual(req: func.HttpRequest) -> func.HttpResponse:
    """Run the sync on demand. Useful for the initial backfill and for testing."""
    try:
        result = run_sync(Settings.from_environment())
    except Exception as exc:  # surfaced to the caller rather than swallowed
        logger.exception("Manual sync failed.")
        return func.HttpResponse(
            json.dumps({"status": "error", "message": str(exc)}),
            status_code=500,
            mimetype="application/json",
        )

    return func.HttpResponse(
        json.dumps(result.as_dict(), indent=2),
        status_code=200,
        mimetype="application/json",
    )
