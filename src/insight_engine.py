#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
from collections import Counter
from email.utils import parsedate_to_datetime
from pathlib import Path
from statistics import mean
from typing import Dict, List, Tuple

from main import (
    Article,
    Event,
    collect_articles,
    contains_any,
    dedup_to_events,
    filter_articles,
    load_config,
    summarize_cluster,
)


def _local_now(now: dt.datetime | None) -> dt.datetime:
    if now is None:
        return dt.datetime.now().astimezone()
    if now.tzinfo is None:
        return now.astimezone()
    return now


def _parse_published(value: str, tz: dt.tzinfo) -> dt.datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None

    candidates = [raw]
    if raw.endswith("Z"):
        candidates.append(raw[:-1] + "+00:00")
    if "T" in raw and "+" not in raw and "-" not in raw[-6:]:
        candidates.append(raw + "+00:00")

    for text in candidates:
        try:
            parsed = dt.datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=tz)
            return parsed.astimezone(tz)
        except ValueError:
            pass

    try:
        parsed = parsedate_to_datetime(raw)
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        return parsed.astimezone(tz)
    except Exception:
        return None


def _source_meta(config: Dict) -> Dict[str, Dict]:
    rows = config["sources"].get("international", []) + config["sources"].get("domestic", [])
    return {str(r.get("name", "")): r for r in rows}


def _confidence_label(heat_score: int, source_count: int) -> str:
    if heat_score >= 80 and source_count >= 3:
        return "A"
    if heat_score >= 65 and source_count >= 2:
        return "B"
    return "C"


def _heat_score(event: Event, cluster: List[Article], now: dt.datetime, window_hours: int) -> int:
    latest = None
    for article in cluster:
        published = _parse_published(article.published, now.tzinfo or dt.timezone.utc)
        if published and (latest is None or published > latest):
            latest = published
    age_hours = window_hours if latest is None else max(0.0, (now - latest).total_seconds() / 3600.0)
    recency_ratio = max(0.0, 1.0 - min(age_hours, float(window_hours)) / float(window_hours))
    avg_weight = mean(a.source_weight for a in cluster) if cluster else 0.7
    score = (
        event.score * 16
        + min(5, len(cluster)) * 8
        + len({a.source for a in cluster}) * 8
        + avg_weight * 14
        + recency_ratio * 22
    )
    return max(1, min(100, int(round(score))))


def _trace_payload(title: str, cluster: List[Article], source_lookup: Dict[str, Dict], tz: dt.tzinfo) -> Dict:
    rows: List[Tuple[dt.datetime, Article]] = []
    for article in cluster:
        parsed = _parse_published(article.published, tz)
        if parsed:
            rows.append((parsed, article))

    rows.sort(key=lambda item: item[0])
    origin_source = rows[0][1].source if rows else cluster[0].source
    first_seen = rows[0][0].strftime("%Y-%m-%d %H:%M") if rows else "-"
    latest_update = rows[-1][0].strftime("%Y-%m-%d %H:%M") if rows else "-"

    channels = Counter(source_lookup.get(a.source, {}).get("channel", "other") for a in cluster)
    regions = Counter(source_lookup.get(a.source, {}).get("region", "unknown") for a in cluster)
    chain = []
    for when, article in rows[:6]:
        meta = source_lookup.get(article.source, {})
        chain.append(
            {
                "time": when.strftime("%m-%d %H:%M"),
                "source": article.source,
                "channel": meta.get("channel", "other"),
                "region": meta.get("region", "unknown"),
                "title": article.title,
                "link": article.link,
            }
        )
    if not chain:
        for article in cluster[:3]:
            meta = source_lookup.get(article.source, {})
            chain.append(
                {
                    "time": "-",
                    "source": article.source,
                    "channel": meta.get("channel", "other"),
                    "region": meta.get("region", "unknown"),
                    "title": article.title,
                    "link": article.link,
                }
            )

    return {
        "title": title,
        "origin_source": origin_source,
        "first_seen": first_seen,
        "latest_update": latest_update,
        "source_channels": dict(channels),
        "source_regions": dict(regions),
        "evidence_chain": chain,
    }


def _recommendation_angle(tags: List[str]) -> Tuple[str, str, str]:
    if "芯片" in tags:
        return ("算力成本与供给变化", "技术管理者/基础设施团队", "图解+数据快评")
    if "开源" in tags:
        return ("开源方案替代与选型清单", "开发者/架构师", "对比评测")
    if "云计算" in tags or "SaaS" in tags:
        return ("落地ROI与组织改造路径", "企业决策层/产品负责人", "深度解读")
    if "AI" in tags or "大模型" in tags:
        return ("业务场景化落地与护城河", "产品经理/业务负责人", "案例拆解")
    return ("行业机会与风险映射", "泛科技从业者", "周报专题")


