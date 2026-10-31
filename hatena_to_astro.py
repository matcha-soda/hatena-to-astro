#!/usr/bin/env python3
"""
Convert Hatena Blog MT export files into Astro-compatible Markdown.

- Converts entry HTML to Markdown.
- Downloads images into post-specific subdirectories (Asset Colocation / Leaf Bundle)
  to fully leverage Astro's built-in image optimization.
- Uses slug.md (via folder/index.md structure) as the preferred path.
- Removes common Hatena-specific elements & embed cards.
- Supports Astro Content Collections (JST timezone, frontmatter slug support).
"""

import argparse
import hashlib
import mimetypes
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as md

try:
    from dateutil import parser as date_parser
except ImportError:
    date_parser = None


# JST (UTC+9) タイムゾーン定義
JST = timezone(timedelta(hours=9))

HATENA_SELECTORS = [
    "script",
    "style",
    "iframe",
    "form",
    ".hatena-star-container",
    ".hatena-star",
    ".bookmark-widget",
    ".hatena-bookmark-button",
    ".social-buttons",
    ".entry-footer-modules",
    ".google-adsense",
    ".adsbygoogle",
    ".embed-card",
    ".hatena-asin-detail",
]

HATENA_URL_PATTERNS = [
    r"https?://(?:www\.)?hatena\.ne\.jp/",
    r"https?://b\.hatena\.ne\.jp/",
    r"https?://s\.hatena\.ne\.jp/",
    r"https?://cdn\.blog\.st-hatena\.com/",
]

FOTO_ID_PATTERN = re.compile(
    r"f:id:[A-Za-z0-9_\-]+:\d{14}(?::(?:image|plain))?(?::w=\d+)?(?::h=\d+)?",
    re.IGNORECASE,
)

SECTION_LABELS = {
    "BODY",
    "EXTENDED BODY",
    "EXCERPT",
    "KEYWORDS",
    "CONVERT BREAKS",
    "ALLOW COMMENTS",
    "ALLOW PINGS",
}

SECTION_LABEL_PATTERN = re.compile(
    r"^(BODY|EXTENDED BODY|EXCERPT|KEYWORDS|CONVERT BREAKS|ALLOW COMMENTS|ALLOW PINGS)\s*:\s*$",
    re.IGNORECASE,
)


def parse_entries(path):
    raw_text = path.read_text(encoding="utf-8-sig", errors="replace")

    text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("：", ":")

    entries = []

    def clean_section(value):
        lines = value.splitlines()

        while lines:
            first_line = lines[0].strip()

            if first_line.upper() in SECTION_LABELS:
                lines.pop(0)
                continue

            if SECTION_LABEL_PATTERN.match(first_line):
                lines.pop(0)
                continue

            break

        return "\n".join(lines).strip()

    for block in re.split(r"(?m)^--------\s*$", text):
        if not block.strip():
            continue

        lines = block.splitlines()

        separator_index = None

        for i, line in enumerate(lines):
            if line.strip() == "-----":
                separator_index = i
                break

        if separator_index is None:
            continue

        header_lines = lines[:separator_index]
        remaining_lines = lines[separator_index + 1:]

        fields = {}
        current_key = None
        current_lines = []

        field_pattern = re.compile(r"^([A-Za-z0-9 _\-]+):\s?(.*)$")

        for line in header_lines:
            match = field_pattern.match(line)

            if match:
                if current_key:
                    fields[current_key] = "\n".join(current_lines).strip()

                current_key = match.group(1).strip().upper()
                current_lines = [match.group(2)]

            elif current_key:
                current_lines.append(line)

        if current_key:
            fields[current_key] = "\n".join(current_lines).strip()

        if "TITLE" not in fields:
            continue

        body_parts = []
        current_section = []

        for line in remaining_lines:
            if line.strip() == "-----":
                body_parts.append("\n".join(current_section))
                current_section = []
            else:
                current_section.append(line)

        body_parts.append("\n".join(current_section))

        body = clean_section(body_parts[0]) if len(body_parts) > 0 else ""
        extended_body = clean_section(body_parts[1]) if len(body_parts) > 1 else ""

        entries.append(
            {
                "title": fields.get("TITLE", "Untitled"),
                "date": fields.get("DATE", ""),
                "status": fields.get("STATUS", "publish").lower(),
                "categories": [
                    category.strip()
                    for category in fields.get("CATEGORY", "").split(",")
                    if category.strip()
                ],
                "basename": fields.get("BASENAME", ""),
                "body": body,
                "extended_body": extended_body,
            }
        )

    return entries


