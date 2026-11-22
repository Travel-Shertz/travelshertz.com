#!/usr/bin/env python3
"""PageSpeed Insights check against the live homepage (CNAME).

Intended path in each site repo: .github/scripts/pagespeed_check.py

Uses secrets.PAGESPEED_API_KEY when present (query param only; never printed).
If the key is missing or the API errors / rate-limits, this step warns and
does not fail the job. Score failures fail the job.

This measures the already-published site on main, not the PR branch.
Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
DEFAULT_PERF = 0.5
DEFAULT_CATEGORY = 0.8
CATEGORIES = ("PERFORMANCE", "ACCESSIBILITY", "BEST_PRACTICES", "SEO")
CATEGORY_LABELS = {
    "performance": "Performance",
    "accessibility": "Accessibility",
    "best-practices": "Best Practices",
    "seo": "SEO",
}


def mask_secret(value: str) -> None:
    if not value:
        return
    # GitHub Actions secret masking. Harmless when run locally.
    sys.stdout.write(f"::add-mask::{value}\n")
    sys.stdout.flush()
    # Also mask common query-escaped forms.
    escaped = urllib.parse.quote(value, safe="")
    if escaped != value:
        sys.stdout.write(f"::add-mask::{escaped}\n")
        sys.stdout.flush()


def load_cname(root: Path) -> str | None:
    p = root / "CNAME"
    if not p.is_file():
        return None
    lines = [
        ln.strip()
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    return lines[0] if lines else None


def live_homepage(cname: str) -> str:
    host = cname.strip().rstrip("/")
    if host.startswith("http://") or host.startswith("https://"):
        parsed = urllib.parse.urlparse(host)
        host = parsed.netloc or host
    return f"https://{host}/"


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"Warning: {name}={raw!r} is not a number; using default {default}.")
        return default


def write_summary(path: Path | None, lines: list[str]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def fetch_psi(page_url: str, api_key: str | None, timeout: int) -> tuple[dict | None, str | None]:
    params: list[tuple[str, str]] = [
        ("url", page_url),
        ("strategy", "mobile"),
    ]
    for cat in CATEGORIES:
        params.append(("category", cat))
    if api_key:
        params.append(("key", api_key))
    query = urllib.parse.urlencode(params)
    request_url = f"{API_ENDPOINT}?{query}"

    last_error = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(
                request_url,
                headers={"Accept": "application/json", "User-Agent": "site-checks-pagespeed"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body), None
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                err_body = exc.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                err_body = ""
            # Never include the request URL (it may contain the key).
            snippet = err_body.replace("\n", " ").strip()[:300]
            last_error = f"PageSpeed API HTTP {status}"
            if snippet:
                last_error += f": {snippet}"
            if status in {429, 500, 502, 503, 504} and attempt == 0:
                time.sleep(5)
                continue
            return None, last_error
        except urllib.error.URLError as exc:
            last_error = f"PageSpeed API request failed: {exc.reason}"
            if attempt == 0:
                time.sleep(3)
                continue
            return None, last_error
        except TimeoutError:
            last_error = f"PageSpeed API timed out after {timeout}s"
            if attempt == 0:
                continue
            return None, last_error
        except json.JSONDecodeError as exc:
            return None, f"PageSpeed API returned invalid JSON: {exc}"
    return None, last_error or "PageSpeed API request failed."


def run(root: Path, summary_path: Path | None) -> int:
    api_key = os.environ.get("PAGESPEED_API_KEY") or ""
    mask_secret(api_key)

    perf_min = env_float("PSI_PERF_THRESHOLD", DEFAULT_PERF)
    cat_min = env_float("PSI_CATEGORY_THRESHOLD", DEFAULT_CATEGORY)
    timeout = int(env_float("PSI_TIMEOUT", 90))

    lines = [
        "## PageSpeed Insights",
        "",
        "This step measures the **live homepage already published from main**, not the pull-request branch.",
        "",
    ]

    cname = load_cname(root)
    if not cname:
        msg = "No CNAME file found; skipped PageSpeed Insights (warning, not a failure)."
        lines += [f"> Warning: {msg}", ""]
        write_summary(summary_path, lines)
        print(f"WARNING: {msg}")
        return 0

    page_url = live_homepage(cname)
    key_state = "present" if api_key else "absent"
    print(f"PageSpeed Insights (mobile) for {page_url}")
    print(f"API key: {key_state}")
    lines.append(f"Live URL: {page_url} (mobile)")
    lines.append(f"API key: {key_state}")
    lines.append(f"Thresholds: performance ≥ {perf_min}, accessibility / SEO / best-practices ≥ {cat_min}")
    lines.append("")

    if not api_key:
        msg = (
            "PAGESPEED_API_KEY is not set. The public API may rate-limit unauthenticated calls; "
            "this step is a warning only and does not fail the job."
        )
        # Still try the public API (it can work at low volume), but without a key
        # neither errors nor low scores fail the job.
        data, error = fetch_psi(page_url, None, timeout)
        if error:
            lines += [f"> Warning: {error}", "", f"> Warning: {msg}", ""]
            write_summary(summary_path, lines)
            print(f"WARNING: {error}")
            print(f"WARNING: {msg}")
            return 0
        scores = extract_scores(data or {})
        lines += score_table(scores)
        if scores:
            lines.append("")
            lines.append("Scores are informational because PAGESPEED_API_KEY is absent; the job does not fail on them.")
            lines.append("")
        write_summary(summary_path, lines)
        for slug, score in scores.items():
            print(f"  {CATEGORY_LABELS.get(slug, slug)}: {format_score(score)}")
        print(f"WARNING: {msg}")
        return 0

    data, error = fetch_psi(page_url, api_key, timeout)
    if error:
        msg = "PageSpeed API error or rate limit; reporting a warning and not failing the job."
        lines += [f"> Warning: {error}", "", f"> Warning: {msg}", ""]
        write_summary(summary_path, lines)
        print(f"WARNING: {error}")
        print(f"WARNING: {msg}")
        return 0

    scores = extract_scores(data or {})
    if not scores:
        msg = "PageSpeed API returned no Lighthouse category scores; warning only, not a failure."
        lines += [f"> Warning: {msg}", ""]
        write_summary(summary_path, lines)
        print(f"WARNING: {msg}")
        return 0

    lines += score_table(scores)
    failures: list[str] = []
    perf = scores.get("performance")
    if perf is None:
        lines.append("")
        lines.append("> Warning: Performance score was missing from the API response.")
    elif perf < perf_min:
        failures.append(f"Performance score {format_score(perf)} is below {perf_min}.")
    for slug in ("accessibility", "best-practices", "seo"):
        score = scores.get(slug)
        label = CATEGORY_LABELS[slug]
        if score is None:
            lines.append(f"> Warning: {label} score was missing from the API response.")
        elif score < cat_min:
            failures.append(f"{label} score {format_score(score)} is below {cat_min}.")

    lines.append("")
    if failures:
        lines.append("**FAIL**")
        for f in failures:
            lines.append(f"- {f}")
        lines.append("")
    else:
        lines.append("PageSpeed scores meet the configured thresholds.")
        lines.append("")

    write_summary(summary_path, lines)
    for slug, score in scores.items():
        print(f"  {CATEGORY_LABELS.get(slug, slug)}: {format_score(score)}")
    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("PageSpeed scores meet the thresholds.")
    return 0


def extract_scores(data: dict) -> dict[str, float | None]:
    cats = (data.get("lighthouseResult") or {}).get("categories") or {}
    out: dict[str, float | None] = {}
    for slug in CATEGORY_LABELS:
        if slug not in cats:
            continue
        raw = cats[slug].get("score")
        out[slug] = float(raw) if raw is not None else None
    return out


def format_score(score: float | None) -> str:
    if score is None:
        return "n/a"
    return f"{score:.2f} ({int(round(score * 100))})"


def score_table(scores: dict[str, float | None]) -> list[str]:
    if not scores:
        return ["No category scores in the API response.", ""]
    rows = [
        "| Category | Score |",
        "| --- | --- |",
    ]
    for slug, label in CATEGORY_LABELS.items():
        if slug in scores:
            rows.append(f"| {label} | {format_score(scores[slug])} |")
    rows.append("")
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PageSpeed Insights check for the live homepage.")
    parser.add_argument("--root", default=".", help="Repository root (default: current directory).")
    parser.add_argument(
        "--summary",
        default=os.environ.get("GITHUB_STEP_SUMMARY"),
        help="Markdown summary path (default: $GITHUB_STEP_SUMMARY).",
    )
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"Root is not a directory: {root}", file=sys.stderr)
        return 2
    summary = Path(args.summary) if args.summary else None
    return run(root, summary)


if __name__ == "__main__":
    sys.exit(main())
