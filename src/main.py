#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple


@dataclass
class Article:
    source: str
    lang: str
    source_weight: float
    title: str
    summary: str
    link: str
    published: str


@dataclass
class Event:
    title: str
    summary_points: List[str]
    impact: str
    score: int
    links: List[Tuple[str, str]]
    tags: List[str]


def load_config(config_path: Path) -> Dict:
    return json.loads(config_path.read_text(encoding="utf-8"))


def fetch_url(url: str, timeout: int = 12) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "AIHotspotAgent/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def parse_rss_or_atom(xml_text: str, source_conf: Dict) -> List[Article]:
    articles: List[Article] = []
    root = ET.fromstring(xml_text)

    for item in root.findall(".//channel/item"):
        title = text_of(item, "title")
        summary = clean_html(text_of(item, "description"))
        link = text_of(item, "link")
        published = text_of(item, "pubDate")
        if title and link:
            articles.append(
                Article(
                    source=source_conf["name"],
                    lang=source_conf.get("lang", "en"),
                    source_weight=float(source_conf.get("weight", 0.7)),
                    title=title,
                    summary=summary,
                    link=link,
                    published=published,
                )
            )

    atom_entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")
    for entry in atom_entries:
        title = text_of(entry, "{http://www.w3.org/2005/Atom}title")
        summary = clean_html(
            text_of(entry, "{http://www.w3.org/2005/Atom}summary")
            or text_of(entry, "{http://www.w3.org/2005/Atom}content")
        )
        link = ""
        link_el = entry.find("{http://www.w3.org/2005/Atom}link")
        if link_el is not None:
            link = (link_el.attrib.get("href") or "").strip()
        published = (
            text_of(entry, "{http://www.w3.org/2005/Atom}published")
            or text_of(entry, "{http://www.w3.org/2005/Atom}updated")
        )
        if title and link:
            articles.append(
                Article(
                    source=source_conf["name"],
                    lang=source_conf.get("lang", "en"),
                    source_weight=float(source_conf.get("weight", 0.7)),
                    title=title,
                    summary=summary,
                    link=link,
                    published=published,
                )
            )
    return articles[:50]


def text_of(parent: ET.Element, tag: str) -> str:
    node = parent.find(tag)
    return (node.text or "").strip() if node is not None and node.text else ""


def clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def contains_any(text: str, keywords: List[str]) -> bool:
    t = text.lower()
    return any(k.lower() in t for k in keywords)


def filter_articles(articles: List[Article], include: List[str], exclude: List[str]) -> List[Article]:
    out: List[Article] = []
    for a in articles:
        combined = f"{a.title} {a.summary}"
        if contains_any(combined, exclude):
            continue
        if not contains_any(combined, include):
            continue
        out.append(a)
    return out


