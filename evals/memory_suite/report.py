"""Reporting: a self-contained HTML scorecard, a JSON dump, and a terminal
summary. The HTML is the 'visual results' artifact — open it in a browser.
"""

import html
import json
from os import path


def _pct(v):
    return "—" if v is None else f"{round(100 * v)}%"


def _bar(v):
    """A 0..1 value as a colored cell style."""
    if v is None:
        return "background:#333;color:#888", "—"
    pct = round(100 * v)
    if v >= 0.9:
        color = "#1f7a3d"
    elif v >= 0.7:
        color = "#8a7d18"
    else:
        color = "#8a2418"
    return f"background:{color};color:#fff", f"{pct}%"


def _chip(passed, label):
    bg = "#1f7a3d" if passed else "#8a2418"
    return (f'<span style="display:inline-block;margin:2px;padding:2px 8px;'
            f'border-radius:10px;background:{bg};color:#fff;font-size:12px">'
            f'{html.escape(label)}</span>')


def _scenario_block(s):
    e = s.get("extraction") or {}
    style, label = _bar(e.get("score"))
    parts = [f'<div style="margin:14px 0;padding:12px;border:1px solid #333;'
             f'border-radius:8px;background:#1a1a1a">']
    parts.append(
        f'<h3 style="margin:0 0 6px">{html.escape(s["title"])} '
        f'<span style="font-size:12px;color:#888">[{html.escape(s["category"])}]</span></h3>')

    # extraction
    parts.append(
        f'<div style="margin:6px 0"><b>Extraction</b> '
        f'<span style="padding:1px 8px;border-radius:6px;{style}">{label}</span> '
        f'<span style="color:#888;font-size:12px">recall {_pct(e.get("recall"))} · '
        f'precision {_pct(e.get("precision"))} · {e.get("fact_count", 0)} facts · '
        f'{e.get("samples", 1)} sample(s)</span></div>')
    if e.get("captured"):
        parts.append('<div>' + ''.join(_chip(True, c) for c in e["captured"]) + '</div>')
    if e.get("missed"):
        parts.append('<div>' + ''.join(_chip(False, "missed: " + c) for c in e["missed"]) + '</div>')
    for fh in e.get("forbidden_hits", []):
        parts.append('<div>' + _chip(False, f'false-positive: {fh["forbidden"]}') + '</div>')
    for dv in e.get("dedup_violations", []):
        parts.append('<div>' + _chip(False, f'dup x{dv["count"]}: {dv["concept"]}') + '</div>')
    if e.get("facts"):
        facts_html = '<br>'.join('• ' + html.escape(f) for f in e["facts"])
        parts.append(f'<details><summary style="cursor:pointer;color:#888;font-size:12px">'
                     f'stored facts</summary><div style="font-size:13px;color:#ccc;'
                     f'margin:4px 0">{facts_html}</div></details>')
    if e.get("summary"):
        parts.append(f'<details><summary style="cursor:pointer;color:#888;font-size:12px">'
                     f'session summary</summary><div style="font-size:13px;color:#ccc;'
                     f'margin:4px 0">{html.escape(e["summary"])}</div></details>')

    # recall
    if s.get("recall"):
        parts.append('<div style="margin-top:8px"><b>Recall</b></div>')
        for p in s["recall"]:
            label = p["query"]
            if p["missing"]:
                label += "  ✗ missing " + ",".join(p["missing"])
            if p["leaked"]:
                label += "  ✗ leaked " + ",".join(p["leaked"])
            parts.append(_chip(p["passed"], label))

    # forget / edits
    if s.get("forget"):
        parts.append('<div style="margin-top:8px"><b>Forget</b></div>')
        for p in s["forget"]:
            label = p["query"]
            if p["still_present"]:
                label += "  ✗ still " + ",".join(p["still_present"])
            parts.append(_chip(p["passed"], label))
    if s.get("edits"):
        parts.append('<div style="margin-top:8px"><b>Edit</b></div>')
        for p in s["edits"]:
            label = p["recall_query"]
            if p["missing"]:
                label += "  ✗ missing " + ",".join(p["missing"])
            if p["leaked"]:
                label += "  ✗ stale " + ",".join(p["leaked"])
            parts.append(_chip(p["passed"], label))

    # greeting
    if s.get("greeting"):
        g = s["greeting"]
        parts.append('<div style="margin-top:8px"><b>Greeting</b> ' +
                     _chip(g["score"] >= 1.0, " ".join(g["problems"]) or "ok") + '</div>')
        parts.append(f'<div style="font-size:13px;color:#ccc;font-style:italic">'
                     f'"{html.escape(g["text"])}"</div>')

    parts.append('</div>')
    return ''.join(parts)


