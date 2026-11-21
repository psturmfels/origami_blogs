#!/usr/bin/env python3
"""Turn saved blog-page MHTML files into clean, self-contained, printable MHTML files.

The output keeps the article's title, text, images, captions, tables and math, but
drops the site chrome (navigation, sidebars, sharing widgets, related posts, ads,
scripts and stylesheets).  Images are embedded as data URIs so the files work
offline; images that were lazy-loaded and therefore missing from the archive are
downloaded from their original URL and cached.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import email
import email.policy
import hashlib
import html
import json
import mimetypes
import re
import sys
import urllib.parse
from pathlib import Path

import requests
from bs4 import BeautifulSoup, Comment, Tag

DROP_TAGS = {
    "script",
    "style",
    "link",
    "noscript",
    "iframe",
    "form",
    "input",
    "button",
    "select",
    "textarea",
    "nav",
    "aside",
    "svg",
}

CLUTTER_RE = re.compile(
    r"(?:"
    r"heateor|the_champ|sharedaddy|sassy-social|social-share|share-|sharing|"
    r"addtoany|shareaholic|dpsp|"
    r"related|promo|subscribe|newsletter|mc4wp|"
    r"advert|adsbygoogle|cookie|consent|"
    r"breadcrumb|screen-reader|sr-only|printfriendly|print-button|"
    r"wp-block-buttons|wp-block-social-links|wp-block-file|wp-block-embed|"
    r"post-tags|cat-links|entry-meta|author-box|post-navigation|"
    r"comments?-|comment-form|sidebar|widget|banner|popup|modal|"
    r"rating|toc-|yarpp|jp-relatedposts"
    r")",
    re.IGNORECASE,
)

PRESENTATIONAL_ATTRS = {
    "class",
    "id",
    "style",
    "align",
    "border",
    "cellpadding",
    "cellspacing",
    "bgcolor",
    "color",
    "face",
    "size",
    "valign",
    "loading",
    "decoding",
    "fetchpriority",
    "srcset",
    "sizes",
    "data-src",
    "data-lazy-src",
    "data-original",
}

CACHE_DIR = Path("/tmp/origami_mhtml_image_cache")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def clean_text(text: str) -> str:
    text = text.replace("\xa0", " ").replace("\u200b", "")
    return re.sub(r"\s+", " ", text).strip()


def url_keys(url: str) -> list[str]:
    if not url:
        return []
    url = url.strip()
    keys = [url]
    defrag = urllib.parse.urldefrag(url)[0]
    if defrag not in keys:
        keys.append(defrag)
    for item in list(keys):
        unquoted = urllib.parse.unquote(item)
        if unquoted not in keys:
            keys.append(unquoted)
    return keys


def parse_mhtml(path: Path):
    with path.open("rb") as handle:
        message = email.message_from_binary_file(handle, policy=email.policy.default)

    html_text = None
    page_url = None
    archive: dict[str, tuple[str, bytes]] = {}
    cid_map: dict[str, tuple[str, bytes]] = {}

    for part in message.walk():
        content_type = part.get_content_type()
        if content_type == "text/html" and html_text is None:
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            html_text = payload.decode(charset, "replace")
            page_url = part.get("Content-Location")
            continue
        if not content_type.startswith("image/"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        item = (content_type, payload)
        location = part.get("Content-Location")
        if location:
            for key in url_keys(location):
                archive[key] = item
        content_id = part.get("Content-ID")
        if content_id:
            cid_map[content_id.strip().strip("<>")] = item

    if html_text is None:
        raise ValueError(f"{path}: no text/html part found")

    if not page_url:
        page_url = message.get("Snapshot-Content-Location")
    return message, html_text, page_url, archive, cid_map


def strip_site_suffix(title: str) -> str:
    patterns = [
        r"\s*\|\s*Abrashi Origami School\s*$",
        r"\s*\|\s*Azure Puzzle Grove\s*$",
        r"\s*[-–]\s*Tsai Origami\s*$",
    ]
    for pattern in patterns:
        title = re.sub(pattern, "", title)
    return title


def extract_title(soup: BeautifulSoup) -> str:
    meta = soup.find("meta", attrs={"property": "og:title"})
    if meta and meta.get("content"):
        title = clean_text(meta["content"])
        if title:
            return strip_site_suffix(title)

    if soup.title:
        title = clean_text(soup.title.get_text(" "))
        if title:
            return strip_site_suffix(title)

    selectors = [
        "article h1",
        "article .entry-title",
        ".single-post-title",
        "h1.post-title",
        "h1.entry-title",
        "h1",
    ]
    for selector in selectors:
        for element in soup.select(selector):
            text = clean_text(element.get_text(" "))
            if text:
                return text
    return "Untitled"


def pick_content_root(soup: BeautifulSoup) -> Tag:
    candidates = soup.select("div.entry-content, div.post-content")
    if candidates:
        return max(candidates, key=lambda el: len(el.get_text(" ", strip=True)))
    for selector in ("article", "main", "body"):
        element = soup.select_one(selector)
        if element:
            return element
    raise ValueError("could not find article content")


def replace_math(root: Tag) -> None:
    # MathJax: the visible CHTML is not usable without its stylesheet.  The
    # assistive subtree contains real MathML, which browsers render natively.
    for container in root.find_all("mjx-container"):
        math = container.find("math")
        if math is not None:
            math = math.extract()
            container.replace_with(math)
        else:
            container.decompose()

    # KaTeX: keep the MathML representation, discard the styled HTML rendering.
    for katex in root.select("span.katex"):
        math = katex.select_one(".katex-mathml math")
        is_block = katex.find_parent(class_="katex-display") is not None
        if math is not None:
            math = math.extract()
            if is_block:
                math["display"] = "block"
            katex.replace_with(math)
        else:
            katex.decompose()


def fetch_live_figure_map(page_url: str | None) -> dict[str, str]:
    """Map figure captions on the live page to image URLs.

    Some archive images were lazy-loaded via ``srcset`` and were not captured at
    all by the browser save.  Their original URL is still present on the live
    page, so use the caption text to recover it.
    """
    if not page_url:
        return {}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(page_url.encode("utf-8")).hexdigest()
    html_path = CACHE_DIR / f"page-{digest}.html"
    try:
        if html_path.exists():
            text = html_path.read_text(encoding="utf-8", errors="replace")
        else:
            response = requests.get(
                page_url,
                headers={"User-Agent": USER_AGENT},
                timeout=(10, 40),
            )
            response.raise_for_status()
            text = response.text
            html_path.write_text(text, encoding="utf-8")
        soup = BeautifulSoup(text, "lxml")
        root = pick_content_root(soup)
    except Exception as exc:  # noqa: BLE001
        print(f"    ! could not read live page {page_url}: {exc}", file=sys.stderr)
        return {}

    mapping: dict[str, str] = {}
    for figure in root.find_all("figure"):
        caption = figure.find("figcaption")
        image = figure.find("img")
        if not caption or not image:
            continue
        src = image.get("src") or image.get("data-src") or image.get("data-lazy-src")
        if not src and image.get("srcset"):
            candidates = []
            for item in image["srcset"].split(","):
                bits = item.strip().split()
                if not bits:
                    continue
                width = 0
                if len(bits) > 1 and bits[1].endswith("w"):
                    try:
                        width = int(bits[1][:-1])
                    except ValueError:
                        width = 0
                candidates.append((width, bits[0]))
            if candidates:
                src = max(candidates)[1]
        if src:
            mapping[clean_text(caption.get_text(" "))] = src
    return mapping


def fetch_image(url: str, referer: str | None) -> tuple[str, bytes] | None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    data_path = CACHE_DIR / f"{digest}.bin"
    meta_path = CACHE_DIR / f"{digest}.json"
    if data_path.exists() and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            return meta["content_type"], data_path.read_bytes()
        except (OSError, ValueError, KeyError):
            pass

    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
    try:
        response = requests.get(url, headers=headers, timeout=(10, 40))
        response.raise_for_status()
        data = response.content
        content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not content_type.startswith("image/"):
            guessed, _ = mimetypes.guess_type(url)
            content_type = guessed or "image/jpeg"
        if len(data) > 30 * 1024 * 1024:
            raise ValueError("image too large")
        data_path.write_bytes(data)
        meta_path.write_text(json.dumps({"content_type": content_type, "url": url}))
        return content_type, data
    except Exception as exc:  # noqa: BLE001 - keep going on any single image failure
        print(f"    ! could not fetch {url}: {exc}", file=sys.stderr)
        return None


def content_type_for(data: bytes, suggested: str | None, url: str | None) -> str:
    if suggested:
        content_type = suggested.split(";")[0].strip().lower()
    else:
        content_type = ""
    if not content_type or not content_type.startswith("image/"):
        if data.lstrip()[:5].lower() in (b"<svg ", b"<?xml"):
            content_type = "image/svg+xml"
        elif url:
            content_type = mimetypes.guess_type(url)[0] or ""
    if content_type == "image/jpg":
        content_type = "image/jpeg"
    return content_type or "image/jpeg"


def data_uri(content_type: str, data: bytes) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


def collect_image_jobs(root: Tag, page_url: str | None, archive, cid_map, live_map):
    jobs = {}
    for img in root.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
        resolved = None
        if src and src.startswith("cid:"):
            key = src[4:].strip().strip("<>")
            if key in cid_map:
                resolved = ("archive", cid_map[key])
        elif src and src.startswith("data:"):
            resolved = ("data", src)
        elif src:
            absolute = urllib.parse.urljoin(page_url or "", src)
            for key in url_keys(absolute):
                if key in archive:
                    resolved = ("archive", archive[key])
                    break
            if resolved is None and absolute.startswith(("http://", "https://")):
                resolved = ("fetch", absolute)
        if resolved is None and img.get("srcset"):
            # Last resort: take the largest candidate from srcset.
            candidates = []
            for item in img["srcset"].split(","):
                bits = item.strip().split()
                if not bits:
                    continue
                width = 0
                if len(bits) > 1 and bits[1].endswith("w"):
                    try:
                        width = int(bits[1][:-1])
                    except ValueError:
                        width = 0
                candidates.append((width, bits[0]))
            if candidates:
                absolute = urllib.parse.urljoin(page_url or "", max(candidates)[1])
                for key in url_keys(absolute):
                    if key in archive:
                        resolved = ("archive", archive[key])
                        break
                if resolved is None:
                    resolved = ("fetch", absolute)
        if resolved is None and live_map:
            figure = img.find_parent("figure")
            caption = figure.find("figcaption") if figure else None
            if caption:
                live_src = live_map.get(clean_text(caption.get_text(" ")))
                if live_src:
                    absolute = urllib.parse.urljoin(page_url or "", live_src)
                    resolved = ("fetch", absolute)
        img["data-printify-source"] = "none"
        if resolved and resolved[0] == "data":
            img["src"] = resolved[1]
            img["data-printify-source"] = "data"
            continue
        if resolved and resolved[0] == "archive":
            content_type, data = resolved[1]
            img["src"] = data_uri(content_type_for(data, content_type, None), data)
            img["data-printify-source"] = "archive"
            continue
        if resolved and resolved[0] == "fetch":
            url = resolved[1]
            jobs.setdefault(url, []).append(img)
            img["data-printify-url"] = url
    return jobs


def fallback_image(img: Tag, url: str | None) -> None:
    alt = clean_text(img.get("alt") or "")
    label = "image unavailable" if not alt else f"image unavailable: {alt}"
    replacement = BeautifulSoup("", "lxml").new_tag("span")
    replacement["class"] = "missing-image"
    replacement.string = f"[{label}]"
    img.replace_with(replacement)


def unwrap_junk(root: Tag) -> None:
    for comment in root.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()

    for element in list(root.find_all(DROP_TAGS)):
        element.decompose()

    for element in list(root.find_all(True)):
        if element.parent is None:
            continue
        classes = " ".join(element.get("class") or [])
        ident = element.get("id") or ""
        if CLUTTER_RE.search(classes) or CLUTTER_RE.search(ident):
            element.decompose()

    # Spans carry no useful semantics once math and site styling are gone.
    for span in list(root.find_all("span")):
        span.unwrap()

    # Keep side-by-side figure rows; unwrap all other layout divs.
    for div in list(root.find_all("div")):
        classes = div.get("class") or []
        if "figrow" in classes:
            continue
        div.unwrap()

    # Footnote back-arrows add noise when printed.
    for anchor in list(root.find_all("a")):
        if clean_text(anchor.get_text(" ")) in ("↩", "↩︎", "↑", "↑︎"):
            anchor.decompose()

    # Linked images / gallery wrappers are noise; keep only the image itself.
    for anchor in list(root.find_all("a")):
        if anchor.find("img"):
            anchor.unwrap()


def clean_caption_text(text: str) -> str:
    text = clean_text(text)
    pattern = re.compile(r"^((?:Figure|Fig\.)\s*\d+)(?:\s*\1)+", re.IGNORECASE)
    match = pattern.match(text)
    if not match:
        return text
    label = match.group(1)
    rest = text[match.end():]
    rest = re.sub(r"\s*" + re.escape(label) + r"\s*$", "", rest, flags=re.IGNORECASE)
    rest = clean_text(rest)
    return clean_text(f"{label} {rest}" if rest else label)


def tidy_tags(root: Tag) -> None:
    for element in root.find_all(True):
        if element.name == "div" and "figrow" in (element.get("class") or []):
            element.attrs = {"class": ["figrow"]}
            continue
        for attr in list(element.attrs):
            if attr in PRESENTATIONAL_ATTRS:
                del element[attr]
        if element.name == "a":
            for attr in list(element.attrs):
                if attr != "href":
                    del element[attr]
        elif element.name == "img":
            for attr in list(element.attrs):
                if attr not in ("src", "alt", "title"):
                    del element[attr]
        elif element.name == "math" or element.find_parent("math"):
            # Preserve MathML attributes (accent, stretchy, display, ...).
            continue
        elif element.name in ("td", "th"):
            for attr in list(element.attrs):
                if attr not in ("colspan", "rowspan"):
                    del element[attr]
        elif element.name == "ol":
            for attr in list(element.attrs):
                if attr not in ("start", "type"):
                    del element[attr]
        else:
            element.attrs = {}

    # Remove leftover empty wrappers.
    for element in reversed(root.find_all(True)):
        if element.name in ("img", "br", "hr", "math", "td", "th", "tr", "table"):
            continue
        if element.find(["img", "br", "hr", "math"]):
            continue
        if element.get_text(" ", strip=True):
            continue
        if element.name in ("figure", "figcaption", "p", "li", "ul", "ol", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "span", "div"):
            element.decompose()

    # Normalise non-breaking spaces / zero-width characters in text nodes.
    for text_node in root.find_all(string=True):
        if text_node.parent and text_node.parent.name in ("pre", "code"):
            continue
        cleaned = text_node.replace("\xa0", " ").replace("\u200b", "")
        if cleaned != text_node:
            text_node.replace_with(cleaned)

    # Repair a WordPress caption that repeats its "Figure N" label.
    for caption in root.find_all("figcaption"):
        if caption.find(["math", "img"]):
            continue
        original = clean_text(caption.get_text(" "))
        cleaned = clean_caption_text(original)
        if cleaned != original:
            caption.clear()
            caption.append(cleaned)


PRINT_CSS = """
@page {
  size: auto;
  margin: 18mm 17mm;
}
html {
  font-size: 11pt;
}
body {
  margin: 0 auto;
  padding: 2.2em 1.2em;
  max-width: 46em;
  font-family: Georgia, "Times New Roman", "Noto Serif", serif;
  line-height: 1.5;
  color: #111;
  background: #fff;
}
h1, h2, h3, h4, h5, h6 {
  font-family: -apple-system, "Helvetica Neue", Arial, sans-serif;
  line-height: 1.22;
  color: #111;
  page-break-after: avoid;
  break-after: avoid;
}
h1.post-title {
  font-size: 21pt;
  margin: 0 0 1em;
  padding-bottom: 0.35em;
  border-bottom: 1.5px solid #999;
}
h2 { font-size: 15pt; margin: 1.5em 0 0.45em; }
h3 { font-size: 13pt; margin: 1.3em 0 0.4em; }
h4 { font-size: 11.5pt; margin: 1.2em 0 0.35em; }
h5, h6 { font-size: 11pt; margin: 1.1em 0 0.3em; }
p {
  margin: 0 0 0.85em;
  orphans: 3;
  widows: 3;
}
ul, ol {
  margin: 0.55em 0 0.9em 1.5em;
  padding: 0;
}
li { margin: 0 0 0.32em; }
blockquote {
  margin: 1em 1.6em;
  padding: 0 0 0 1em;
  border-left: 3px solid #bbb;
  color: #333;
  font-style: italic;
}
figure {
  margin: 1.15em 0;
  text-align: center;
  page-break-inside: avoid;
  break-inside: avoid;
}
img {
  display: block;
  max-width: 100%;
  max-height: 9in;
  width: auto;
  height: auto;
  margin: 0 auto;
}
figcaption {
  margin-top: 0.4em;
  color: #444;
  font-size: 9.5pt;
  font-style: italic;
  line-height: 1.35;
}
.figrow {
  display: flex;
  flex-wrap: wrap;
  gap: 1em;
  align-items: flex-start;
  justify-content: center;
  margin: 1.15em 0;
  page-break-inside: avoid;
  break-inside: avoid;
}
.figrow figure {
  flex: 1 1 42%;
  min-width: 14em;
  margin: 0;
}
.figrow img { max-height: 4.5in; }
table {
  border-collapse: collapse;
  margin: 1em auto;
  font-size: 10pt;
  page-break-inside: avoid;
  break-inside: avoid;
}
th, td {
  border: 1px solid #999;
  padding: 0.3em 0.55em;
  text-align: left;
  vertical-align: top;
}
thead { display: table-header-group; }
hr {
  border: 0;
  border-top: 1px solid #bbb;
  margin: 1.6em 0;
}
a { color: inherit; text-decoration: none; }
code, pre, kbd, samp {
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  font-size: 9.5pt;
}
pre {
  white-space: pre-wrap;
  padding: 0.6em;
  border: 1px solid #ddd;
  background: #f7f7f7;
}
sup, sub { line-height: 0; }
math {
  font-family: "STIX Two Math", "Latin Modern Math", Cambria Math, serif;
  font-size: 1.05em;
}
math[display="block"] {
  display: block;
  margin: 0.8em auto;
  text-align: center;
}
.missing-image {
  display: inline-block;
  padding: 0.5em 0.8em;
  border: 1px dashed #aaa;
  color: #777;
  font-size: 9pt;
  font-style: italic;
}
.source {
  margin-top: 2.5em;
  padding-top: 0.7em;
  border-top: 1px solid #ccc;
  color: #666;
  font-size: 8.5pt;
  line-height: 1.4;
  word-break: break-all;
}
@media print {
  body { padding: 0; max-width: none; }
  a[href^="http"]::after { content: ""; }
}
"""


def build_printable_html(title: str, root: Tag, source_url: str | None) -> str:
    inner = "".join(str(child) for child in root.contents)
    source = ""
    if source_url:
        source = (
            '<div class="source">Source: '
            f'<a href="{html.escape(source_url, quote=True)}">{html.escape(source_url)}</a>'
            "</div>"
        )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{PRINT_CSS}</style>
</head>
<body>
<h1 class="post-title">{html.escape(title)}</h1>
{inner}
{source}
</body>
</html>
"""