def normalize_for_key(text: str) -> str:
    t = text.lower()
    t = re.sub(r"[^\w\u4e00-\u9fff\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def make_cluster_key(article: Article) -> str:
    base = normalize_for_key(article.title)
    tokens = [x for x in base.split(" ") if len(x) > 2][:8]
    joined = " ".join(tokens)
    return hashlib.md5(joined.encode("utf-8")).hexdigest()


def dedup_to_events(articles: List[Article]) -> List[List[Article]]:
    clusters: Dict[str, List[Article]] = {}
    for a in articles:
        key = make_cluster_key(a)
        clusters.setdefault(key, []).append(a)
    return list(clusters.values())


def zh_title_if_needed(article: Article) -> str:
    if article.lang == "zh":
        return article.title
    return f"[EN] {article.title}"


def summarize_cluster(cluster: List[Article], source_weight_factor: float) -> Event:
    primary = sorted(cluster, key=lambda x: x.source_weight, reverse=True)[0]
    title = zh_title_if_needed(primary)

    points: List[str] = []
    if primary.summary:
        points.append(shorten(primary.summary, 80))
    if len(cluster) > 1:
        points.append(f"多源交叉报道（共{len(cluster)}篇），可信度提升。")
    points.append(f"来源：{primary.source}。")
    points = points[:3]

    tags = infer_tags(primary.title + " " + primary.summary)
    disruption = 5 if any(t in tags for t in ["AI", "大模型", "芯片"]) else 3
    industry_impact = 4 if any(t in tags for t in ["云计算", "SaaS", "开源"]) else 3
    source_score = min(5, int(round(primary.source_weight * source_weight_factor + 2)))
    recency = 4
    score_raw = 0.35 * disruption + 0.30 * industry_impact + 0.20 * source_score + 0.15 * recency
    score = max(1, min(5, int(round(score_raw))))

    impact = impact_line(tags)
    links = [(c.title, c.link) for c in cluster[:3]]
    return Event(title=title, summary_points=points, impact=impact, score=score, links=links, tags=tags)


def shorten(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def infer_tags(text: str) -> List[str]:
    t = text.lower()
    mapping = {
        "AI": ["ai", "人工智能", "machine learning", "llm", "agent", "openai", "anthropic", "deepmind"],
        "大模型": ["llm", "large language model", "foundation model", "大模型"],
        "芯片": ["chip", "gpu", "nvidia", "芯片"],
        "云计算": ["cloud", "云", "aws", "azure", "gcp"],
        "SaaS": ["saas", "软件即服务"],
        "开源": ["open source", "github", "开源"],
        "软件开发": ["dev", "developer", "编程", "软件开发", "framework"],
    }
    tags: List[str] = []
    for tag, keys in mapping.items():
        if any(k in t for k in keys):
            tags.append(tag)
    return tags or ["科技"]


def impact_line(tags: List[str]) -> str:
    if "AI" in tags or "大模型" in tags:
        return "该事件可能加速 AI 应用落地与行业竞争重排。"
    if "芯片" in tags:
        return "该动态将影响算力成本与模型训练效率。"
    if "开源" in tags:
        return "开源生态活跃，可能降低技术试错与落地门槛。"
    return "该事件对科技产业有持续参考价值。"


def classify(events: List[Event]) -> Tuple[List[Event], List[Event], List[Event], List[Event]]:
    sorted_events = sorted(events, key=lambda x: x.score, reverse=True)
    top_news = sorted_events[:2]
    ai_watch = [e for e in sorted_events if any(t in e.tags for t in ["AI", "大模型"])][:4]
    software_it = [e for e in sorted_events if any(t in e.tags for t in ["软件开发", "云计算", "SaaS"])][:4]
    bonus = [e for e in sorted_events if "开源" in e.tags][:1]
    return top_news, ai_watch, software_it, bonus


def render_report(date_str: str, top_news: List[Event], ai_watch: List[Event], software_it: List[Event], bonus: List[Event]) -> str:
    lines = []
    lines.append(f"# 📅 【科技雷达】每日速递 ({date_str})")
    lines.append("")
    lines.append("## 🌟 【头条重磅】(Top News)")
    lines.extend(render_section(top_news))
    lines.append("")
    lines.append("## 🤖 【AI 前沿风向】(AI Watch)")
    lines.extend(render_section(ai_watch))
    lines.append("")
    lines.append("## 💻 【软件与信息化】(Software & IT)")
    lines.extend(render_section(software_it))
    lines.append("")
    lines.append("## 🛠 【开源/工具推荐】(Bonus)")
    lines.extend(render_section(bonus))
    lines.append("")
    return "\n".join(lines)


def render_section(events: List[Event]) -> List[str]:
    if not events:
        return ["- 今日暂无符合条件的高价值资讯。"]
    out: List[str] = []
    for e in events:
        out.append(f"- 事件：{e.title}（评分：{'⭐' * e.score}）")
        for p in e.summary_points:
            out.append(f"  - {p}")
        out.append(f"  - 行业影响：{e.impact}")
        t, l = e.links[0]
        out.append(f"  - 🔗 [{t}]({l})")
    return out


def load_sample(sample_path: Path) -> List[Article]:
    raw = json.loads(sample_path.read_text(encoding="utf-8"))
    out: List[Article] = []
    for item in raw:
        out.append(
            Article(
                source=item["source"],
                lang=item["lang"],
                source_weight=float(item["source_weight"]),
                title=item["title"],
                summary=item["summary"],
                link=item["link"],
                published=item.get("published", ""),
            )
        )
    return out


def collect_articles(config: Path, use_sample: bool) -> List[Article]:
    cfg = load_config(config)
    sources = cfg["sources"]["international"] + cfg["sources"]["domestic"]

    articles: List[Article] = []
    if use_sample:
        sample_path = config.parent.parent / "data" / "sample_articles.json"
        articles = load_sample(sample_path)
    else:
        for s in sources:
            if s.get("type") != "rss":
                continue
            try:
                xml_text = fetch_url(s["url"])
                articles.extend(parse_rss_or_atom(xml_text, s))
            except Exception:
                continue
    return articles


def collect_events(config: Path, use_sample: bool) -> List[Event]:
    cfg = load_config(config)
    articles = collect_articles(config, use_sample)

    filtered = filter_articles(
        articles,
        include=cfg["filter"]["include_keywords"],
        exclude=cfg["filter"]["exclude_keywords"],
    )
    clusters = dedup_to_events(filtered)
    events = [summarize_cluster(c, cfg["scoring"].get("source_weight_factor", 2.0)) for c in clusters]

    min_score = int(cfg["scoring"].get("min_push_score", 3))
    return [e for e in events if e.score >= min_score]


def filter_events_by_keywords(events: List[Event], keywords: List[str]) -> List[Event]:
    keys = [k.strip().lower() for k in keywords if k.strip()]
    if not keys:
        return sorted(events, key=lambda x: x.score, reverse=True)

    matched: List[Event] = []
    for e in events:
        combined = " ".join([e.title] + e.summary_points + e.tags).lower()
        if any(k in combined for k in keys):
            matched.append(e)
    return sorted(matched, key=lambda x: x.score, reverse=True)


def report_content_from_events(date_str: str, events: List[Event]) -> str:
    top_news, ai_watch, software_it, bonus = classify(events)
    return render_report(date_str, top_news, ai_watch, software_it, bonus)


def write_report(output_dir: Path, date_str: str, events: List[Event], filename: str | None = None) -> Path:
    content = report_content_from_events(date_str, events)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_name = filename or f"radar_{date_str}.md"
    out_path = output_dir / out_name
    out_path.write_text(content, encoding="utf-8")
    return out_path


def run(config: Path, output_dir: Path, use_sample: bool) -> Path:
    events = collect_events(config, use_sample)
    date_str = dt.datetime.now().strftime("%Y-%m-%d")
    return write_report(output_dir, date_str, events)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI Hotspot Agent Daily Reporter")
    parser.add_argument("--config", default="config/sources.json", help="Path to JSON config file")
    parser.add_argument("--output-dir", default="output", help="Directory to write daily report")
    parser.add_argument("--use-sample", action="store_true", help="Use built-in sample data for offline run")
    parser.add_argument("--keywords", default="", help="Comma separated keywords to filter events")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    project_root = Path(__file__).resolve().parents[1]
    config_path = (project_root / args.config).resolve()
    output_dir = (project_root / args.output_dir).resolve()

    events = collect_events(config_path, args.use_sample)
    if args.keywords.strip():
        keys = [x.strip() for x in args.keywords.split(",")]
        events = filter_events_by_keywords(events, keys)

    date_str = dt.datetime.now().strftime("%Y-%m-%d")
    report_path = write_report(output_dir, date_str, events)
    print(f"Generated: {report_path}")


if __name__ == "__main__":
    main()
