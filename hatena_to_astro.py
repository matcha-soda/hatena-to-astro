#!/usr/bin/env python3
"""
Convert Hatena Blog MT export files into Astro-compatible Markdown.

- Converts entry HTML to Markdown.
- Downloads images into post-specific subdirectories (Asset Colocation / Leaf Bundle)
  so that Astro's built-in image optimization can be used.
- Each post is written to <posts-dir>/<slug>/index.md.
- Removes common Hatena-specific elements (stars, keyword links, ads, blog cards).
- Keeps the URL of non-Hatena iframe embeds (e.g. YouTube) as a plain link.
- Writes dates in JST (ISO 8601) and `draft: true` for non-published entries.
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


# JST (UTC+9)
JST = timezone(timedelta(hours=9))

# iframe / .embed-card は preserve_embeds() で個別に処理する
HATENA_SELECTORS = [
    "script",
    "style",
    "form",
    ".hatena-star-container",
    ".hatena-star",
    ".bookmark-widget",
    ".hatena-bookmark-button",
    ".social-buttons",
    ".entry-footer-modules",
    ".google-adsense",
    ".adsbygoogle",
    ".hatena-asin-detail",
]

HATENA_URL_PATTERNS = [
    r"https?://(?:www\.)?hatena\.ne\.jp/",
    r"https?://b\.hatena\.ne\.jp/",
    r"https?://s\.hatena\.ne\.jp/",
    r"https?://cdn\.blog\.st-hatena\.com/",
]

# f:id:account:20200101120000:image などのはてなフォトライフ記法。
# [ ] で囲まれた形、markdownify が [ ] や _ をエスケープした形も含めて除去する。
_FOTO_ID_CORE = (
    r"f:id:[A-Za-z0-9_\-\\]+:\d{14}"
    r"(?::(?:image|plain))?(?::w=\d+)?(?::h=\d+)?"
)
FOTO_ID_PATTERN = re.compile(
    r"\\?\[" + _FOTO_ID_CORE + r"\\?\]|" + _FOTO_ID_CORE,
    re.IGNORECASE,
)

# 「BODY:」などのセクション見出し行（全角コロンも許容）
SECTION_LABEL_PATTERN = re.compile(
    r"^(BODY|EXTENDED BODY|EXCERPT|KEYWORDS|CONVERT BREAKS|ALLOW COMMENTS|ALLOW PINGS)\s*[:：]\s*$",
    re.IGNORECASE,
)

# ヘッダのフィールド行（「TITLE: xxx」。全角コロンもキー直後のみ許容）
HEADER_FIELD_PATTERN = re.compile(r"^([A-Za-z0-9 _\-]+)[:：]\s?(.*)$")

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".avif")

# 実行結果のサマリ表示用
STATS = {"foto_removed": 0, "date_failed": 0, "image_failed": 0}


def warn(message):
    sys.stderr.write(message + "\n")


# ----------------------------------------------------------------------
# MT形式のパース
# ----------------------------------------------------------------------

def clean_section(value):
    """セクション先頭の「BODY:」等の見出し行だけを取り除く。"""
    lines = value.splitlines()

    while lines and SECTION_LABEL_PATTERN.match(lines[0].strip()):
        lines.pop(0)

    return "\n".join(lines).strip()


def parse_entries(path):
    raw_text = path.read_text(encoding="utf-8-sig", errors="replace")
    text = raw_text.replace("\r\n", "\n").replace("\r", "\n")

    entries = []

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

        # 同じキーが複数回出る（CATEGORY など）ため、値はリストで保持する
        pairs = []
        for line in header_lines:
            match = HEADER_FIELD_PATTERN.match(line)

            if match:
                pairs.append([match.group(1).strip().upper(), [match.group(2)]])
            elif pairs:
                pairs[-1][1].append(line)

        fields = {}
        for key, value_lines in pairs:
            fields.setdefault(key, []).append("\n".join(value_lines).strip())

        if "TITLE" not in fields:
            continue

        def first(key, default=""):
            values = fields.get(key)
            return values[0] if values else default

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

        categories = []
        for category in fields.get("CATEGORY", []):
            category = category.strip()
            if category and category not in categories:
                categories.append(category)

        entries.append(
            {
                "title": first("TITLE") or "Untitled",
                "date": first("DATE"),
                "status": (first("STATUS") or "publish").lower(),
                "categories": categories,
                "basename": first("BASENAME"),
                "body": body,
                "extended_body": extended_body,
            }
        )

    return entries


def parse_date(value):
    value = str(value).strip()

    if not value:
        STATS["date_failed"] += 1
        warn("  ! DATE field is empty. Using current date.")
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
        STATS["date_failed"] += 1
        warn("  ! Could not parse DATE value: " + repr(value) + ". Using current date.")
        return datetime.now(JST)

    if parsed_dt.tzinfo is None:
        parsed_dt = parsed_dt.replace(tzinfo=JST)
    else:
        parsed_dt = parsed_dt.astimezone(JST)

    return parsed_dt


def slugify(value):
    """ASCII の英数字とハイフンだけのスラッグにする。作れなければ空文字を返す。"""
    value = value.lower()
    value = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE)
    value = re.sub(r"[\s_]+", "-", value)
    value = re.sub(r"[^a-z0-9\-]", "", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value[:80].strip("-")


# ----------------------------------------------------------------------
# HTML -> Markdown
# ----------------------------------------------------------------------

def make_link_paragraph(soup, url):
    paragraph = soup.new_tag("p")
    anchor = soup.new_tag("a", href=url)
    anchor.string = url
    paragraph.append(anchor)
    return paragraph


def preserve_embeds(soup):
    """
    ブログカード等は除去し、YouTube 等の外部 iframe はURLだけリンクとして残す。
    （はてなのブログカードは直後の <cite> にリンクが残るため、iframe 自体は不要）
    """
    for iframe in soup.find_all("iframe"):
        src = (iframe.get("src") or "").strip()

        if src.startswith("//"):
            src = "https:" + src

        if (
            not src.startswith(("http://", "https://"))
            or "hatenablog-parts.com" in src
        ):
            iframe.decompose()
        else:
            iframe.replace_with(make_link_paragraph(soup, src))

    for card in soup.select(".embed-card"):
        link = card.find("a", href=True)

        if link:
            card.replace_with(make_link_paragraph(soup, link["href"]))
        else:
            card.decompose()


def is_image_link(href):
    parsed = urlparse(href)
    return (
        parsed.path.lower().endswith(IMAGE_EXTENSIONS)
        or "st-hatena.com" in parsed.netloc
    )


def unwrap_image_links(soup):
    """画像だけを囲んでいる「元画像へのリンク」を外す（外部URLが残るのを防ぐ）。"""
    for link in soup.find_all("a", href=True):
        if (
            link.find("img")
            and not link.get_text(strip=True)
            and is_image_link(link["href"])
        ):
            link.unwrap()


def clean_hatena_elements(soup):
    preserve_embeds(soup)

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
        replaced, count = FOTO_ID_PATTERN.subn("", text_node)

        if count:
            STATS["foto_removed"] += count

            if not replaced.strip():
                text_node.extract()
            else:
                text_node.replace_with(replaced)

    keep_tags = ["img", "a", "iframe", "hr", "table", "video", "audio", "svg"]

    for element in soup.find_all(["div", "span", "p"]):
        if getattr(element, "decomposed", False):
            continue

        if not element.get_text(strip=True) and not element.find(keep_tags):
            element.decompose()

    return soup


def html_to_markdown(html, cleanup=True):
    soup = BeautifulSoup(html, "html.parser")

    if cleanup:
        soup = clean_hatena_elements(soup)
    else:
        for element in soup(["script", "style"]):
            element.decompose()

    unwrap_image_links(soup)

    markdown = md(str(soup), heading_style="ATX", bullets="-")

    markdown, count = FOTO_ID_PATTERN.subn("", markdown)
    STATS["foto_removed"] += count

    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()

    return markdown + "\n"


# ----------------------------------------------------------------------
# 画像のダウンロード
# ----------------------------------------------------------------------

def candidate_names(stem, suffix, url):
    """元の名前 -> URLハッシュ付き -> 連番付き の順に候補を返す。"""
    yield stem + suffix

    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    hashed = stem + "-" + digest
    yield hashed + suffix

    number = 2
    while True:
        yield hashed + "-" + str(number) + suffix
        number += 1


def download_image(url, folder, session):
    """
    画像を保存してファイル名を返す。失敗時は None。
    同名ファイルが既にあり、内容が同じなら再利用する（再実行しても増殖しない）。
    """
    try:
        if url.startswith("//"):
            url = "https:" + url

        response = session.get(url, timeout=30)
        response.raise_for_status()
        content = response.content

        parsed_url = urlparse(url)
        original_name = Path(unquote(parsed_url.path)).name
        original_name = re.sub(r"[^A-Za-z0-9._\-]", "_", original_name)

        if not original_name or original_name in (".", ".."):
            original_name = "image"

        name_path = Path(original_name)
        stem, suffix = name_path.stem, name_path.suffix

        if not suffix:
            content_type = response.headers.get("content-type", "").split(";")[0].strip()
            suffix = {"image/jpeg": ".jpg"}.get(content_type) or (
                mimetypes.guess_extension(content_type) or ".jpg"
            )

        for name in candidate_names(stem, suffix, url):
            destination = folder / name

            if not destination.exists():
                folder.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
                return name

            if destination.read_bytes() == content:
                return name

    except Exception as error:
        STATS["image_failed"] += 1
        warn("  ! Failed to download image: " + url + " (" + str(error) + ")")
        return None


def localize_images(markdown, post_dir, session, sleep_seconds):
    pattern = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
    cache = {}  # 同じ記事内で同じURLを二度ダウンロードしない

    def replace(match):
        alt = match.group(1)
        url = match.group(2)

        if url.startswith(("data:", "#", "mailto:")):
            return match.group(0)

        if url in cache:
            filename = cache[url]
        else:
            filename = download_image(url, post_dir, session)
            cache[url] = filename

            if filename and sleep_seconds:
                time.sleep(sleep_seconds)

        if not filename:
            return match.group(0)

        # Astroの最適化対象にするため、同じフォルダ内を指す相対パスにする
        return "![" + alt + "](./" + filename + ")"

    return pattern.sub(replace, markdown)


# ----------------------------------------------------------------------
# フロントマター
# ----------------------------------------------------------------------

def escape_yaml(value):
    value = value.replace("\\", "\\\\").replace('"', '\\"')
    return re.sub(r"[\r\n\t]+", " ", value)


def yaml_quote(value):
    return '"' + escape_yaml(value) + '"'


def make_frontmatter(entry, date, is_draft):
    lines = [
        "---",
        "title: " + yaml_quote(entry["title"]),
        'description: ""',
        "pubDate: " + date.replace(microsecond=0).isoformat(),
    ]

    if entry["categories"]:
        tags = ", ".join(yaml_quote(category) for category in entry["categories"])
        lines.append("tags: [" + tags + "]")

    if is_draft:
        lines.append("draft: true")

    lines.extend(["---", "", ""])

    return "\n".join(lines)


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Convert Hatena Blog MT export files into Astro Markdown with Asset Colocation."
    )
    parser.add_argument("--input", required=True, help="Hatena Blog export .txt file")
    parser.add_argument("--astro-dir", default=".", help="Astro project root")
    parser.add_argument("--posts-dir", default="src/content/blog", help="Directory for Content Collections")
    parser.add_argument("--include-drafts", action="store_true", help="Also convert drafts (written with draft: true)")
    parser.add_argument("--drafts-only", action="store_true", help="Convert drafts only (written with draft: true)")
    parser.add_argument("--sleep", type=float, default=0.2, help="Seconds to wait after each image download")
    parser.add_argument("--no-cleanup", action="store_true", help="Skip Hatena-specific element cleanup")
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
        STATS["date_failed"] = 0

    session = requests.Session()
    session.headers["User-Agent"] = "hatena-to-astro/1.1"

    used_slugs = set()
    converted = 0

    for index, entry in enumerate(entries, 1):
        is_draft = entry["status"] != "publish"

        if args.drafts_only and not is_draft:
            print("[" + str(index) + "] Skipped published entry: " + entry["title"])
            continue

        if is_draft and not args.include_drafts and not args.drafts_only:
            print("[" + str(index) + "] Skipped draft: " + entry["title"])
            continue

        date = parse_date(entry["date"])

        # BASENAME -> タイトル -> 日時 の順でスラッグを決める
        base_slug = (
            slugify(entry["basename"])
            or slugify(entry["title"])
            or date.strftime("%Y%m%d%H%M%S")
        )
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

        # 記事ごとにフォルダを切り（Leaf Bundle）、画像と index.md を同じ場所に置く
        post_dir = posts_dir / slug

        markdown = localize_images(markdown, post_dir, session, args.sleep)

        post_dir.mkdir(parents=True, exist_ok=True)
        output_path = post_dir / "index.md"

        output_path.write_text(
            make_frontmatter(entry, date, is_draft) + markdown,
            encoding="utf-8",
        )

        converted += 1
        print("  -> " + str(output_path.relative_to(astro_dir)))

    print("Done. Converted " + str(converted) + " entries.")

    if STATS["date_failed"]:
        warn("! " + str(STATS["date_failed"]) + " entries had an unreadable DATE (current date was used).")

    if STATS["image_failed"]:
        warn("! " + str(STATS["image_failed"]) + " images failed to download (remote URLs were kept).")

    if STATS["foto_removed"]:
        warn(
            "! " + str(STATS["foto_removed"])
            + " Hatena Fotolife notations (f:id:...) were removed. "
            "Those images could not be resolved to a URL; please check the posts."
        )


if __name__ == "__main__":
    main()