#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import threading
import urllib.parse
import uuid
from collections import Counter
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List

from insight_engine import build_insight_snapshot, load_snapshot, save_snapshot
from main import collect_events, filter_events_by_keywords, load_config, run, write_report
from push_runner import load_subscriptions, parse_hhmm, run_subscription_push, save_subscriptions


LOCK = threading.Lock()


def latest_report_path(output_dir: Path) -> Path | None:
    candidates = sorted(output_dir.glob("radar_*.md"))
    return candidates[-1] if candidates else None


def subscriptions_html(rows: List[Dict]) -> str:
    if not rows:
        return '<div class="empty">暂无订阅，先创建你的第一个关键词订阅。</div>'

    cards = []
    for row in rows:
        name = html.escape(str(row.get("name", "未命名订阅")))
        keywords = html.escape(" / ".join(row.get("keywords", [])))
        send_time = html.escape(str(row.get("send_time", "08:30")))
        channel = html.escape(str(row.get("channel", "web")))
        receiver = html.escape(str(row.get("receiver", "网页收件箱")))
        last_push = html.escape(str(row.get("last_push_date", "-")))
        sub_id = html.escape(str(row.get("id", "")))
        cards.append(
            "<article class='sub-card'>"
            f"<h3>{name}</h3>"
            f"<p>关键词: {keywords}</p>"
            f"<p>推送: 每日 {send_time} | 渠道: {channel} | 接收: {receiver}</p>"
            f"<p>最近推送: {last_push}</p>"
            "<form method='post' action='/unsubscribe'>"
            f"<input type='hidden' name='id' value='{sub_id}' />"
            "<button class='btn ghost' type='submit'>删除</button>"
            "</form>"
            "</article>"
        )
    return "".join(cards)


def sources_overview_html(config_path: Path) -> str:
    cfg = load_config(config_path)
    all_sources = cfg["sources"].get("international", []) + cfg["sources"].get("domestic", [])
    total = len(all_sources)
    region = Counter(s.get("region", "unknown") for s in all_sources)
    channel = Counter(s.get("channel", "other") for s in all_sources)
    category = Counter(s.get("category", "other") for s in all_sources)

    top_category = "、".join(f"{k}:{v}" for k, v in category.most_common(6))
    channels = "".join(f"<span class='badge'>{html.escape(k)} {v}</span>" for k, v in channel.most_common())

    source_rows = []
    for src in all_sources:
        name = html.escape(str(src.get("name", "")))
        url = html.escape(str(src.get("url", "")))
        ch = html.escape(str(src.get("channel", "other")))
        cat = html.escape(str(src.get("category", "other")))
        rg = html.escape(str(src.get("region", "unknown")))
        source_rows.append(
            f"<tr><td>{name}</td><td>{rg}</td><td>{ch}</td><td>{cat}</td>"
            f"<td><a href='{url}' target='_blank' rel='noreferrer'>link</a></td></tr>"
        )

    return (
        "<div class='source-head'>"
        f"<div><strong>{total}</strong><span>总来源</span></div>"
        f"<div><strong>{region.get('global', 0)}</strong><span>国际源</span></div>"
        f"<div><strong>{region.get('cn', 0)}</strong><span>国内源</span></div>"
        "</div>"
        f"<p class='hint'>平台分布: {channels}</p>"
        f"<p class='hint'>类别Top: {html.escape(top_category)}</p>"
        "<div class='table-wrap'><table><thead><tr><th>来源</th><th>地区</th><th>平台</th><th>类别</th><th>URL</th></tr></thead>"
        f"<tbody>{''.join(source_rows)}</tbody></table></div>"
    )


def _trend_bars_html(snapshot: Dict) -> str:
    hourly = snapshot.get("hourly_distribution", [])[-12:]
    if not hourly:
        return "<div class='hint'>暂无时间序列数据。</div>"

    peak = max(int(item.get("count", 0)) for item in hourly) or 1
    bars = []
    for item in hourly:
        count = int(item.get("count", 0))
        slot = html.escape(str(item.get("slot", ""))[-5:])
        ratio = max(10, int(round((count / peak) * 100)))
        bars.append(
            "<div class='bar'>"
            f"<div class='bar-ink' style='height:{ratio}%'></div>"
            f"<span>{slot}</span><em>{count}</em>"
            "</div>"
        )
    return "<div class='bars'>" + "".join(bars) + "</div>"


