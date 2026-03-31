#!/usr/bin/env python3
import argparse
import json
from collections import Counter


def main() -> int:
    parser = argparse.ArgumentParser(description="Show success statistics for domain registrar history JSONL")
    parser.add_argument("history_file", help="Path to history JSONL file")
    args = parser.parse_args()

    attempts = 0
    attempt_success = 0
    skipped = 0
    account_success = 0
    account_failed = 0
    reasons = Counter()

    with open(args.history_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            kind = row.get("kind")
            if kind == "attempt":
                attempts += 1
                if row.get("success"):
                    attempt_success += 1
                if row.get("skip_mailbox"):
                    skipped += 1
                if not row.get("success") and row.get("error"):
                    reasons[str(row["error"])] += 1
            elif kind == "account_result":
                if row.get("success"):
                    account_success += 1
                else:
                    account_failed += 1

    processed = account_success + account_failed
    account_success_rate = (account_success / processed * 100.0) if processed else 0.0
    attempt_success_rate = (attempt_success / attempts * 100.0) if attempts else 0.0

    print(f"accounts_processed={processed}")
    print(f"accounts_success={account_success}")
    print(f"accounts_failed={account_failed}")
    print(f"account_success_rate={account_success_rate:.1f}%")
    print(f"attempts={attempts}")
    print(f"attempt_success={attempt_success}")
    print(f"attempt_success_rate={attempt_success_rate:.1f}%")
    print(f"skip_mailboxes={skipped}")
    if reasons:
        print("top_errors:")
        for reason, count in reasons.most_common(10):
            print(f"  {count}  {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
