"""Ad-hoc probe: dump the real column names of a Dataverse table.

Written because guessing logical names from documentation is how extraction code
ends up silently selecting nothing.

    python scripts/probe_table_schema.py --table bot
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import httpx
from azure.identity import AzureCliCredential

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from copilot_transcript_sync.credentials import dataverse_scope  # noqa: E402
from copilot_transcript_sync.http import DEFAULT_TIMEOUT, TokenProvider, request_with_retry  # noqa: E402
from copilot_transcript_sync.powerplatform import PowerPlatformAdminClient  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--table", default="bot")
    parser.add_argument("--environment", help="Display name substring. Defaults to the first with rows.")
    parser.add_argument("--sample", action="store_true", help="Also print one row.")
    args = parser.parse_args()

    tokens = TokenProvider(AzureCliCredential(process_timeout=120))

    with httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as client:
        environments = PowerPlatformAdminClient(client, tokens).list_environments()
        if args.environment:
            environments = [
                e for e in environments
                if args.environment.casefold() in e.display_name.casefold()
            ]

        for environment in environments:
            headers = tokens.auth_header(dataverse_scope(environment.environment_url))
            headers.update({"Accept": "application/json", "OData-Version": "4.0"})

            meta = (
                f"{environment.api_base}/EntityDefinitions(LogicalName='{args.table}')"
                f"/Attributes?$select=LogicalName,AttributeType,IsValidForRead"
            )
            response = request_with_retry(client, "GET", meta, headers=headers)
            if not response.is_success:
                continue

            attributes = response.json().get("value") or []
            readable = sorted(
                (a["LogicalName"], a.get("AttributeType", "?"))
                for a in attributes
                if a.get("IsValidForRead")
            )

            print(f"=== {args.table} in '{environment.display_name}' ===")
            print(f"{len(readable)} readable columns\n")
            for name, kind in readable:
                print(f"  {name:<44} {kind}")

            if args.sample:
                rows = request_with_retry(
                    client,
                    "GET",
                    f"{environment.api_base}/{args.table}s?$top=1",
                    headers=headers,
                )
                if rows.is_success:
                    values = rows.json().get("value") or []
                    if values:
                        print("\n--- sample row (non-null fields) ---")
                        for key, value in sorted(values[0].items()):
                            if value not in (None, "") and not key.startswith("@"):
                                print(f"  {key:<44} {str(value)[:70]}")
            return 0

    print(f"Table '{args.table}' not readable in any environment.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
