"""Ad-hoc smoke test: list every environment the app can see, app-only.

Not part of the test suite -- this one talks to the real tenant.

    python scripts/smoke_discovery.py
"""

from __future__ import annotations

import logging
import pathlib
import sys

import httpx
from azure.identity import DefaultAzureCredential

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from copilot_transcript_sync.http import DEFAULT_TIMEOUT, TokenProvider  # noqa: E402
from copilot_transcript_sync.powerplatform import (  # noqa: E402
    PowerPlatformAdminClient,
    is_transcript_eligible,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

EXCLUDED = frozenset()  # nothing excluded: Developer environments do hold transcripts


def main() -> int:
    tokens = TokenProvider(DefaultAzureCredential(process_timeout=120))

    with httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as client:
        admin = PowerPlatformAdminClient(client, tokens)
        environments = admin.list_environments()

    print()
    print(f"{'NAME':<34} {'TYPE':<10} {'STATE':<8} {'ELIGIBLE':<9} URL")
    print("-" * 120)
    for environment in environments:
        eligible = is_transcript_eligible(environment, EXCLUDED)
        print(
            f"{environment.display_name[:33]:<34} "
            f"{environment.environment_type:<10} "
            f"{environment.state:<8} "
            f"{str(eligible):<9} "
            f"{environment.environment_url}"
        )
    print()
    print(f"{len(environments)} with Dataverse, "
          f"{sum(1 for e in environments if is_transcript_eligible(e, EXCLUDED))} eligible.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