def _topic_recommendations(hotspots: List[Dict], tag_counter: Counter) -> List[Dict]:
    recs: List[Dict] = []
    for idx, item in enumerate(hotspots[:6], start=1):
        angle, audience, fmt = _recommendation_angle(item.get("tags", []))
        evidence = item.get("evidence", [])[:2]
        recs.append(
            {
                "rank": idx,
                "title": f"{item['title']}：{angle}",
                "suggested_angle": angle,
                "target_audience": audience,
                "content_format": fmt,
                "why_now": f"热度分 {item['heat_score']}，由 {item['source_count']} 个来源共同驱动。",
                "evidence": evidence,
            }
        )

    if tag_counter and (tag_counter.get("AI", 0) + tag_counter.get("大模型", 0)) / max(sum(tag_counter.values()), 1) > 0.6:
        recs.append(
            {
                "rank": len(recs) + 1,
                "title": "AI 外溢影响专题：传统行业如何被重塑",
                "suggested_angle": "从供应链、客服、研发三个环节评估 AI 外溢价值。",
                "target_audience": "产业研究/战略团队",
                "content_format": "专题长文",
                "why_now": "当前热点集中在 AI，本选题可帮助拉开差异化。",
                "evidence": [],
            }
        )

    return recs


def build_insight_snapshot(
    config_path: Path,
    *,
    use_sample: bool = False,
    keywords: List[str] | None = None,
    window_hours: int = 24,
    now: dt.datetime | None = None,
) -> Dict:
    local_now = _local_now(now)
    cfg = load_config(config_path)
    source_lookup = _source_meta(cfg)
    keys = [k.strip() for k in (keywords or []) if k.strip()]

    raw_articles = collect_articles(config_path, use_sample=use_sample)
    filtered = filter_articles(
        raw_articles,
        include=cfg["filter"]["include_keywords"],
        exclude=cfg["filter"]["exclude_keywords"],
    )

    if keys:
        filtered = [a for a in filtered if contains_any(f"{a.title} {a.summary}", keys)]

    clusters = dedup_to_events(filtered)
    min_score = int(cfg["scoring"].get("min_push_score", 3))

    event_pairs: List[Tuple[Event, List[Article]]] = []
    for cluster in clusters:
        event = summarize_cluster(cluster, cfg["scoring"].get("source_weight_factor", 2.0))
        if event.score >= min_score:
            event_pairs.append((event, cluster))

    hotspot_insights: List[Dict] = []
    trace_analysis: List[Dict] = []
    tag_counter: Counter = Counter()

    for event, cluster in event_pairs:
        heat = _heat_score(event, cluster, local_now, window_hours)
        unique_sources = sorted({a.source for a in cluster})
        evidence = [
            {
                "source": a.source,
                "title": a.title,
                "link": a.link,
                "published": a.published or "-",
            }
            for a in sorted(cluster, key=lambda x: x.source_weight, reverse=True)[:4]
        ]
        tag_counter.update(event.tags)

        hotspot_insights.append(
            {
                "title": event.title,
                "heat_score": heat,
                "event_score": event.score,
                "summary": event.summary_points,
                "impact": event.impact,
                "tags": event.tags,
                "source_count": len(unique_sources),
                "sources": unique_sources,
                "confidence": _confidence_label(heat, len(unique_sources)),
                "evidence": evidence,
            }
        )
        trace_analysis.append(_trace_payload(event.title, cluster, source_lookup, local_now.tzinfo or dt.timezone.utc))

    hotspot_insights.sort(key=lambda item: item["heat_score"], reverse=True)
    trace_analysis.sort(
        key=lambda item: next((i for i, h in enumerate(hotspot_insights) if h["title"] == item["title"]), 999)
    )

    hourly_counts = Counter()
    in_window_articles = 0
    for article in filtered:
        published = _parse_published(article.published, local_now.tzinfo or dt.timezone.utc)
        if not published:
            continue
        hours_delta = (local_now - published).total_seconds() / 3600.0
        if 0 <= hours_delta <= window_hours:
            bucket = published.replace(minute=0, second=0, microsecond=0).strftime("%m-%d %H:00")
            hourly_counts[bucket] += 1
            in_window_articles += 1

    hourly_distribution = []
    for i in reversed(range(window_hours)):
        slot = (local_now - dt.timedelta(hours=i)).replace(minute=0, second=0, microsecond=0).strftime("%m-%d %H:00")
        hourly_distribution.append({"slot": slot, "count": hourly_counts.get(slot, 0)})

    avg_heat = int(round(mean(item["heat_score"] for item in hotspot_insights))) if hotspot_insights else 0
    cross_source = sum(1 for item in hotspot_insights if item["source_count"] >= 2)
    top_tags = [{"tag": k, "count": v} for k, v in tag_counter.most_common(8)]

    topic_recommendations = _topic_recommendations(hotspot_insights, tag_counter)

    return {
        "generated_at": local_now.strftime("%Y-%m-%d %H:%M:%S %z"),
        "window_hours": window_hours,
        "use_sample": use_sample,
        "keywords": keys,
        "kpis": {
            "total_collected_articles": len(raw_articles),
            "filtered_articles": len(filtered),
            "in_window_articles": in_window_articles,
            "hotspot_count": len(hotspot_insights),
            "cross_source_hotspots": cross_source,
            "avg_heat_score": avg_heat,
        },
        "top_tags": top_tags,
        "hourly_distribution": hourly_distribution,
        "hotspot_insights": hotspot_insights[:12],
        "trace_analysis": trace_analysis[:10],
        "topic_recommendations": topic_recommendations,
    }


def save_snapshot(snapshot_path: Path, snapshot: Dict) -> None:
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")


def load_snapshot(snapshot_path: Path) -> Dict | None:
    if not snapshot_path.exists():
        return None
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None
