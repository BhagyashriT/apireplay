"""Human-readable reports from a diff document: Markdown and HTML."""

import difflib
import html
import json
from datetime import datetime, timezone

from .diff import FAIL


def _pretty_body(body, encoding):
    from .pack import decode_body

    data = decode_body(body or "", encoding)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "<binary body>"
    try:
        return json.dumps(json.loads(text), indent=2, sort_keys=True)
    except (json.JSONDecodeError, ValueError):
        return text


def _unified_diff_text(expected_body, expected_enc, actual_body, actual_enc) -> str:
    exp_lines = _pretty_body(expected_body, expected_enc).splitlines()
    act_lines = _pretty_body(actual_body, actual_enc).splitlines()
    return "\n".join(
        difflib.unified_diff(exp_lines, act_lines,
                             fromfile="recorded", tofile="replayed", lineterm="")
    )


def render_markdown(diff: dict) -> str:
    summary = diff.get("summary", {})
    lat = summary.get("latency_ms", {})
    rec = lat.get("recorded", {})
    rep = lat.get("replayed", {})

    lines = [
        "# APIReplay report",
        "",
        f"Generated {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Summary",
        "",
        f"- Total requests: {summary.get('total', 0)}",
        f"- Passed: {summary.get('passed', 0)}",
        f"- Failed: {summary.get('failed', 0)}",
        "",
        "### Latency (ms)",
        "",
        "| percentile | recorded | replayed |",
        "|------------|----------|----------|",
        f"| p50 | {rec.get('p50')} | {rep.get('p50')} |",
        f"| p95 | {rec.get('p95')} | {rep.get('p95')} |",
        "",
    ]

    failures = [r for r in diff.get("requests", []) if r.get("verdict") == FAIL]
    if not failures:
        lines += ["## Failures", "", "None — every request matched its recording.", ""]
    else:
        lines += ["## Failures", ""]
        for req in failures:
            lines += [f"### {req.get('method')} {req.get('path')} ({req.get('id')})", ""]
            for check_name, check in req.get("checks", {}).items():
                mark = "ok" if check.get("pass") else "MISMATCH"
                lines.append(f"- {check_name}: {mark}")
            lines.append("")
            details = req.get("details", [])
            if details:
                lines.append("**Details**")
                lines.append("")
                for detail in details:
                    lines.append(f"- {detail}")
                lines.append("")
            body_diffs = req.get("checks", {}).get("body", {}).get("diffs", [])
            if body_diffs:
                lines += ["**Body diff**", "", "```diff"]
                # Rebuild a unified diff from the stored request data when available.
                recorded = req.get("_recorded", {})
                replayed = req.get("_replayed", {})
                udiff = _unified_diff_text(
                    recorded.get("body"), recorded.get("body_encoding"),
                    replayed.get("body"), replayed.get("body_encoding"),
                )
                lines.append(udiff if udiff else "(no textual diff available)")
                lines += ["```", ""]
    return "\n".join(lines) + "\n"


_HTML_CSS = """
body { font-family: -apple-system, Segoe UI, Helvetica, Arial, sans-serif;
       max-width: 960px; margin: 2rem auto; padding: 0 1rem; color: #1f2328; }
h1, h2, h3 { color: #0b3d91; }
table { border-collapse: collapse; margin: 1rem 0; }
th, td { border: 1px solid #d0d7de; padding: 0.4rem 0.8rem; text-align: left; }
th { background: #f6f8fa; }
.pass { color: #1a7f37; font-weight: bold; }
.fail { color: #d1242f; font-weight: bold; }
pre { background: #f6f8fa; padding: 1rem; overflow-x: auto;
      border: 1px solid #d0d7de; border-radius: 6px; }
.meta { color: #57606a; font-size: 0.9rem; }
"""


def render_html(diff: dict) -> str:
    summary = diff.get("summary", {})
    lat = summary.get("latency_ms", {})
    rec = lat.get("recorded", {})
    rep = lat.get("replayed", {})

    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        "<title>APIReplay report</title>",
        f"<style>{_HTML_CSS}</style></head><body>",
        "<h1>APIReplay report</h1>",
        f"<p class='meta'>Generated {html.escape(datetime.now(timezone.utc).isoformat())}</p>",
        "<h2>Summary</h2>",
        "<table><tr><th>Total</th><th>Passed</th><th>Failed</th></tr>",
        f"<tr><td>{summary.get('total', 0)}</td>"
        f"<td class='pass'>{summary.get('passed', 0)}</td>"
        f"<td class='fail'>{summary.get('failed', 0)}</td></tr></table>",
        "<h3>Latency (ms)</h3>",
        "<table><tr><th>percentile</th><th>recorded</th><th>replayed</th></tr>",
        f"<tr><td>p50</td><td>{rec.get('p50')}</td><td>{rep.get('p50')}</td></tr>",
        f"<tr><td>p95</td><td>{rec.get('p95')}</td><td>{rep.get('p95')}</td></tr>",
        "</table>",
        "<h2>Failures</h2>",
    ]
    failures = [r for r in diff.get("requests", []) if r.get("verdict") == FAIL]
    if not failures:
        parts.append("<p class='pass'>None — every request matched its recording.</p>")
    for req in failures:
        parts.append(
            f"<h3>{html.escape(str(req.get('method')))} "
            f"{html.escape(str(req.get('path')))} "
            f"({html.escape(str(req.get('id')))})</h3>"
        )
        parts.append("<table><tr><th>check</th><th>result</th></tr>")
        for check_name, check in req.get("checks", {}).items():
            cls = "pass" if check.get("pass") else "fail"
            label = "ok" if check.get("pass") else "MISMATCH"
            parts.append(
                f"<tr><td>{html.escape(check_name)}</td>"
                f"<td class='{cls}'>{label}</td></tr>"
            )
        parts.append("</table>")
        details = req.get("details", [])
        if details:
            parts.append("<ul>")
            parts += [f"<li>{html.escape(d)}</li>" for d in details]
            parts.append("</ul>")
        recorded = req.get("_recorded", {})
        replayed = req.get("_replayed", {})
        udiff = _unified_diff_text(
            recorded.get("body"), recorded.get("body_encoding"),
            replayed.get("body"), replayed.get("body_encoding"),
        )
        if udiff:
            parts.append("<h4>Body diff</h4>")
            parts.append(f"<pre>{html.escape(udiff)}</pre>")
    parts.append("</body></html>")
    return "\n".join(parts)


def write_report(diff: dict, fmt: str, path: str) -> None:
    if fmt == "html":
        text = render_html(diff)
    elif fmt == "md":
        text = render_markdown(diff)
    else:
        raise ValueError(f"unknown report format {fmt!r}; expected 'md' or 'html'")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
