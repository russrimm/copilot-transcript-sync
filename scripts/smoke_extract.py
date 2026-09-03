"""Ad-hoc smoke test: extract real transcripts and ingest them into ADX.

Not part of the test suite -- this talks to the real tenant and a real cluster.
Uses an in-memory watermark so it can run where the watermark storage account is
not reachable.

    python scripts/smoke_extract.py --cluster <uri> --ingest <uri> --database <db>
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import httpx
from azure.identity import AzureCliCredential, DefaultAzureCredential

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from copilot_transcript_sync.adx import AdxTranscriptSink  # noqa: E402
from copilot_transcript_sync.dataverse import DataverseTranscriptReader  # noqa: E402
from copilot_transcript_sync.http import DEFAULT_TIMEOUT, TokenProvider  # noqa: E402
from copilot_transcript_sync.powerplatform import (  # noqa: E402
    PowerPlatformAdminClient,
    is_transcript_eligible,
)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

EXCLUDED = frozenset({"developer", "teams"})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ingest", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--table", default="CopilotTranscriptRaw")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--provision", action="store_true", help="Call addAppUser first.")
    parser.add_argument("--no-ingest", action="store_true", help="Read only; skip ADX.")
    args = parser.parse_args()

    # Power Platform: whatever DefaultAzureCredential resolves to.
    # ADX: the az login session, which the Bicep grants Admin.
    pp_tokens = TokenProvider(DefaultAzureCredential())
    since = datetime.now(timezone.utc) - timedelta(days=args.days)

    totals: list[tuple[str, str, int, str]] = []

    with httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as client:
        admin = PowerPlatformAdminClient(client, pp_tokens)
        environments = [
            e for e in admin.list_environments() if is_transcript_eligible(e, EXCLUDED)
        ]
        print(f"{len(environments)} eligible environments\n")

        sink = None
        if not args.no_ingest:
            sink = AdxTranscriptSink(
                ingest_uri=args.ingest,
                database=args.database,
                table=args.table,
                credential=AzureCliCredential(),
            )

        try:
            for environment in environments:
                if args.provision:
                    admin.ensure_application_user(
                        environment.environment_id,
                        DefaultAzureCredential()
                        and __import__("os").environ.get("AZURE_CLIENT_ID", ""),
                    )

                reader = DataverseTranscriptReader(client, pp_tokens, environment, 25)
                count = 0
                status = "ok"
                try:
                    for row in reader.read_since(since):
                        count += 1
                        if sink is not None:
                            sink.add(row)
                except PermissionError as exc:
                    status = f"denied ({str(exc)[:60]}...)"
                except Exception as exc:  # noqa: BLE001 - smoke test
                    status = f"error: {type(exc).__name__}: {str(exc)[:80]}"

                totals.append((environment.display_name, environment.environment_type, count, status))
                print(f"  {environment.display_name[:32]:<34} {count:>5} transcripts   {status}")

            if sink is not None:
                sink.flush()
                print(f"\nQueued {sink.rows_ingested} rows for ingestion.")
        finally:
            if sink is not None:
                sink.close()

    print(f"\nTotal transcripts read: {sum(t[2] for t in totals)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
