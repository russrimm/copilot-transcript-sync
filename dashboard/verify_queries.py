"""Run every dashboard tile query against a live cluster.

Schema validation proves the file is well formed. It says nothing about whether
the queries work. This substitutes the dashboard parameters and executes each
tile, so a broken query is caught here rather than by a viewer looking at a
blank tile.

    python dashboard/verify_queries.py --cluster https://mycluster.eastus.kusto.windows.net
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_DASHBOARD = HERE / "CopilotStudioAnalytics.json"

# Dashboard parameters are substituted by the ADX web UI at render time. To run
# a tile outside the dashboard the same declarations have to be prepended.
PARAM_PRELUDE = """let _startTime = ago({lookback});
let _endTime = now();
let IncludeTestPane = {include_test_pane};
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dashboard", type=pathlib.Path, default=DEFAULT_DASHBOARD)
    parser.add_argument("--cluster", required=True)
    parser.add_argument("--database", default="CopilotTranscripts")
    parser.add_argument("--lookback", default="90d")
    args = parser.parse_args()

    try:
        from azure.identity import AzureCliCredential
        from azure.kusto.data import KustoClient, KustoConnectionStringBuilder
    except ImportError:
        print("azure-kusto-data is required: pip install -r requirements-dev.txt")
        return 2

    dashboard = json.loads(args.dashboard.read_text(encoding="utf-8"))
    pages = {p["id"]: p["name"] for p in dashboard["pages"]}

    client = KustoClient(
        KustoConnectionStringBuilder.with_azure_token_credential(
            args.cluster, AzureCliCredential(process_timeout=120)
        )
    )

    failures = 0
    for tile in dashboard["tiles"]:
        used = set(tile.get("usedParamVariables") or [])
        prelude = ""
        if used:
            prelude = PARAM_PRELUDE.format(
                lookback=args.lookback, include_test_pane="false"
            )
            # Only declare what the tile references, or Kusto rejects unused lets.
            keep = [
                line
                for line in prelude.splitlines()
                if any(f"let {v} " in line for v in used)
            ]
            prelude = "\n".join(keep) + "\n" if keep else ""

        page = pages[tile["pageId"]]
        label = f"{page}/{tile['title']}"
        try:
            result = client.execute(args.database, prelude + tile["query"])
            rows = len(result.primary_results[0])
            print(f"  OK   {label:46s} {rows:>5} rows")
        except Exception as exc:
            failures += 1
            message = str(exc).replace("\n", " ")[:180]
            print(f"  FAIL {label:46s} {message}")

    total = len(dashboard["tiles"])
    print(f"\n{total - failures}/{total} tiles returned data")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