def parse_date(value):
    value = str(value).strip()

    if not value:
        sys.stderr.write("  ! DATE field is empty. Using current date.\n")
        return datetime.now(JST)

    value = value.replace("\u00a0", " ").strip()
    value = re.sub(r"\s+", " ", value)

    formats = [
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %I:%M:%S %p",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y/%m/%d %H:%M:%S",
    ]

    parsed_dt = None
    for date_format in formats:
        try:
            parsed_dt = datetime.strptime(value, date_format)
            break
        except ValueError:
            continue

    if parsed_dt is None and date_parser:
        try:
            parsed_dt = date_parser.parse(value)
        except (ValueError, OverflowError):
            pass

    if parsed_dt is None:
        sys.stderr.write(
            "  ! Could not parse DATE value: "
            + repr(value)
            + ". Using current date.\n"
        )
        return datetime.now(JST)

    if parsed_dt.tzinfo is None:
        parsed_dt = parsed_dt.replace(tzinfo=JST)
    else:
        parsed_dt = parsed_dt.astimezone(JST)

    return parsed_dt


def slugify(value, fallback):
    value = value.lower()
    value = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE)
    value = re.sub(r"[\s_]+", "-", value).strip("-")
    value = re.sub(r"[^a-z0-9\-]", "", value)
    value = value[:80]

    if not value:
        value = slugify(fallback, "post")

    return value or "post"


def clean_hatena_elements(soup):
    for selector in HATENA_SELECTORS:
        for element in soup.select(selector):
            element.decompose()

    for selector in ["a.keyword", "a.okeyword"]:
        for link in soup.select(selector):
            link.unwrap()

    for link in soup.find_all("a", href=True):
        href = link.get("href", "")

        if any(re.match(pattern, href) for pattern in HATENA_URL_PATTERNS):
            link.unwrap()

    for text_node in soup.find_all(string=True):
        if FOTO_ID_PATTERN.search(text_node):
            replaced = FOTO_ID_PATTERN.sub("", text_node)

            if not replaced.strip():
                text_node.extract()
            else:
                text_node.replace_with(replaced)

    for element in soup.find_all(["div", "span", "p"]):
        if not element.get_text(strip=True) and not element.find(
            ["img", "a", "iframe"]
        ):
            element.decompose()

    return soup


def html_to_markdown(html, cleanup=True):
    soup = BeautifulSoup(html, "html.parser")

    if cleanup:
        soup = clean_hatena_elements(soup)
    else:
        for element in soup(["script", "style"]):
            element.decompose()

    markdown = md(str(soup), heading_style="ATX", bullets="-")

    markdown = FOTO_ID_PATTERN.sub("", markdown)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()

    return markdown + "\n"


def download_image(url, folder, session):
    try:
        response = session.get(url, timeout=30)
        response.raise_for_status()

        parsed_url = urlparse(url)
        original_name = Path(unquote(parsed_url.path)).name
        original_name = re.sub(r"[^A-Za-z0-9._\-]", "_", original_name)

        if not original_name or original_name in (".", ".."):
            original_name = "image"

        destination = folder / original_name

        if not destination.suffix:
            content_type = response.headers.get("content-type", "").split(";")[0]
            extension = mimetypes.guess_extension(content_type) or ".jpg"
            destination = destination.with_suffix(extension)

        if destination.exists():
            digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
            destination = destination.with_name(
                destination.stem + "-" + digest + destination.suffix
            )

        if destination.exists():
            extra_digest = hashlib.md5(str(time.time()).encode("utf-8")).hexdigest()[:4]
            destination = destination.with_name(
                destination.stem + "-" + extra_digest + destination.suffix
            )

        folder.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(response.content)
        return destination.name

    except Exception as error:
        sys.stderr.write(
            "  ! Failed to download image: "
            + url
            + " ("
            + str(error)
            + ")\n"
        )
        return None