def _run_section(run):
    sm = run["summary"]
    head = (f'<h2 style="margin:24px 0 4px">Profile: {html.escape(run["profile"])}</h2>'
            f'<div style="color:#888;font-size:13px">{html.escape(run.get("profile_description",""))} '
            f'· n_ctx {run.get("n_ctx")} · {run.get("samples")} sample(s)</div>')
    cards = [
        ("Extraction", _pct(sm.get("extraction_score"))),
        ("Ext. recall", _pct(sm.get("extraction_recall"))),
        ("Ext. precision", _pct(sm.get("extraction_precision"))),
        ("Recall probes", sm.get("recall_probes")),
        ("Forget", sm.get("forget_probes")),
        ("Edit", sm.get("edit_probes")),
    ]
    card_html = ''.join(
        f'<div style="display:inline-block;margin:6px;padding:10px 14px;'
        f'border:1px solid #333;border-radius:8px;background:#1a1a1a;text-align:center">'
        f'<div style="font-size:20px;font-weight:700">{html.escape(str(v))}</div>'
        f'<div style="font-size:11px;color:#888">{html.escape(k)}</div></div>'
        for k, v in cards)
    scenarios = ''.join(_scenario_block(s) for s in run["scenarios"])
    return head + '<div style="margin:10px 0">' + card_html + '</div>' + scenarios


def build_html(runs, title="Persistent Memory — Test Suite") -> str:
    body = ''.join(_run_section(r) for r in runs)
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(title)}</title>'
            f'</head><body style="font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;'
            f'background:#111;color:#eee;max-width:1000px;margin:0 auto;padding:24px">'
            f'<h1>{html.escape(title)}</h1>{body}</body></html>')


def sweep_table_html(axis, runs) -> str:
    """A compact comparison row per profile, for a single-axis sweep."""
    rows = []
    for r in runs:
        sm = r["summary"]
        s1, l1 = _bar(sm.get("extraction_score"))
        s2, l2 = _bar(sm.get("recall_rate"))
        rows.append(
            f'<tr><td style="padding:6px">{html.escape(r["profile"])}</td>'
            f'<td style="padding:6px;{s1}">{l1}</td>'
            f'<td style="padding:6px">{_pct(sm.get("extraction_recall"))}</td>'
            f'<td style="padding:6px">{_pct(sm.get("extraction_precision"))}</td>'
            f'<td style="padding:6px;{s2}">{l2}</td></tr>')
    return (f'<h2>Sweep: {html.escape(axis)}</h2>'
            f'<table style="border-collapse:collapse;width:100%"><tr style="text-align:left">'
            f'<th style="padding:6px">profile</th><th style="padding:6px">ext score</th>'
            f'<th style="padding:6px">ext recall</th><th style="padding:6px">ext precision</th>'
            f'<th style="padding:6px">recall probes</th></tr>'
            + ''.join(rows) + '</table>')


def write_reports(runs, outdir, sweep_axis=None):
    json_path = path.join(outdir, "results.json")
    html_path = path.join(outdir, "report.html")
    with open(json_path, "w") as f:
        json.dump(runs, f, indent=2)
    body = build_html(runs)
    if sweep_axis:
        body = body.replace("</h1>", "</h1>" + sweep_table_html(sweep_axis, runs))
    with open(html_path, "w") as f:
        f.write(body)
    return html_path, json_path


def print_summary(runs):
    print(f"\n{'═' * 72}")
    print(f"{'profile':18}{'ext':>6}{'recall':>8}{'prec':>7}{'recall@probe':>14}{'forget':>9}")
    for r in runs:
        sm = r["summary"]
        print(f"{r['profile'][:18]:18}"
              f"{_pct(sm.get('extraction_score')):>6}"
              f"{_pct(sm.get('extraction_recall')):>8}"
              f"{_pct(sm.get('extraction_precision')):>7}"
              f"{str(sm.get('recall_probes')):>14}"
              f"{str(sm.get('forget_probes')):>9}")
