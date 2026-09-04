"""Remove any leftover smoke-test data from the transcript archive.

`.delete table records` removes rows from the source table, but a materialized
view keeps its already-materialized rows until it is rebuilt, so synthetic data
can linger in CopilotTranscript after it is gone from CopilotTranscriptRaw.

Do NOT use `.clear materialized-view data` to deal with that. Clearing empties
the view and it does not backfill: the view then only materializes ingestion
that arrives afterwards, so every historical row silently disappears from it.
Dropping and recreating with `backfill=true` is the safe route, which is what
this script does by asking you to re-run deploy_kql.py.

    python scripts/cleanup_smoke_data.py --cluster <uri> --database <db>
"""

from __future__ import annotations

import argparse

from azure.identity import AzureCliCredential
from azure.kusto.data import KustoClient, KustoConnectionStringBuilder

SMOKE_PREFIX = "SMOKE TEST"


def count(client: KustoClient, database: str, query: str) -> int:
    table = client.execute(database, query).primary_results[0]
    return next(iter(table))["Count"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cluster", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--table", default="CopilotTranscriptRaw")
    parser.add_argument("--view", default="CopilotTranscript")
    parser.add_argument(
        "--rebuild-view",
        action="store_true",
        help="Drop the materialized view so deploy_kql.py can recreate it with backfill=true.",
    )
    args = parser.parse_args()

    kcsb = KustoConnectionStringBuilder.with_azure_token_credential(
        args.cluster.rstrip("/"), credential=AzureCliCredential(process_timeout=120)
    )

    with KustoClient(kcsb) as client:
        raw_smoke = count(
            client,
            args.database,
            f"{args.table} | where EnvironmentName startswith '{SMOKE_PREFIX}' | count",
        )
        view_smoke = count(
            client,
            args.database,
            f"{args.view} | where EnvironmentName startswith '{SMOKE_PREFIX}' | count",
        )
        print(f"synthetic rows in {args.table}: {raw_smoke}")
        print(f"synthetic rows in {args.view}: {view_smoke}")

        if raw_smoke:
            client.execute_mgmt(
                args.database,
                f".delete table {args.table} records <| "
                f"{args.table} | where EnvironmentName startswith '{SMOKE_PREFIX}'",
            )
            print(f"deleted {raw_smoke} synthetic rows from {args.table}")

        if view_smoke:
            if args.rebuild_view:
                client.execute_mgmt(args.database, f".drop materialized-view {args.view}")
                print(
                    f"dropped {args.view}. Re-run scripts/deploy_kql.py to recreate it "
                    "with backfill=true."
                )
            else:
                print(
                    f"\n{view_smoke} synthetic row(s) remain in {args.view}. "
                    "Re-run with --rebuild-view to drop it, then run deploy_kql.py to "
                    "recreate it with backfill=true."
                )

        print()
        print(f"{args.table} total: {count(client, args.database, f'{args.table} | count')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

