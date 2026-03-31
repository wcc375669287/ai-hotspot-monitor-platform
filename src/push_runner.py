#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Dict, List

from main import collect_events, filter_events_by_keywords, write_report


def load_subscriptions(file_path: Path) -> List[Dict]:
    if not file_path.exists():
        return []
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def save_subscriptions(file_path: Path, rows: List[Dict]) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def safe_slug(text: str) -> str:
    raw = "".join(ch if ch.isalnum() else "-" for ch in text.lower()).strip("-")
    while "--" in raw:
        raw = raw.replace("--", "-")
    return raw[:32] or "sub"


def parse_hhmm(value: str) -> str:
    value = value.strip()
    parts = value.split(":")
    if len(parts) != 2:
        return "08:30"
    try:
        hh = int(parts[0])
        mm = int(parts[1])
    except ValueError:
        return "08:30"
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        return "08:30"
    return f"{hh:02d}:{mm:02d}"


def is_due_today(row: Dict, now: dt.datetime) -> bool:
    if not row.get("active", True):
        return False
    send_time = parse_hhmm(str(row.get("send_time", "08:30")))
    hh, mm = send_time.split(":")
    threshold = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    last_push_date = str(row.get("last_push_date", ""))
    today = now.strftime("%Y-%m-%d")
    return now >= threshold and last_push_date != today


def run_subscription_push(
    config_path: Path,
    subscriptions_path: Path,
    subscriptions_output: Path,
    *,
    use_sample: bool = False,
    only_due: bool = True,
    now: dt.datetime | None = None,
) -> Dict[str, int]:
    now = now or dt.datetime.now()
    today = now.strftime("%Y-%m-%d")
    pushed = 0
    skipped = 0

    rows = load_subscriptions(subscriptions_path)
    if not rows:
        return {"pushed": 0, "skipped": 0, "total": 0}

    events = collect_events(config_path, use_sample=use_sample)

    for row in rows:
        if not row.get("active", True):
            skipped += 1
            continue
        if only_due and not is_due_today(row, now):
            skipped += 1
            continue

        filtered = filter_events_by_keywords(events, row.get("keywords", []))
        sub_name = str(row.get("name", "sub"))
        sub_slug = safe_slug(sub_name)
        sub_id = str(row.get("id", "sub"))
        filename = f"radar_{today}_{sub_slug}_{sub_id}.md"
        write_report(subscriptions_output, today, filtered, filename=filename)

        row["last_push_date"] = today
        row["last_push_at"] = now.isoformat(timespec="seconds")
        pushed += 1

    save_subscriptions(subscriptions_path, rows)
    return {"pushed": pushed, "skipped": skipped, "total": len(rows)}
