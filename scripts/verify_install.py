"""Verify an installation: schema present, rows landed, nothing failing.

Exits non-zero when something is wrong, so install.ps1 can fail loudly.

    python scripts/verify_install.py --cluster <uri> --database CopilotTranscripts
"""

from __future__ import annotations

import argparse
import sys
import time

from azure.identity import AzureCliCredential
from azure.kusto.data import KustoClient, KustoConnectionStringBuilder

REQUIRED_FUNCTIONS = {
    "CopilotTranscriptTurn",
    "CopilotConversationPair",
    "CopilotTranscriptCoverage",
}


def query(client: KustoClient, database: str, text: str, attempts: int = 5):
    """Run a query, retrying the transient TLS resets these endpoints sometimes throw."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return client.execute(database, text).primary_results[0]
        except Exception as exc:  # noqa: BLE001 - retried below
            last = exc
            if attempt < attempts - 1:
                time.sleep(6)
    raise RuntimeError(f"Query failed after {attempts} attempts: {last}")


def scalar(client: KustoClient, database: str, text: str):
    table = query(client, database, text)
    for row in table:
        return list(row)[0]
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cluster", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Do not treat zero transcripts as a failure. Useful in a tenant with no traffic.",
    )
    args = parser.parse_args()

    kcsb = KustoConnectionStringBuilder.with_azure_token_credential(
        args.cluster.rstrip("/"),
        # The 10-second default regularly times out against a cold Azure CLI.
        credential=AzureCliCredential(process_timeout=120),
    )

    problems: list[str] = []
    warnings: list[str] = []

    with KustoClient(kcsb) as client:
        # --- schema ---
        tables = {r["TableName"] for r in query(client, args.database, ".show tables")}
        if "CopilotTranscriptRaw" not in tables:
            problems.append("Table CopilotTranscriptRaw is missing. Re-run deploy_kql.py.")
        print(f"  tables            : {len(tables)} present")

        views = {r["Name"] for r in query(client, args.database, ".show materialized-views")}
        if "CopilotTranscript" not in views:
            problems.append("Materialized view CopilotTranscript is missing.")
        print(f"  materialized views: {', '.join(sorted(views)) or 'none'}")

        functions = {r["Name"] for r in query(client, args.database, ".show functions")}
        missing = REQUIRED_FUNCTIONS - functions
        if missing:
            problems.append(f"Missing query functions: {', '.join(sorted(missing))}")
        print(f"  functions         : {len(functions)} present")

        if problems:
            for problem in problems:
                print(f"\nFAIL: {problem}", file=sys.stderr)
            return 1

        # --- data ---
        raw = scalar(client, args.database, "CopilotTranscriptRaw | count")
        deduped = scalar(client, args.database, "CopilotTranscript | count")
        parse_errors = scalar(
            client,
            args.database,
            "CopilotTranscriptRaw | where isnotempty(ContentParseError) | count",
        )
        turns = scalar(client, args.database, "CopilotTranscriptTurn() | count")

        print(f"  raw rows          : {raw}")
        print(f"  deduplicated      : {deduped}")
        print(f"  turns expanded    : {turns}")
        print(f"  parse errors      : {parse_errors}")

        failures = scalar(
            client,
            args.database,
            ".show ingestion failures | where FailedOn > ago(2h) | count",
        )
        print(f"  ingestion failures: {failures}")

        if parse_errors:
            problems.append(f"{parse_errors} transcript(s) failed to parse.")
        if failures:
            problems.append(f"{failures} ingestion failure(s) in the last 2 hours.")

        if raw == 0:
            message = (
                "No transcripts ingested. Either no agent traffic exists in the last 30 days, "
                "transcript saving is disabled, or ingestion is still batching."
            )
            (warnings if args.allow_empty else problems).append(message)
        elif deduped == 0:
            problems.append(
                "Rows landed but the materialized view is empty. It may still be "
                "materializing, or it was cleared without a backfill."
            )
        elif turns == 0:
            warnings.append(
                "Transcripts present but no activities expanded. Check Content encoding."
            )

        # --- coverage ---
        coverage = query(client, args.database, "CopilotTranscriptCoverage(30d)")
        rows = list(coverage)
        if rows:
            print("\n  Per-environment coverage:")
            for row in rows:
                print(
                    f"    {str(row['EnvironmentName'])[:30]:<32} "
                    f"{row['Transcripts']:>5} transcripts, "
                    f"{row['Agents']} agent(s)"
                )

    for warning in warnings:
        print(f"\nWARNING: {warning}")

    if problems:
        for problem in problems:
            print(f"\nFAIL: {problem}", file=sys.stderr)
        return 1

    print("\n  Verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
