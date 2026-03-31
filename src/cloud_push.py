#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import urllib.request
from pathlib import Path

from push_runner import run_subscription_push


def notify_webhook(webhook_url: str, text: str) -> None:
    data = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10):
        return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run cloud subscription push job")
    parser.add_argument("--use-sample", action="store_true", help="Use sample data instead of live RSS")
    parser.add_argument("--no-only-due", action="store_true", help="Push all active subscriptions regardless of time")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = Path(__file__).resolve().parents[1]
    data_dir = Path(os.getenv("AI_AGENT_DATA_DIR", str(root / "data")))
    output_dir = Path(os.getenv("AI_AGENT_OUTPUT_DIR", str(root / "output")))

    config_path = root / "config" / "sources.json"
    subscriptions_path = data_dir / "subscriptions.json"
    subscriptions_output = output_dir / "subscriptions"

    result = run_subscription_push(
        config_path,
        subscriptions_path,
        subscriptions_output,
        use_sample=args.use_sample,
        only_due=not args.no_only_due,
    )

    summary = (
        "AI Hotspot push done: "
        f"pushed={result['pushed']}, skipped={result['skipped']}, total={result['total']}"
    )
    print(summary)

    webhook_url = os.getenv("AI_AGENT_NOTIFY_WEBHOOK", "").strip()
    if webhook_url:
        try:
            notify_webhook(webhook_url, summary)
        except Exception as exc:
            print(f"Webhook notify failed: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