def localize_images(markdown, post_dir, session, sleep_seconds):
    pattern = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")

    def replace(match):
        alt = match.group(1)
        url = match.group(2)

        if url.startswith(("data:", "#", "mailto:")):
            return match.group(0)

        filename = download_image(url, post_dir, session)

        if not filename:
            return match.group(0)

        if sleep_seconds:
            time.sleep(sleep_seconds)

        # Astroの最適化対象にするため、同じフォルダ内を指す相対パス（./filename）にする
        return "![" + alt + "](./" + filename + ")"

    return pattern.sub(replace, markdown)


def escape_yaml(value):
    return value.replace('"', '\\"').replace("\n", " ")


def make_frontmatter(entry):
    date = parse_date(entry["date"]).strftime("%Y-%m-%dT%H:%M:%S%z")
    if len(date) >= 5 and date[-3] != ':':
        date = date[:-2] + ':' + date[-2:]

    title = escape_yaml(entry["title"])

    tags = ", ".join(
        escape_yaml(category) for category in entry["categories"]
    )

    lines = [
        "---",
        'title: "' + title + '"',
        'description: ""',
        "pubDate: " + date,
    ]

    if entry["categories"]:
        lines.append("tags: [" + tags + "]")

    lines.append("---")
    lines.append("")

    return "\n".join(lines) + "\n\n"


def main():
    parser = argparse.ArgumentParser(
        description="Convert Hatena Blog MT export files into Astro Markdown with Asset Colocation."
    )
    parser.add_argument("--input", required=True, help="Hatena Blog export .txt file")
    parser.add_argument("--astro-dir", default=".", help="Astro project root")
    parser.add_argument("--posts-dir", default="src/content/blog", help="Directory for Content Collections")
    parser.add_argument("--include-drafts", action="store_true")
    parser.add_argument("--drafts-only", action="store_true")
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--no-cleanup", action="store_true")
    parser.add_argument("--debug-date", action="store_true")
    args = parser.parse_args()

    input_path = Path(args.input).expanduser().resolve()
    astro_dir = Path(args.astro_dir).expanduser().resolve()
    posts_dir = astro_dir / args.posts_dir

    posts_dir.mkdir(parents=True, exist_ok=True)

    entries = parse_entries(input_path)
    print("Found " + str(len(entries)) + " entries.")

    if args.debug_date and entries:
        print("First entry DATE:", repr(entries[0]["date"]))
        print("Parsed first date:", parse_date(entries[0]["date"]))

    session = requests.Session()
    session.headers["User-Agent"] = "hatena-to-astro/1.0"

    used_slugs = set()

    for index, entry in enumerate(entries, 1):
        is_draft = entry["status"] != "publish"

        if args.drafts_only and not is_draft:
            print("[" + str(index) + "] Skipped published entry: " + entry["title"])
            continue

        if is_draft and not args.include_drafts and not args.drafts_only:
            print("[" + str(index) + "] Skipped draft: " + entry["title"])
            continue

        base_slug = slugify(entry["basename"], entry["title"])
        slug = base_slug
        number = 2

        while slug in used_slugs:
            slug = base_slug + "-" + str(number)
            number += 1

        used_slugs.add(slug)

        print("[" + str(index) + "] Converting: " + entry["title"])

        html = entry["body"]

        if entry["extended_body"]:
            html += "\n" + entry["extended_body"]

        markdown = html_to_markdown(html, cleanup=not args.no_cleanup)

        # 記事ごとにフォルダを切り（Leaf Bundle）、その中に画像とindex.mdを配置する
        post_dir = posts_dir / slug
        
        markdown = localize_images(
            markdown,
            post_dir,
            session,
            args.sleep,
        )

        # ファイル名は slug.md ではなく、フォルダ配下の index.md にすることで
        # Astroが同じフォルダ内の画像（./image.jpg）を認識・最適化できるようになる
        post_dir.mkdir(parents=True, exist_ok=True)
        output_path = post_dir / "index.md"

        output_path.write_text(
            make_frontmatter(entry) + markdown,
            encoding="utf-8",
        )

        print("  -> " + str(output_path.relative_to(astro_dir)))

    print("Done.")


if __name__ == "__main__":
    main()