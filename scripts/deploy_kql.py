"""Apply the .kql schema files to the Azure Data Explorer database.

Usage:
    python scripts/deploy_kql.py --cluster https://<cluster>.<region>.kusto.windows.net \
                                 --database CopilotTranscripts

Authenticates with the Azure CLI session by default, because that is what the
Bicep template grants Admin to. Note that DefaultAzureCredential is deliberately
not the default here: if AZURE_CLIENT_ID and AZURE_CLIENT_SECRET happen to be set
in the environment, EnvironmentCredential silently wins and you authenticate as
that service principal instead of yourself. Pass --credential default if you
actually want that behavior.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

from azure.identity import AzureCliCredential, DefaultAzureCredential
from azure.kusto.data import KustoClient, KustoConnectionStringBuilder
from azure.kusto.data.exceptions import KustoServiceError

KQL_DIR = pathlib.Path(__file__).resolve().parent.parent / "kql"
FENCE = "```"


def split_commands(text: str) -> list[str]:
    """Split a .kql file into individual control commands.

    Control commands begin with '.' at column zero, but multi-line payloads (an
    ingestion mapping, a policy body) are wrapped in ``` fences and may
    themselves contain such lines. Track fence state so those stay intact.

    Comment blocks that sit between commands belong to the command that follows,
    not the one that precedes. Appending them to the previous command breaks
    anything whose body ends in a brace, such as a materialized view.
    """
    commands: list[str] = []
    current: list[str] = []
    in_fence = False

    def is_noise(line: str) -> bool:
        stripped = line.strip()
        return not stripped or stripped.startswith("//")

    def has_content() -> bool:
        return any(not is_noise(line) for line in current)

    def finalize() -> None:
        # Drop trailing blank and comment lines; they introduce the next command.
        while current and is_noise(current[-1]):
            current.pop()
        if current:
            block = "\n".join(current).strip()
            if block:
                commands.append(block)
        current.clear()

    for line in text.splitlines():
        stripped = line.strip()

        if stripped.startswith(FENCE):
            in_fence = not in_fence
            current.append(line)
            continue

        if not in_fence:
            if line.startswith(".") and has_content():
                finalize()
            if not current and is_noise(line):
                continue

        current.append(line)

    finalize()
    return commands


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", required=True, help="ADX cluster query URI.")
    parser.add_argument("--database", required=True, help="Target database name.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the commands without executing them."
    )
    parser.add_argument(
        "--credential",
        choices=["cli", "default"],
        default="cli",
        help="Which credential to use. 'cli' (default) uses the az login session. "
        "'default' uses DefaultAzureCredential, which may resolve to a service "
        "principal if AZURE_CLIENT_ID/AZURE_CLIENT_SECRET are set.",
    )
    args = parser.parse_args()

    files = sorted(KQL_DIR.glob("*.kql"))
    if not files:
        print(f"No .kql files found in {KQL_DIR}", file=sys.stderr)
        return 1

    credential = AzureCliCredential() if args.credential == "cli" else DefaultAzureCredential()

    kcsb = KustoConnectionStringBuilder.with_azure_token_credential(
        args.cluster.rstrip("/"), credential=credential
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