def write_mhtml(path: Path, title: str, final_html: str, page_url: str | None) -> None:
    message = email.message.EmailMessage(policy=email.policy.default.clone(linesep="\r\n"))
    message["MIME-Version"] = "1.0"
    message["Subject"] = title
    if page_url:
        message["Snapshot-Content-Location"] = page_url
    message.set_type("multipart/related")
    message.set_param("type", "text/html")

    part = email.message.EmailMessage(policy=email.policy.default.clone(linesep="\r\n"))
    if page_url:
        part["Content-Location"] = page_url
    part.set_content(final_html, subtype="html", charset="utf-8", cte="base64")
    message.attach(part)

    path.write_bytes(message.as_bytes())


def transform(path: Path, fetch: bool, workers: int) -> tuple[int, int, int]:
    _message, html_text, page_url, archive, cid_map = parse_mhtml(path)
    soup = BeautifulSoup(html_text, "lxml")
    title = extract_title(soup)
    root = pick_content_root(soup)

    replace_math(root)

    needs_live = any(
        not (
            img.get("src")
            or img.get("data-src")
            or img.get("data-lazy-src")
            or img.get("srcset")
        )
        for img in root.find_all("img")
    )
    live_map = fetch_live_figure_map(page_url) if fetch and needs_live else {}
    jobs = collect_image_jobs(root, page_url, archive, cid_map, live_map)

    fetched = 0
    failed = 0
    if fetch and jobs:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            future_to_url = {
                pool.submit(fetch_image, url, page_url): url for url in jobs
            }
            for future in concurrent.futures.as_completed(future_to_url):
                url = future_to_url[future]
                result = future.result()
                if result is None:
                    failed += len(jobs[url])
                    for img in jobs[url]:
                        fallback_image(img, url)
                    continue
                content_type, data = result
                uri = data_uri(content_type_for(data, content_type, url), data)
                for img in jobs[url]:
                    img["src"] = uri
                    img["data-printify-source"] = "fetched"
                fetched += len(jobs[url])
    else:
        for url, imgs in jobs.items():
            failed += len(imgs)
            for img in imgs:
                fallback_image(img, url)

    for img in list(root.find_all("img")):
        if img.get("data-printify-source") == "none":
            fallback_image(img, img.get("data-printify-url"))
    for img in root.find_all("img"):
        img.attrs = {
            key: value
            for key, value in img.attrs.items()
            if key in ("src", "alt", "title")
        }

    unwrap_junk(root)
    tidy_tags(root)

    final_html = build_printable_html(title, root, page_url)
    write_mhtml(path, title, final_html, page_url)

    from_archive = sum(
        1
        for img in BeautifulSoup(final_html, "lxml").find_all("img")
        if (img.get("src") or "").startswith("data:")
    )
    print(
        f"  {path.name}\n"
        f"    title: {title}\n"
        f"    images embedded: {from_archive}  fetched: {fetched}  failed: {failed}"
    )
    return from_archive, fetched, failed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="MHTML files to rewrite in place")
    parser.add_argument("--no-fetch", action="store_true", help="do not download missing images")
    parser.add_argument("--workers", type=int, default=8, help="parallel image downloads")
    args = parser.parse_args()

    totals = [0, 0, 0]
    for path in args.paths:
        print(f"Processing {path.name}")
        try:
            result = transform(path, not args.no_fetch, args.workers)
        except Exception as exc:  # noqa: BLE001
            print(f"  !! failed: {exc}", file=sys.stderr)
            continue
        totals = [a + b for a, b in zip(totals, result)]

    print(f"\nDone. embedded={totals[0]} fetched={totals[1]} failed={totals[2]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
