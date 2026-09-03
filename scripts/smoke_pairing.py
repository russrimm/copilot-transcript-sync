"""Ad-hoc smoke test: verify CopilotConversationPair() with a synthetic conversation.

The tenant's real transcripts are greeting-only sessions with no user-typed
messages, so the pairing logic is otherwise unexercised. This ingests one
synthetic transcript containing a real back-and-forth, checks the pairing, and
then deletes it again.

    python scripts/smoke_pairing.py --cluster <uri> --ingest <uri> --database <db>
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time
from datetime import datetime, timezone

from azure.identity import AzureCliCredential
from azure.kusto.data import KustoClient, KustoConnectionStringBuilder

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from copilot_transcript_sync.adx import AdxTranscriptSink  # noqa: E402
from copilot_transcript_sync.dataverse import TranscriptRow  # noqa: E402

SYNTHETIC_ENV_ID = "00000000-smoke-test-synthetic-0000"


def _activity(index: int, role: int, activity_type: str, text: str | None, epoch: int) -> dict:
    return {
        "id": f"act-{index}",
        "type": activity_type,
        "timestamp": epoch,
        "channelId": "directline",
        "from": {"id": "u1" if role == 1 else "b1", "role": role},
        "text": text,
    }


def _synthetic_row() -> TranscriptRow:
    base = 1787000000
    # role 1 = user, role 0 = agent (documented, and confirmed against real data).
    activities = [
        _activity(0, 0, "trace", None, base),
        _activity(1, 1, "message", "How do I reset my password?", base + 1),
        _activity(2, 0, "message", "I can help with that.", base + 2),
        _activity(3, 0, "message", "Go to the self-service portal.", base + 3),
        _activity(4, 1, "message", "Thanks, that worked", base + 10),
        _activity(5, 0, "message", "Glad to hear it.", base + 11),
    ]
    return TranscriptRow(
        environment_id=SYNTHETIC_ENV_ID,
        environment_name="SMOKE TEST (synthetic)",
        environment_url="https://synthetic.invalid",
        organization_id="synthetic",
        conversation_transcript_id="00000000-0000-0000-0000-00000000cafe",
        name="smokeconv_smokebot",
        conversation_id="smokeconv",
        bot_id="smokebot",
        bot_name="Smoke Bot",
        batch_id="0",
        aad_tenant_id="synthetic",
        schema_type="powervirtualagents",
        schema_version="1.0",
        conversation_start_time="2026-09-01T00:00:00Z",
        created_on="2026-09-01T00:00:00Z",
        modified_on="2026-09-01T00:00:00Z",
        version_number=1,
        metadata={"BotId": "smokebot", "BotName": "Smoke Bot", "BatchId": 0},
        content={"activities": activities},
        content_parse_error=None,
        ingested_at=datetime.now(timezone.utc).isoformat(),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cluster", required=True)
    parser.add_argument("--ingest", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--table", default="CopilotTranscriptRaw")
    parser.add_argument("--keep", action="store_true", help="Do not delete the synthetic row.")
    args = parser.parse_args()

    credential = AzureCliCredential()

    with AdxTranscriptSink(
        ingest_uri=args.ingest,
        database=args.database,
        table=args.table,
        credential=credential,
    ) as sink:
        sink.add(_synthetic_row())
        sink.flush()
    print("Ingested synthetic transcript; waiting for batching...")

    kcsb = KustoConnectionStringBuilder.with_azure_token_credential(
        args.cluster.rstrip("/"), credential=credential
    )

    exit_code = 1
    with KustoClient(kcsb) as client:
        for attempt in range(20):
            time.sleep(30)
            result = client.execute(
                args.database,
                f"CopilotTranscriptRaw | where EnvironmentId == '{SYNTHETIC_ENV_ID}' | count",
            ).primary_results[0]
            if next(iter(result))["Count"]:
                print(f"Synthetic row visible after {(attempt + 1) * 30}s.")
                break
        else:
            print("Synthetic row never became visible.", file=sys.stderr)
            return 1

        query = (
            f"CopilotConversationPair() "
            f"| where EnvironmentId == '{SYNTHETIC_ENV_ID}' "
            f"| project TurnNumber, UserPrompt, AgentResponse, ResponseCount, ResponseLatency "
            f"| order by TurnNumber asc"
        )
        table = client.execute(args.database, query).primary_results[0]
        rows = list(table)

        print("\n=== CopilotConversationPair() ===")
        for row in rows:
            print(
                f"turn {row['TurnNumber']}: "
                f"user={row['UserPrompt']!r} "
                f"agent={row['AgentResponse']!r} "
                f"responses={row['ResponseCount']} "
                f"latency={row['ResponseLatency']}"
            )

        expected_first = "How do I reset my password?"
        if len(rows) == 2 and rows[0]["UserPrompt"] == expected_first:
            # The first turn must merge BOTH agent replies that followed it.
            if rows[0]["ResponseCount"] == 2 and "self-service portal" in rows[0]["AgentResponse"]:
                print("\nPASS: prompts paired, consecutive agent replies merged.")
                exit_code = 0
            else:
                print("\nFAIL: agent replies were not merged correctly.", file=sys.stderr)
        else:
            print(f"\nFAIL: expected 2 pairs, got {len(rows)}.", file=sys.stderr)

        if not args.keep:
            print("\nRemoving synthetic data...")
            client.execute_mgmt(
                args.database,
                f".delete table {args.table} records <| "
                f"{args.table} | where EnvironmentId == '{SYNTHETIC_ENV_ID}'",
            )
            print("Removed.")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
