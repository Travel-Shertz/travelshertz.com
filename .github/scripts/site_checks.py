#!/usr/bin/env python3
"""HTML sanity, internal-link, and JSON-LD checks for static site repos.

Intended path in each site repo: .github/scripts/site_checks.py
Stdlib only. Exit 1 when any check fails.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urljoin, urlparse

SKIP_SCHEMES = {
    "mailto",
    "tel",
    "sms",
    "callto",
    "fax",
    "javascript",
    "data",
    "blob",
    "cid",
    "about",
    "intent",
    "market",
    "itms-apps",
}

# Public copy rule: the legal name is used in public text, never the acronym.
AGII_TOKEN = "AGII"


@dataclass
class PageDoc:
    path: Path  # repo-relative
    lang: str | None = None
    title: str = ""
    descriptions: list[str] = field(default_factory=list)
    h1s: list[str] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    alts: list[str] = field(default_factory=list)
    refs: list[tuple[str, str, str]] = field(default_factory=list)  # tag, attr, value
    jsonld: list[str] = field(default_factory=list)
    noindex: bool = False
    redirect_stub: bool = False
    parse_error: str | None = None


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.doc = PageDoc(path=Path("."))
        self._in_title = False
        self._in_h1 = False
        self._h1_buf: list[str] = []
        self._title_buf: list[str] = []
        self._in_script = False
        self._script_type = ""
        self._script_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        ad = {k: (v if v is not None else "") for k, v in attrs}
        if tag == "html":
            self.doc.lang = ad.get("lang")
        if tag == "title":
            self._in_title = True
        if tag == "h1":
            self._in_h1 = True
            self._h1_buf = []
        if tag == "meta" and ad.get("name", "").strip().lower() == "description":
            self.doc.descriptions.append(ad.get("content", ""))
        if tag == "meta" and ad.get("name", "").strip().lower() == "robots":
            if "noindex" in ad.get("content", "").lower():
                self.doc.noindex = True
        if tag == "meta" and ad.get("http-equiv", "").strip().lower() == "refresh":
            self.doc.redirect_stub = True
        if tag == "script":
            self._in_script = True
            self._script_type = ad.get("type", "")
            self._script_buf = []
        if "id" in ad and ad["id"] != "":
            self.doc.ids.append(ad["id"])
        if tag in {"a", "area"} and ad.get("name"):
            self.doc.names.append(ad["name"])
        if "alt" in ad:
            self.doc.alts.append(ad.get("alt", ""))
        for attr in ("href", "src"):
            if attr in ad:
                self.doc.refs.append((tag, attr, ad[attr]))

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            self.doc.title = "".join(self._title_buf)
            self._title_buf = []
        if tag == "h1":
            self._in_h1 = False
            self.doc.h1s.append("".join(self._h1_buf))
            self._h1_buf = []
        if tag == "script":
            if "ld+json" in self._script_type.lower():
                self.doc.jsonld.append("".join(self._script_buf))
            self._in_script = False
            self._script_type = ""
            self._script_buf = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_buf.append(data)
        if self._in_h1:
            self._h1_buf.append(data)
        if self._in_script:
            self._script_buf.append(data)


@dataclass
class Finding:
    check: str
    path: str
    message: str


def repo_html_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for p in root.rglob("*.html"):
        if any(part == ".git" or part == "node_modules" for part in p.parts):
            continue
        files.append(p)
    return sorted(files, key=lambda p: p.as_posix().lower())


def parse_page(path: Path, rel: Path) -> PageDoc:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8", errors="replace")
        doc = PageDoc(path=rel, parse_error="File is not valid UTF-8 (replaced invalid bytes to continue).")
        # still try to parse
        parser = PageParser()
        try:
            parser.feed(text)
            parser.close()
            parsed = parser.doc
            parsed.path = rel
            parsed.parse_error = doc.parse_error
            return parsed
        except Exception as exc:  # noqa: BLE001
            doc.parse_error = f"{doc.parse_error} Parser error: {exc}"
            return doc
    parser = PageParser()
    try:
        parser.feed(text)
        parser.close()
        parser.doc.path = rel
        return parser.doc
    except Exception as exc:  # noqa: BLE001
        return PageDoc(path=rel, parse_error=f"HTML parser error: {exc}")


def load_cname(root: Path) -> str | None:
    p = root / "CNAME"
    if not p.is_file():
        return None
    lines = [ln.strip() for ln in p.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip() and not ln.strip().startswith("#")]
    return lines[0] if lines else None


def site_hosts(cname: str | None) -> set[str]:
    if not cname:
        return set()
    host = cname.strip().lower().rstrip(".")
    if host.startswith("www."):
        apex = host[4:]
        return {host, apex}
    return {host, "www." + host}


def is_shop_host(host: str) -> bool:
    hostname = host.lower().split("@")[-1].split(":")[0]
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname.startswith("shop.")


def page_base_path(rel: Path) -> str:
    posix = rel.as_posix()
    if posix == "index.html":
        return "/"
    if posix.endswith("/index.html"):
        return "/" + posix[: -len("index.html")]
    return "/" + posix


def fragment_targets(doc: PageDoc) -> set[str]:
    return set(doc.ids) | set(doc.names)


def resolve_to_file(root: Path, url_path: str) -> Path | None:
    raw = unquote(url_path)
    raw = raw.split(";")[0]  # ignore path parameters
    raw = raw.lstrip("/")
    candidates: list[str] = []
    if raw == "" or raw.endswith("/"):
        candidates.append(raw + "index.html")
    else:
        candidates.append(raw)
        suffix = Path(raw).suffix
        if suffix == "":
            candidates.extend([raw + ".html", raw + "/index.html"])
        elif suffix == ".html":
            # /privacy.html is a real file; also allow a directory equivalent
            stem = raw[: -len(".html")]
            candidates.append(stem + "/index.html")
    for cand in candidates:
        # Guard against escaping the repo
        p = (root / cand).resolve()
        try:
            p.relative_to(root.resolve())
        except ValueError:
            continue
        if p.is_file():
            return p
    return None


def classify_url(
    raw: str,
    page_rel: Path,
    hosts: set[str],
) -> tuple[str, str | None, str | None]:
    """Return (kind, path, fragment).

    kind is one of: ignore, internal, invalid.
    """
    url = (raw or "").strip()
    if url == "":
        # Empty href/src on the current page is a same-document reference.
        return "internal", page_base_path(page_rel), None

    # Keep backslash typo visible as invalid rather than silently converting.
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme in SKIP_SCHEMES:
        return "ignore", None, None

    if url.startswith("#"):
        frag = unquote(url[1:])
        return "internal", page_base_path(page_rel), frag

    if url.startswith("//"):
        # Protocol-relative. Treat by host.
        parsed = urlparse("https:" + url)
        scheme = "https"

    if scheme in {"http", "https"}:
        host = (parsed.hostname or "").lower()
        if not host:
            return "invalid", None, None
        if is_shop_host(host):
            return "ignore", None, None
        if host not in hosts:
            return "ignore", None, None
        path = parsed.path or "/"
        frag = unquote(parsed.fragment) if parsed.fragment else None
        return "internal", path, frag

    if scheme and scheme not in {"", "http", "https"}:
        # Unknown scheme (e.g. ftp:). Not an in-repo file.
        return "ignore", None, None

    # Relative or root-relative, possibly with query/fragment.
    base = "https://site.invalid" + page_base_path(page_rel)
    joined = urljoin(base, url)
    parsed = urlparse(joined)
    path = parsed.path or "/"
    frag = unquote(parsed.fragment) if parsed.fragment else None
    return "internal", path, frag


def jsonld_issues(block: str, rel: Path, index: int) -> list[Finding]:
    findings: list[Finding] = []
    loc = f"{rel.as_posix()} (JSON-LD #{index + 1})"
    try:
        data = json.loads(block)
    except json.JSONDecodeError as exc:
        findings.append(Finding("jsonld", loc, f"Does not parse as JSON: {exc}"))
        return findings

    def has_context_and_type(obj: object) -> bool:
        if isinstance(obj, list):
            return any(has_context_and_type(x) for x in obj)
        if not isinstance(obj, dict):
            return False
        has_ctx = "@context" in obj
        has_type = "@type" in obj
        has_graph = "@graph" in obj
        if has_ctx and (has_type or has_graph):
            return True
        # A @graph node may itself be the typed payload; require the top-level
        # object to declare @context plus @type or @graph.
        return False

    if not has_context_and_type(data):
        findings.append(
            Finding(
                "jsonld",
                loc,
                "JSON parsed but is missing @context together with @type or @graph at the top level.",
            )
        )
    return findings


def agii_hits(doc: PageDoc) -> list[Finding]:
    hits: list[Finding] = []
    rel = doc.path.as_posix()

    def check(label: str, text: str) -> None:
        if AGII_TOKEN in (text or ""):
            hits.append(
                Finding(
                    "agii",
                    rel,
                    f'The string "AGII" appears in {label}. Public copy must use the full legal company name, not the acronym.',
                )
            )

    check("<title>", doc.title)
    for i, desc in enumerate(doc.descriptions):
        check("meta description" if len(doc.descriptions) == 1 else f"meta description #{i + 1}", desc)
    for i, alt in enumerate(doc.alts):
        check(f"alt text #{i + 1}", alt)
    for i, block in enumerate(doc.jsonld):
        check(f"JSON-LD block #{i + 1}", block)
    return hits


def markdown_escape(text: str) -> str:
    return text.replace("|", "\\|")


def write_summary(path: Path | None, lines: Iterable[str]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def run(root: Path, summary_path: Path | None) -> int:
    html_files = repo_html_files(root)
    cname = load_cname(root)
    hosts = site_hosts(cname)

    findings: list[Finding] = []
    pages: list[PageDoc] = []
    id_cache: dict[Path, set[str]] = {}

    if not html_files:
        write_summary(
            summary_path,
            [
                "## HTML, links, and JSON-LD",
                "",
                f"No `*.html` files found under `{root}`. Nothing to check.",
                "",
            ],
        )
        print("No HTML files found. Passing.")
        return 0

    for abs_path in html_files:
        rel = abs_path.relative_to(root)
        doc = parse_page(abs_path, rel)
        pages.append(doc)
        id_cache[abs_path] = fragment_targets(doc)
        if doc.parse_error:
            findings.append(Finding("html", rel.as_posix(), doc.parse_error))
            continue

        if doc.lang is None or not str(doc.lang).strip():
            findings.append(Finding("html", rel.as_posix(), "<html> is missing a lang attribute."))
        if not doc.title.strip():
            findings.append(Finding("html", rel.as_posix(), "Missing or empty <title>."))
        # Pages kept out of search (robots noindex) don't need a meta description,
        # and meta-refresh redirect stubs don't need an <h1>.
        if not doc.noindex and not any(d.strip() for d in doc.descriptions):
            findings.append(Finding("html", rel.as_posix(), "Missing or empty meta description."))
        if len(doc.h1s) == 0:
            if not doc.redirect_stub:
                findings.append(Finding("html", rel.as_posix(), "Missing <h1>."))
        elif len(doc.h1s) > 1:
            findings.append(
                Finding(
                    "html",
                    rel.as_posix(),
                    f"Expected one <h1>, found {len(doc.h1s)}: "
                    + "; ".join(repr(h.strip()) for h in doc.h1s),
                )
            )
        elif not doc.h1s[0].strip():
            findings.append(Finding("html", rel.as_posix(), "The <h1> is empty."))

        seen: dict[str, int] = defaultdict(int)
        for i in doc.ids:
            seen[i] += 1
        dups = sorted(k for k, n in seen.items() if n > 1)
        if dups:
            findings.append(
                Finding(
                    "html",
                    rel.as_posix(),
                    "Duplicate id values: " + ", ".join(repr(d) for d in dups) + ".",
                )
            )

        for i, block in enumerate(doc.jsonld):
            findings.extend(jsonld_issues(block, rel, i))
        findings.extend(agii_hits(doc))

        for tag, attr, value in doc.refs:
            kind, path, frag = classify_url(value, rel, hosts)
            if kind == "ignore":
                continue
            if kind == "invalid":
                findings.append(
                    Finding(
                        "links",
                        rel.as_posix(),
                        f"<{tag} {attr}> has an unusable URL: {value!r}.",
                    )
                )
                continue
            assert path is not None
            target = resolve_to_file(root, path)
            if target is None:
                findings.append(
                    Finding(
                        "links",
                        rel.as_posix(),
                        f"<{tag} {attr}=\"{value}\"> does not resolve to a file in the repo.",
                    )
                )
                continue
            if frag is None or frag == "":
                continue
            # Same-document or cross-page fragment must exist on the target page.
            if target.suffix.lower() != ".html":
                findings.append(
                    Finding(
                        "links",
                        rel.as_posix(),
                        f"<{tag} {attr}=\"{value}\"> points at a fragment on a non-HTML file.",
                    )
                )
                continue
            if target not in id_cache:
                trel = target.relative_to(root)
                tdoc = parse_page(target, trel)
                id_cache[target] = fragment_targets(tdoc)
            if frag not in id_cache[target]:
                findings.append(
                    Finding(
                        "links",
                        rel.as_posix(),
                        f"<{tag} {attr}=\"{value}\"> resolves, but #{frag} is not present on the target page.",
                    )
                )

    # Summary
    def by_check(name: str) -> list[Finding]:
        return [f for f in findings if f.check == name]

    html_f = by_check("html")
    link_f = by_check("links")
    json_f = by_check("jsonld")
    agii_f = by_check("agii")
    jsonld_pages = sum(1 for p in pages if p.jsonld)
    jsonld_blocks = sum(len(p.jsonld) for p in pages)

    def status(items: list[Finding]) -> str:
        return "FAIL" if items else "PASS"

    lines = [
        "## HTML, links, and JSON-LD",
        "",
        f"Repository root: `{root}`",
        f"Live host (CNAME): `{cname or 'not found'}`",
        f"HTML files checked: {len(pages)}",
        f"JSON-LD: {jsonld_blocks} block(s) on {jsonld_pages} page(s)"
        + (" — none present; that is allowed." if jsonld_blocks == 0 else "."),
        "",
        "| Check | Result | Findings |",
        "| --- | --- | ---: |",
        f"| HTML sanity | {status(html_f)} | {len(html_f)} |",
        f"| Internal links | {status(link_f)} | {len(link_f)} |",
        f"| JSON-LD | {status(json_f)} | {len(json_f)} |",
        f'| AGII public-copy rule | {status(agii_f)} | {len(agii_f)} |',
        "",
    ]
    if findings:
        lines.extend(["### Findings", ""])
        for f in findings:
            lines.append(f"- **{f.check}** `{f.path}`: {markdown_escape(f.message)}")
        lines.append("")
    else:
        lines.extend(["All HTML, link, JSON-LD, and AGII checks passed.", ""])

    write_summary(summary_path, lines)

    print(f"Checked {len(pages)} HTML file(s) under {root}")
    print(f"CNAME: {cname or 'not found'}")
    print(
        f"HTML {status(html_f)} ({len(html_f)}) | "
        f"links {status(link_f)} ({len(link_f)}) | "
        f"JSON-LD {status(json_f)} ({len(json_f)}) | "
        f"AGII {status(agii_f)} ({len(agii_f)})"
    )
    for f in findings:
        print(f"FAIL [{f.check}] {f.path}: {f.message}")
    if findings:
        print(f"\n{len(findings)} finding(s).")
        return 1
    print("All checks passed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Static site HTML, link, and JSON-LD checks.")
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
