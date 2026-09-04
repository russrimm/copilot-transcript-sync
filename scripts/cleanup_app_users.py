"""Find, and optionally remove, the Dataverse application users this project created.

`addAppUser` provisions an application user bound to the app registration's client
ID in every eligible environment. Deleting the app registration orphans those
records but does not remove them, and the Power Platform admin center documents
only a manual, per-environment removal.

This script does it across the tenant. It authenticates with the Azure CLI
session, so run it as a Power Platform administrator.

    # report only
    python scripts/cleanup_app_users.py --client-id <client-id>

    # actually remove them
    python scripts/cleanup_app_users.py --client-id <client-id> --delete
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
    parser.add_argument("--client-id", required=True, help="App registration client ID.")
    parser.add_argument(
        "--delete", action="store_true", help="Disable and delete the users rather than report."
    )
    args = parser.parse_args()

    # process_timeout is raised because the 10-second default regularly times out
    # against a cold Azure CLI, surfacing as "Failed to invoke the Azure CLI".
    tokens = TokenProvider(AzureCliCredential(process_timeout=120))
    found = 0
    removed = 0

    with httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as client:
        environments = PowerPlatformAdminClient(client, tokens).list_environments()
        print(f"Checking {len(environments)} environments for application user "
              f"{args.client_id}\n")

        for environment in environments:
            label = environment.display_name[:32]
            try:
                headers = tokens.auth_header(dataverse_scope(environment.environment_url))
                headers.update({"Accept": "application/json", "OData-Version": "4.0"})

                url = (
                    f"{environment.api_base}/systemusers"
                    f"?$select=systemuserid,fullname,applicationid,isdisabled"
                    f"&$filter=applicationid eq {args.client_id}"
                )
                response = request_with_retry(client, "GET", url, headers=headers)
                if not response.is_success:
                    print(f"  {label:<34} HTTP {response.status_code}")
                    continue

                users = response.json().get("value") or []
                if not users:
                    print(f"  {label:<34} none")
                    continue

                for user in users:
                    found += 1
                    user_id = user["systemuserid"]
                    state = "disabled" if user.get("isdisabled") else "enabled"
                    print(f"  {label:<34} {user_id} ({state})")

                    if not args.delete:
                        continue

                    # Dataverse requires an application user to be disabled before
                    # it can be deleted.
                    request_with_retry(
                        client,
                        "PATCH",
                        f"{environment.api_base}/systemusers({user_id})",
                        headers={**headers, "Content-Type": "application/json"},
                        json_body={"isdisabled": True},
                    )
                    delete = request_with_retry(
                        client,
                        "DELETE",
                        f"{environment.api_base}/systemusers({user_id})",
                        headers=headers,
                    )
                    if delete.is_success:
                        removed += 1
                        print(f"  {'':<34} removed")
                    else:
                        print(
                            f"  {'':<34} could not delete: HTTP {delete.status_code} "
                            f"{delete.text[:120]}"
                        )
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup tool
                print(f"  {label:<34} error: {type(exc).__name__}: {str(exc)[:80]}")

    print(f"\n{found} application user(s) found, {removed} removed.")
    if found and not args.delete:
        print("Re-run with --delete to remove them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