def _hotspot_cards_html(snapshot: Dict) -> str:
    rows = snapshot.get("hotspot_insights", [])
    if not rows:
        return "<div class='empty'>暂无热点洞察，先执行一次24小时监测。</div>"

    cards = []
    for idx, item in enumerate(rows[:8], start=1):
        title = html.escape(str(item.get("title", "未知事件")))
        heat = int(item.get("heat_score", 0))
        source_count = int(item.get("source_count", 0))
        confidence = html.escape(str(item.get("confidence", "C")))
        impact = html.escape(str(item.get("impact", "")))
        tags = "".join(f"<span class='badge'>{html.escape(str(tag))}</span>" for tag in item.get("tags", []))
        points = "".join(f"<li>{html.escape(str(point))}</li>" for point in item.get("summary", [])[:2])

        evidence = []
        for ref in item.get("evidence", [])[:2]:
            source = html.escape(str(ref.get("source", "")))
            link = html.escape(str(ref.get("link", "")))
            evidence.append(f"<a href='{link}' target='_blank' rel='noreferrer'>{source}</a>")
        evidence_html = " | ".join(evidence) if evidence else "-"

        cards.append(
            "<article class='hot-card'>"
            f"<div class='hot-head'><strong>#{idx} {title}</strong><span>热度 {heat}</span></div>"
            f"<p class='hint'>可信度 {confidence} · 来源数 {source_count}</p>"
            f"<ul>{points}</ul>"
            f"<p class='hint'>行业影响：{impact}</p>"
            f"<p class='hint'>标签：{tags}</p>"
            f"<p class='hint'>证据：{evidence_html}</p>"
            "</article>"
        )
    return "<div class='hot-grid'>" + "".join(cards) + "</div>"


def _trace_cards_html(snapshot: Dict) -> str:
    rows = snapshot.get("trace_analysis", [])
    if not rows:
        return "<div class='empty'>暂无溯源链路数据。</div>"

    blocks = []
    for item in rows[:6]:
        title = html.escape(str(item.get("title", "")))
        origin = html.escape(str(item.get("origin_source", "-")))
        first_seen = html.escape(str(item.get("first_seen", "-")))
        latest = html.escape(str(item.get("latest_update", "-")))

        chain_rows = []
        for link in item.get("evidence_chain", [])[:5]:
            tm = html.escape(str(link.get("time", "-")))
            source = html.escape(str(link.get("source", "-")))
            channel = html.escape(str(link.get("channel", "other")))
            url = html.escape(str(link.get("link", "")))
            chain_rows.append(
                "<li>"
                f"<span>{tm}</span>"
                f"<span>{source} ({channel})</span>"
                f"<a href='{url}' target='_blank' rel='noreferrer'>查看</a>"
                "</li>"
            )
        chain_html = "".join(chain_rows) if chain_rows else "<li><span>-</span><span>暂无证据</span><span>-</span></li>"

        blocks.append(
            "<details class='trace-item'>"
            f"<summary>{title}</summary>"
            "<div class='trace-body'>"
            f"<p class='hint'>首发来源：{origin} | 首次出现：{first_seen} | 最近更新：{latest}</p>"
            f"<ul class='trace-chain'>{chain_html}</ul>"
            "</div>"
            "</details>"
        )
    return "".join(blocks)


