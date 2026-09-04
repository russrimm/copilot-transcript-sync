"""Ad-hoc probe: do Developer (and other excluded) environments actually hold transcripts?

Microsoft documents that transcripts are not written for Developer environments,
but that is worth testing rather than trusting. Queries every environment the
signed-in user can reach, including the ones the sync currently skips, and
reports the actual conversationtranscript row count.

Authenticates as the signed-in user, so it does not depend on application users
existing in the skipped environments.

    python scripts/probe_all_environments.py
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import httpx
from azure.identity import AzureCliCredential

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from copilot_transcript_sync.credentials import dataverse_scope  # noqa: E402
from copilot_transcript_sync.http import DEFAULT_TIMEOUT, TokenProvider, request_with_retry  # noqa: E402
from copilot_transcript_sync.powerplatform import PowerPlatformAdminClient  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30, help="Lookback window.")
    args = parser.parse_args()

    tokens = TokenProvider(AzureCliCredential(process_timeout=120))
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"Probing every environment for transcripts created in the last {args.days} days.\n")
    print(f"{'ENVIRONMENT':<34} {'TYPE':<12} {'TRANSCRIPTS':>11}  NOTE")
    print("-" * 96)

    with httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as client:
        environments = PowerPlatformAdminClient(client, tokens).list_environments()

        totals: dict[str, int] = {}
        for environment in environments:
            label = environment.display_name[:32]
            env_type = environment.environment_type or "?"
            note = ""
            count: int | str = "-"

            try:
                headers = tokens.auth_header(dataverse_scope(environment.environment_url))
                headers.update({"Accept": "application/json", "OData-Version": "4.0"})

                url = (
                    f"{environment.api_base}/conversationtranscripts"
                    f"?$select=conversationtranscriptid"
                    f"&$filter=createdon gt {since}"
                    f"&$count=true&$top=1"
                )
                response = request_with_retry(client, "GET", url, headers=headers)

                if response.is_success:
                    payload = response.json()
                    count = int(payload.get("@odata.count", 0))
                    totals[env_type] = totals.get(env_type, 0) + count
                elif response.status_code in (401, 403):
                    note = "no access"
                elif response.status_code == 404:
                    note = "table not present"
                else:
                    note = f"HTTP {response.status_code}: {response.text[:70]}"
            except Exception as exc:  # noqa: BLE001 - probe tool
                note = f"{type(exc).__name__}"

            print(f"{label:<34} {env_type:<12} {str(count):>11}  {note}")

    print("\nTotals by environment type:")
    for env_type, total in sorted(totals.items(), key=lambda kv: -kv[1]):
        print(f"  {env_type:<14} {total:>6} transcripts")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
