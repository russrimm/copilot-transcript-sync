"""Apply the .kql schema files to the Azure Data Explorer database.

Usage:
    python scripts/deploy_kql.py --cluster https://<cluster>.<region>.kusto.windows.net \
                                 --database CopilotTranscripts

Authenticates with DefaultAzureCredential, so an ``az login`` session with Admin
on the database is enough. The Bicep template grants that to
``adxAdminPrincipalId``.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

from azure.identity import DefaultAzureCredential
from azure.kusto.data import KustoClient, KustoConnectionStringBuilder
from azure.kusto.data.exceptions import KustoServiceError

KQL_DIR = pathlib.Path(__file__).resolve().parent.parent / "kql"
FENCE = "```"


def split_commands(text: str) -> list[str]:
    """Split a .kql file into individual control commands.

    Control commands begin with '.' at column zero, but multi-line payloads (an
    ingestion mapping, a policy body) are wrapped in ``` fences and may
    themselves contain such lines. Track fence state so those stay intact.
    """
    commands: list[str] = []
    current: list[str] = []
    in_fence = False

    for line in text.splitlines():
        stripped = line.strip()

        if stripped.startswith(FENCE):
            in_fence = not in_fence
            current.append(line)
            continue

        if not in_fence:
            if stripped.startswith("//") and not current:
                continue
            if line.startswith(".") and current:
                commands.append("\n".join(current).strip())
                current = []

        current.append(line)

    if current:
        commands.append("\n".join(current).strip())

    return [c for c in commands if c and not all(l.strip().startswith("//") for l in c.splitlines())]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", required=True, help="ADX cluster query URI.")
    parser.add_argument("--database", required=True, help="Target database name.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the commands without executing them."
    )
    args = parser.parse_args()

    files = sorted(KQL_DIR.glob("*.kql"))
    if not files:
        print(f"No .kql files found in {KQL_DIR}", file=sys.stderr)
        return 1

    kcsb = KustoConnectionStringBuilder.with_azure_token_credential(
        args.cluster.rstrip("/"), credential=DefaultAzureCredential()
    )

    with KustoClient(kcsb) as client:
        for path in files:
            commands = split_commands(path.read_text(encoding="utf-8"))
            print(f"\n=== {path.name}: {len(commands)} command(s) ===")

            for index, command in enumerate(commands, start=1):
                headline = command.splitlines()[0][:100]
                if args.dry_run:
                    print(f"  [{index}] {headline}")
                    continue

                print(f"  [{index}] {headline}")
                try:
                    client.execute_mgmt(args.database, command)
                except KustoServiceError as exc:
                    print(f"      FAILED: {exc}", file=sys.stderr)
                    return 1

    print("\nSchema applied." if not args.dry_run else "\nDry run complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