def _recommend_cards_html(snapshot: Dict) -> str:
    rows = snapshot.get("topic_recommendations", [])
    if not rows:
        return "<div class='empty'>暂无选题推荐。</div>"

    cards = []
    for item in rows[:8]:
        rank = int(item.get("rank", 0))
        title = html.escape(str(item.get("title", "")))
        audience = html.escape(str(item.get("target_audience", "")))
        angle = html.escape(str(item.get("suggested_angle", "")))
        why_now = html.escape(str(item.get("why_now", "")))
        fmt = html.escape(str(item.get("content_format", "")))
        cards.append(
            "<article class='topic-card'>"
            f"<h3>#{rank} {title}</h3>"
            f"<p><strong>切入角度：</strong>{angle}</p>"
            f"<p><strong>目标读者：</strong>{audience}</p>"
            f"<p><strong>建议形态：</strong>{fmt}</p>"
            f"<p class='hint'>{why_now}</p>"
            "</article>"
        )
    return "<div class='topic-grid'>" + "".join(cards) + "</div>"


def insight_section_html(snapshot: Dict | None) -> str:
    if not snapshot:
        return "<div class='empty'>还没有24小时监测快照。点击“执行24小时监测”立即生成。</div>"

    kpis = snapshot.get("kpis", {})
    top_tags = snapshot.get("top_tags", [])
    generated_at = html.escape(str(snapshot.get("generated_at", "-")))
    keys = snapshot.get("keywords", [])
    key_line = "、".join(html.escape(str(k)) for k in keys) if keys else "全量关键词"

    tag_html = "".join(
        f"<span class='badge'>{html.escape(str(item.get('tag', '')))} {int(item.get('count', 0))}</span>" for item in top_tags[:8]
    ) or "<span class='hint'>暂无标签统计</span>"

    return (
        "<section class='panel' style='margin-top:14px;'>"
        "<h2>24小时热点洞察</h2>"
        f"<p class='hint'>快照时间：{generated_at} | 过滤关键词：{key_line}</p>"
        "<div class='kpi-grid'>"
        f"<div class='kpi'><strong>{int(kpis.get('total_collected_articles', 0))}</strong><span>抓取文章</span></div>"
        f"<div class='kpi'><strong>{int(kpis.get('filtered_articles', 0))}</strong><span>有效文章</span></div>"
        f"<div class='kpi'><strong>{int(kpis.get('hotspot_count', 0))}</strong><span>热点事件</span></div>"
        f"<div class='kpi'><strong>{int(kpis.get('avg_heat_score', 0))}</strong><span>平均热度</span></div>"
        "</div>"
        "<div class='sub-layout'>"
        "<div>"
        "<h3>热点强度趋势（最近12小时）</h3>"
        f"{_trend_bars_html(snapshot)}"
        f"<p class='hint'>热门标签：{tag_html}</p>"
        "</div>"
        "<div>"
        "<h3>热点洞察榜</h3>"
        f"{_hotspot_cards_html(snapshot)}"
        "</div>"
        "</div>"
        "</section>"
        "<section class='panel' style='margin-top:14px;'>"
        "<h2>溯源分析</h2>"
        f"{_trace_cards_html(snapshot)}"
        "</section>"
        "<section class='panel' style='margin-top:14px;'>"
        "<h2>选题推荐</h2>"
        f"{_recommend_cards_html(snapshot)}"
        "</section>"
    )


def page_html(
    message: str,
    latest_name: str,
    latest_content: str,
    rows: List[Dict],
    sources_html: str,
    snapshot: Dict | None,
) -> str:
    escaped_msg = html.escape(message)
    escaped_name = html.escape(latest_name)
    escaped_content = html.escape(latest_content)
    subs_html = subscriptions_html(rows)
    insight_html = insight_section_html(snapshot)
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>24小时热点资讯监测平台</title>
  <style>
    :root {{
      --bg: #f4f7ff;
      --ink: #14233d;
      --muted: #5f6f8a;
      --card: rgba(255, 255, 255, .82);
      --line: rgba(105, 128, 176, .24);
      --primary: #ff6b6b;
      --primary2: #ff9f43;
      --accent: #1fa2ff;
      --good: #0f9d58;
      --warn: #d7263d;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      font-family: "Space Grotesk", "Noto Sans SC", "PingFang SC", sans-serif;
      background:
        radial-gradient(980px 520px at 12% 0%, rgba(49, 194, 255, .22), transparent 58%),
        radial-gradient(900px 520px at 92% -5%, rgba(255, 124, 124, .2), transparent 60%),
        radial-gradient(850px 580px at 50% 110%, rgba(255, 201, 86, .18), transparent 55%),
        linear-gradient(165deg, #f6f9ff 0%, #fff8f2 46%, #f8fcff 100%);
      min-height: 100vh;
    }}
    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      background-image: radial-gradient(circle at 1px 1px, rgba(71, 99, 151, .08) 1px, transparent 0);
      background-size: 22px 22px;
      pointer-events: none;
      mask-image: radial-gradient(circle at center, black 48%, transparent 94%);
    }}
    body::after {{
      content: "";
      position: fixed;
      width: 360px;
      height: 360px;
      right: -100px;
      top: 120px;
      background: radial-gradient(circle at center, rgba(52, 178, 255, .18), transparent 65%);
      filter: blur(8px);
      pointer-events: none;
    }}
    .shell {{ max-width: 1280px; margin: 24px auto 36px; padding: 0 16px; position: relative; z-index: 1; }}
    .hero {{
      background:
        linear-gradient(125deg, rgba(255, 255, 255, .94), rgba(255, 255, 255, .82)),
        linear-gradient(90deg, rgba(31, 162, 255, .12), rgba(255, 159, 67, .1));
      border: 1px solid var(--line);
      border-radius: 24px;
      padding: 22px;
      box-shadow: 0 16px 42px rgba(42, 72, 126, .14), inset 0 1px 0 rgba(255, 255, 255, .86);
      animation: rise .65s ease-out;
    }}
    .title {{
      margin: 0;
      font-size: clamp(26px, 4vw, 42px);
      letter-spacing: .4px;
      background: linear-gradient(90deg, #1b3f77, #1fa2ff 45%, #ff7b5c);
      -webkit-background-clip: text;
      background-clip: text;
      color: transparent;
    }}
    .sub {{ color: var(--muted); margin-top: 6px; }}
    .statline {{ margin-top: 14px; display: flex; gap: 10px; flex-wrap: wrap; }}
    .pill {{
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, .74);
      color: #22406f;
      border-radius: 999px;
      padding: 7px 12px;
      font-size: 13px;
    }}
    .msg {{
      margin-top: 12px;
      color: #0d7e49;
      font-weight: 600;
      background: rgba(15, 157, 88, .1);
      border: 1px solid rgba(15, 157, 88, .25);
      border-radius: 12px;
      padding: 8px 10px;
      display: inline-block;
      max-width: 100%;
    }}
    .layout {{ margin-top: 16px; display: grid; grid-template-columns: 1.05fr .95fr; gap: 14px; }}
    .panel {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 16px;
      backdrop-filter: blur(9px);
      animation: rise .8s ease-out;
      box-shadow: 0 12px 30px rgba(54, 85, 138, .1);
    }}
    .panel:hover {{ box-shadow: 0 16px 34px rgba(54, 85, 138, .14); }}
    .panel h2 {{ margin: 0 0 10px; font-size: 18px; }}
    h3 {{ margin: 8px 0 8px; font-size: 16px; }}
    .hint {{ color: var(--muted); margin: 8px 0; font-size: 14px; line-height: 1.5; }}
    .row {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }}
    .btn {{
      border: 0;
      border-radius: 11px;
      padding: 10px 14px;
      color: #fff;
      background: linear-gradient(90deg, var(--primary), var(--primary2));
      font-weight: 700;
      cursor: pointer;
      box-shadow: 0 8px 16px rgba(255, 128, 96, .3);
      transition: transform .15s ease, filter .15s ease, box-shadow .15s ease;
    }}
    .btn:hover {{ transform: translateY(-1px); filter: saturate(115%); box-shadow: 0 10px 20px rgba(255, 128, 96, .4); }}
    .btn.ghost {{
      color: #b13d55;
      background: rgba(255, 130, 151, .12);
      border: 1px solid rgba(255, 130, 151, .45);
      box-shadow: none;
    }}
    input, select {{
      background: rgba(255, 255, 255, .8);
      color: #1a3156;
      border: 1px solid rgba(99, 131, 183, .3);
      border-radius: 10px;
      padding: 9px 10px;
      min-width: 120px;
      outline: none;
    }}
    input:focus, select:focus {{ border-color: var(--accent); box-shadow: 0 0 0 3px rgba(31, 162, 255, .16); }}
    .sub-grid {{ display: grid; gap: 10px; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); }}
    .sub-card {{ border: 1px solid var(--line); border-radius: 14px; padding: 12px; background: rgba(255, 255, 255, .72); }}
    .sub-card h3 {{ margin: 0 0 7px; font-size: 16px; }}
    .sub-card p {{ color: var(--muted); margin: 4px 0; font-size: 13px; line-height: 1.5; }}
    .empty {{
      color: var(--muted);
      border: 1px dashed var(--line);
      border-radius: 12px;
      padding: 14px;
      background: rgba(255, 255, 255, .45);
    }}
    .source-head {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin-bottom: 8px; }}
    .source-head > div {{ border: 1px solid var(--line); border-radius: 12px; padding: 10px; background: rgba(255, 255, 255, .76); }}
    .source-head strong {{ font-size: 20px; display: block; }}
    .source-head span {{ color: var(--muted); font-size: 12px; }}
    .badge {{
      display: inline-block;
      margin: 3px 5px 3px 0;
      padding: 4px 9px;
      border-radius: 999px;
      background: rgba(31, 162, 255, .14);
      border: 1px solid rgba(31, 162, 255, .34);
      color: #22558b;
      font-size: 12px;
    }}
    .table-wrap {{ overflow: auto; max-height: 280px; border: 1px solid var(--line); border-radius: 12px; }}
    table {{ width: 100%; border-collapse: collapse; min-width: 680px; }}
    th, td {{ padding: 8px 10px; border-bottom: 1px solid rgba(137, 167, 219, .2); text-align: left; font-size: 13px; color: #1f365d; }}
    th {{ position: sticky; top: 0; background: rgba(245, 250, 255, .95); }}
    a {{ color: #1465cc; text-decoration: none; }}
    .report {{
      white-space: pre-wrap;
      background: rgba(255, 255, 255, .86);
      border: 1px dashed var(--line);
      border-radius: 12px;
      padding: 12px;
      max-height: 420px;
      overflow: auto;
      line-height: 1.55;
      font-family: "IBM Plex Mono", "SFMono-Regular", monospace;
      font-size: 13px;
      color: #1e365c;
    }}
    .kpi-grid {{ display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); margin: 12px 0; }}
    .kpi {{
      border: 1px solid rgba(90, 127, 190, .24);
      border-radius: 12px;
      padding: 12px;
      background:
        linear-gradient(135deg, rgba(255, 255, 255, .9), rgba(245, 251, 255, .82)),
        linear-gradient(120deg, rgba(31, 162, 255, .08), rgba(255, 159, 67, .05));
    }}
    .kpi strong {{ display: block; font-size: 24px; }}
    .kpi span {{ color: #6a7b98; font-size: 12px; }}
    .sub-layout {{ display: grid; gap: 12px; grid-template-columns: .8fr 1.2fr; }}
    .bars {{
      display: grid;
      grid-template-columns: repeat(12, minmax(0, 1fr));
      gap: 6px;
      align-items: end;
      min-height: 170px;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 10px;
      background: rgba(255, 255, 255, .62);
    }}
    .bar {{ display: grid; gap: 4px; justify-items: center; }}
    .bar-ink {{ width: 100%; max-width: 16px; border-radius: 6px 6px 2px 2px; background: linear-gradient(180deg, #1fa2ff, #ff8f6f); min-height: 8px; }}
    .bar span {{ font-size: 11px; color: var(--muted); writing-mode: vertical-rl; transform: rotate(180deg); }}
    .bar em {{ font-style: normal; font-size: 11px; color: #244b86; }}
    .hot-grid {{ display: grid; gap: 8px; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }}
    .hot-card {{
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 10px;
      background:
        linear-gradient(145deg, rgba(255, 255, 255, .9), rgba(248, 252, 255, .8));
    }}
    .hot-head {{ display: flex; justify-content: space-between; gap: 8px; }}
    .hot-card ul {{ margin: 8px 0; padding-left: 18px; }}
    .hot-card li {{ color: #2a466f; font-size: 13px; margin: 3px 0; }}
    .trace-item {{
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 0 10px;
      margin: 8px 0;
      background: rgba(255, 255, 255, .75);
    }}
    .trace-item summary {{ cursor: pointer; padding: 10px 0; font-weight: 600; }}
    .trace-body {{ padding: 0 0 10px; }}
    .trace-chain {{ list-style: none; margin: 0; padding: 0; }}
    .trace-chain li {{ display: grid; gap: 8px; grid-template-columns: 90px 1fr 44px; border-bottom: 1px dashed rgba(137, 167, 219, .2); padding: 6px 0; font-size: 12px; }}
    .topic-grid {{ display: grid; gap: 9px; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); }}
    .topic-card {{
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 12px;
      background:
        linear-gradient(130deg, rgba(255, 255, 255, .92), rgba(255, 247, 241, .84));
    }}
    .topic-card h3 {{ margin: 0 0 8px; font-size: 15px; }}
    .topic-card p {{ margin: 6px 0; font-size: 13px; line-height: 1.5; }}
    @keyframes rise {{
      from {{ opacity: 0; transform: translateY(10px); }}
      to {{ opacity: 1; transform: translateY(0); }}
    }}
    @media (max-width: 980px) {{
      .layout {{ grid-template-columns: 1fr; }}
      .sub-layout {{ grid-template-columns: 1fr; }}
      .bars {{ grid-template-columns: repeat(6, minmax(0, 1fr)); }}
      .bar span {{ writing-mode: horizontal-tb; transform: none; }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <h1 class="title">24小时热点资讯监测平台</h1>
      <p class="sub">热点洞察、溯源分析、选题推荐一体化控制台。刷新时间: {now}</p>
      <div class="statline">
        <span class="pill">最新报告: {escaped_name}</span>
        <span class="pill"><a href="/report" target="_blank">打开 Markdown</a></span>
        <span class="pill"><a href="/insights" target="_blank">查看洞察快照 JSON</a></span>
      </div>
      <div class="msg">{escaped_msg}</div>
    </section>

    <section class="layout">
      <div class="panel">
        <h2>监测与订阅操作</h2>
        <form method="post" action="/monitor">
          <div class="row"><input name="keywords" placeholder="监测关键词（可空），如：AI Agent, GPU, SaaS" style="width:100%;" /></div>
          <div class="row">
            <label><input type="checkbox" name="use_sample" value="1" /> 样例模式(调试)</label>
            <button class="btn" type="submit">执行24小时监测</button>
          </div>
        </form>

        <div class="row" style="margin-top:12px;">
          <form method="post" action="/generate">
            <div class="row">
              <input type="text" name="keywords" placeholder="生成报告关键词（可空）" />
              <label><input type="checkbox" name="use_sample" value="1" /> 样例模式(调试)</label>
              <button class="btn" type="submit">生成日报</button>
            </div>
          </form>
        </div>

        <div class="row" style="margin-top:12px;">
          <form method="post" action="/run-push">
            <div class="row">
              <label><input type="checkbox" name="use_sample" value="1" /> 样例模式(调试)</label>
              <label><input type="checkbox" name="only_due" value="1" checked /> 仅推送到点订阅</label>
              <button class="btn" type="submit">执行每日推送</button>
            </div>
          </form>
        </div>

        <div class="row" style="margin-top:12px;">
          <form method="post" action="/subscribe" style="width:100%;">
            <div class="row"><input required name="name" placeholder="订阅名称，例如 全球AI投融资" style="width:100%;" /></div>
            <div class="row"><input required name="keywords" placeholder="关键词，逗号分隔：AI, GPU, Agent" style="width:100%;" /></div>
            <div class="row">
              <input name="send_time" value="08:30" placeholder="08:30" />
              <select name="channel"><option value="web">Web Inbox</option><option value="email">Email(占位)</option></select>
              <input name="receiver" value="网页收件箱" placeholder="接收地址" />
              <button class="btn" type="submit">创建订阅</button>
            </div>
          </form>
        </div>

        <p class="hint">生产环境请关闭样例模式，平台会抓取真实RSS并生成24小时热点快照。</p>
      </div>

      <div class="panel">
        <h2>数据源矩阵（国际 + 国内 + 社交）</h2>
        {sources_html}
      </div>
    </section>

    {insight_html}

    <section class="panel" style="margin-top:14px;">
      <h2>我的订阅</h2>
      <div class="sub-grid">{subs_html}</div>
    </section>

    <section class="panel" style="margin-top:14px;">
      <h2>最新报告预览</h2>
      <div class="report">{escaped_content}</div>
    </section>
  </div>
</body>
</html>
"""


class AppHandler(BaseHTTPRequestHandler):
    root = Path(__file__).resolve().parents[1]
    data_dir = Path(os.getenv("AI_AGENT_DATA_DIR", str(root / "data")))
    output_dir = Path(os.getenv("AI_AGENT_OUTPUT_DIR", str(root / "output")))
    config_path = root / "config" / "sources.json"
    subscriptions_path = data_dir / "subscriptions.json"
    subscriptions_output = output_dir / "subscriptions"
    insights_snapshot_path = output_dir / "insights" / "latest_snapshot.json"
    cron_token = os.getenv("AI_AGENT_CRON_TOKEN", "")

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/report"):
            self._serve_report()
            return
        if self.path.startswith("/insights"):
            self._serve_insights()
            return

        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        msg = params.get("msg", [""])[0]

        latest = latest_report_path(self.output_dir)
        latest_name = latest.name if latest else "暂无"
        latest_content = latest.read_text(encoding="utf-8") if latest else "还没有生成报告，请先执行监测或生成日报。"
        rows = load_subscriptions(self.subscriptions_path)
        sources_html = sources_overview_html(self.config_path)
        snapshot = load_snapshot(self.insights_snapshot_path)

        body = page_html(msg, latest_name, latest_content, rows, sources_html, snapshot).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.startswith("/monitor"):
            self._handle_monitor()
            return
        if self.path.startswith("/generate"):
            self._handle_generate()
            return
        if self.path.startswith("/subscribe"):
            self._handle_subscribe()
            return
        if self.path.startswith("/unsubscribe"):
            self._handle_unsubscribe()
            return
        if self.path.startswith("/run-push"):
            self._handle_run_push(web_mode=True)
            return
        if self.path.startswith("/api/run-push"):
            self._handle_api_run_push()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _read_form(self) -> Dict[str, List[str]]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8", errors="ignore") if length > 0 else ""
        return urllib.parse.parse_qs(raw)

    def _redirect_with_msg(self, msg: str) -> None:
        location = "/?msg=" + urllib.parse.quote(msg)
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def _handle_monitor(self) -> None:
        form = self._read_form()
        use_sample = form.get("use_sample", [""])[0] == "1"
        keywords = [x.strip() for x in form.get("keywords", [""])[0].split(",") if x.strip()]
        try:
            with LOCK:
                snapshot = build_insight_snapshot(
                    self.config_path,
                    use_sample=use_sample,
                    keywords=keywords,
                    window_hours=24,
                )
                save_snapshot(self.insights_snapshot_path, snapshot)
            hotspot_count = int(snapshot.get("kpis", {}).get("hotspot_count", 0))
            self._redirect_with_msg(f"24小时监测完成: 识别到 {hotspot_count} 个热点事件")
        except Exception as exc:
            self._redirect_with_msg(f"监测失败: {type(exc).__name__}: {exc}")

    def _handle_generate(self) -> None:
        form = self._read_form()
        use_sample = form.get("use_sample", [""])[0] == "1"
        keywords = [x.strip() for x in form.get("keywords", [""])[0].split(",") if x.strip()]

        try:
            with LOCK:
                if keywords:
                    events = collect_events(self.config_path, use_sample)
                    filtered = filter_events_by_keywords(events, keywords)
                    date_str = dt.datetime.now().strftime("%Y-%m-%d")
                    out = write_report(self.output_dir, date_str, filtered)
                else:
                    out = run(self.config_path, self.output_dir, use_sample)
            self._redirect_with_msg(f"报告生成成功: {out.name}")
        except Exception as exc:
            self._redirect_with_msg(f"报告生成失败: {type(exc).__name__}: {exc}")

    def _handle_subscribe(self) -> None:
        form = self._read_form()
        name = form.get("name", [""])[0].strip()
        keys_raw = form.get("keywords", [""])[0]
        send_time = parse_hhmm(form.get("send_time", ["08:30"])[0])
        channel = form.get("channel", ["web"])[0].strip() or "web"
        receiver = form.get("receiver", ["网页收件箱"])[0].strip() or "网页收件箱"
        keywords = [x.strip() for x in keys_raw.split(",") if x.strip()]

        if not name or not keywords:
            self._redirect_with_msg("订阅失败: 名称和关键词不能为空")
            return

        with LOCK:
            rows = load_subscriptions(self.subscriptions_path)
            rows.append(
                {
                    "id": uuid.uuid4().hex[:12],
                    "name": name,
                    "keywords": keywords,
                    "send_time": send_time,
                    "channel": channel,
                    "receiver": receiver,
                    "active": True,
                    "created_at": dt.datetime.now().isoformat(timespec="seconds"),
                    "last_push_date": "",
                }
            )
            save_subscriptions(self.subscriptions_path, rows)
        self._redirect_with_msg(f"订阅已创建: {name}")

    def _handle_unsubscribe(self) -> None:
        form = self._read_form()
        sub_id = form.get("id", [""])[0].strip()
        if not sub_id:
            self._redirect_with_msg("删除失败: 缺少订阅ID")
            return

        with LOCK:
            rows = load_subscriptions(self.subscriptions_path)
            new_rows = [r for r in rows if str(r.get("id")) != sub_id]
            if len(new_rows) == len(rows):
                self._redirect_with_msg("删除失败: 未找到该订阅")
                return
            save_subscriptions(self.subscriptions_path, new_rows)
        self._redirect_with_msg("订阅已删除")

    def _handle_run_push(self, web_mode: bool, use_sample: bool | None = None, only_due: bool | None = None) -> Dict[str, int] | None:
        if use_sample is None or only_due is None:
            form = self._read_form()
            use_sample = form.get("use_sample", [""])[0] == "1"
            only_due = form.get("only_due", [""])[0] == "1"

        try:
            with LOCK:
                result = run_subscription_push(
                    self.config_path,
                    self.subscriptions_path,
                    self.subscriptions_output,
                    use_sample=use_sample,
                    only_due=only_due,
                )
            if web_mode:
                self._redirect_with_msg(
                    f"推送完成: 成功{result['pushed']}个, 跳过{result['skipped']}个, 总订阅{result['total']}"
                )
                return None
            return result
        except Exception as exc:
            if web_mode:
                self._redirect_with_msg(f"推送失败: {type(exc).__name__}: {exc}")
                return None
            raise

    def _handle_api_run_push(self) -> None:
        token = self.headers.get("X-Run-Token", "")
        if self.cron_token and token != self.cron_token:
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.end_headers()
            return

        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        use_sample = qs.get("use_sample", ["0"])[0] == "1"
        only_due = qs.get("only_due", ["1"])[0] != "0"

        result = self._handle_run_push(web_mode=False, use_sample=use_sample, only_due=only_due)
        payload = json.dumps(result or {}, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _serve_report(self) -> None:
        latest = latest_report_path(self.output_dir)
        content = "暂无报告，请先在首页生成。" if latest is None else latest.read_text(encoding="utf-8")
        body = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_insights(self) -> None:
        snapshot = load_snapshot(self.insights_snapshot_path)
        payload = json.dumps(snapshot or {"message": "暂无洞察快照"}, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="24小时热点资讯监测平台 Web 控制台")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"), help="Bind host")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8080")), help="Bind port")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    print(f"Web console running: http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
