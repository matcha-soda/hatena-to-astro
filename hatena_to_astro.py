#!/usr/bin/env python3
"""
Convert Hatena Blog MT export files into Astro-compatible Markdown.

- Converts entry HTML to Markdown.
- Downloads images into post-specific subdirectories (Asset Colocation / Leaf Bundle)
  so that Astro's built-in image optimization can be used.
- Each post is written to converted/<slug>/index.md, in a `converted`
  folder created in the current directory.
- Removes common Hatena-specific elements (stars, keyword links, ads, blog cards).
- The Hatena-generated table of contents is removed unless --keep-toc is given.
- Keeps the URL of non-Hatena iframe embeds (e.g. YouTube) as a plain link.
- Writes dates in JST (ISO 8601) and `draft: true` for non-published entries.
- MT sections (BODY / EXTENDED BODY / EXCERPT / ...) are matched by their
  label lines, not by position, so a `-----` line inside the body text is
  kept as content and an EXCERPT is never mistaken for the extended body.
- Writes redirect-map.csv (BASENAME -> slug) into the converted folder.
- Run without --input (or with --interactive) to use the interactive wizard.
"""

OUTPUT_DIR_NAME = "converted"

import argparse
import csv
import hashlib
import re
import sys
import time
from dataclasses import dataclass, field
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

# iframe / .embed-card は preserve_embeds() で個別に処理する。
# .hatena-asin-detail も clean_hatena_elements() で個別に処理する（リンクを残す）。
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
]

HATENA_URL_PATTERNS = [
    r"https?://(?:www\.)?hatena\.ne\.jp/",
    r"https?://b\.hatena\.ne\.jp/",
    r"https?://s\.hatena\.ne\.jp/",
    r"https?://f\.hatena\.ne\.jp/",
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

# セクションの開始として扱う見出し行。既知のラベルに加え、カスタムフィールド
# （CF...: など）のような大文字ラベルの行も区切りとみなし、未知のセクションが
# 本文に混入しないようにする。これ以外の「-----」行は本文の一部とみなす。
SECTION_SPLIT_PATTERN = re.compile(
    r"^([A-Z][A-Z0-9_ ]*|(?i:BODY|EXTENDED BODY|EXCERPT|KEYWORDS|COMMENT|PING))\s*[:：]\s*$"
)

# ヘッダのフィールド行（「TITLE: xxx」。全角コロンもキー直後のみ許容）
HEADER_FIELD_PATTERN = re.compile(r"^([A-Za-z0-9 _\-]+)[:：]\s?(.*)$")

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".avif")

# 実行結果のサマリ表示用
STATS = {
    "foto_removed": 0,
    "date_failed": 0,
    "image_failed": 0,
    "blocks_total": 0,
    "blocks_skipped": 0,
}

# 日付を読めなかった記事のタイトル（最後にまとめて表示する）
FAILED_DATE_TITLES = []

# ダウンロードできなかった画像（記事タイトルとURL。最後にまとめて表示する）
FAILED_IMAGES = []

# 変換中にエラーが発生した記事（タイトルと理由。最後にまとめて表示する）
FAILED_ENTRIES = []


@dataclass
class Entry:
    title: str = "Untitled"
    date: str = ""
    status: str = "publish"
    categories: list = field(default_factory=list)
    basename: str = ""
    author: str = ""
    body: str = ""
    extended_body: str = ""
    excerpt: str = ""


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


def split_sections(lines):
    """
    ヘッダ以降の行を、見出し行（BODY: など）を手がかりにセクションへ分ける。
    「-----」の直後が見出し行のときだけ区切りとみなし、それ以外の
    「-----」行は本文の一部として残す。
    """
    sections = {}
    current_label = "BODY"
    buffer = []

    def flush():
        sections.setdefault(current_label, []).extend(buffer)
        buffer.clear()

    total = len(lines)
    index = 0

    while index < total and not lines[index].strip():
        index += 1

    if index < total:
        first_match = SECTION_SPLIT_PATTERN.match(lines[index].strip())
        if first_match:
            current_label = first_match.group(1).strip().upper()
            index += 1

    while index < total:
        line = lines[index]

        if line.strip() == "-----":
            lookahead = index + 1
            while lookahead < total and not lines[lookahead].strip():
                lookahead += 1

            match = None
            if lookahead < total:
                match = SECTION_SPLIT_PATTERN.match(lines[lookahead].strip())

            if match:
                flush()
                current_label = match.group(1).strip().upper()
                index = lookahead + 1
                continue

        buffer.append(line)
        index += 1

    flush()

    return {
        label: "\n".join(section_lines)
        for label, section_lines in sections.items()
    }


def parse_tag_list(value):
    """TAGS フィールド（カンマ区切り、引用符つき要素あり）を分解する。"""
    try:
        row = next(csv.reader([value], skipinitialspace=True))
    except csv.Error:
        row = value.split(",")

    return [item.strip() for item in row if item.strip()]


def parse_entries(path):
    raw_text = path.read_text(encoding="utf-8-sig", errors="replace")
    text = raw_text.replace("\r\n", "\n").replace("\r", "\n")

    entries = []

    for block in re.split(r"(?m)^--------\s*$", text):
        if not block.strip():
            continue

        STATS["blocks_total"] += 1
        lines = block.splitlines()
        preview = next((line.strip() for line in lines if line.strip()), "")[:40]

        separator_index = None
        for i, line in enumerate(lines):
            if line.strip() == "-----":
                separator_index = i
                break

        if separator_index is None:
            STATS["blocks_skipped"] += 1
            warn("  ! ヘッダの区切りがないため、ブロックをスキップしました: " + preview)
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
            STATS["blocks_skipped"] += 1
            warn("  ! TITLE がないため、ブロックをスキップしました: " + preview)
            continue

        def first(key, default=""):
            values = fields.get(key)
            return values[0] if values else default

        sections = split_sections(remaining_lines)

        categories = []
        for category in fields.get("CATEGORY", []):
            category = category.strip()
            if category and category not in categories:
                categories.append(category)

        primary_category = first("PRIMARY CATEGORY")
        if primary_category and primary_category not in categories:
            categories.insert(0, primary_category)

        for tags_field in fields.get("TAGS", []):
            for tag in parse_tag_list(tags_field):
                if tag not in categories:
                    categories.append(tag)

        entries.append(
            Entry(
                title=first("TITLE") or "Untitled",
                date=first("DATE"),
                status=(first("STATUS") or "publish").lower(),
                categories=categories,
                basename=first("BASENAME"),
                author=first("AUTHOR"),
                body=clean_section(sections.get("BODY", "")),
                extended_body=clean_section(sections.get("EXTENDED BODY", "")),
                excerpt=clean_section(sections.get("EXCERPT", "")),
            )
        )

    return entries


def parse_date(value, title="", count_failure=True):
    value = str(value).strip()

    if not value:
        if count_failure:
            STATS["date_failed"] += 1
            if title:
                FAILED_DATE_TITLES.append(title)
        warn("  ! DATE が空のため、現在の日時を使います。")
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
        if count_failure:
            STATS["date_failed"] += 1
            if title:
                FAILED_DATE_TITLES.append(title)
        warn("  ! DATE を読み取れませんでした: " + repr(value) + "。現在の日時を使います。")
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
    if parsed.path.lower().endswith(IMAGE_EXTENSIONS):
        return True
    # 拡張子のないフォトライフ画像URL（/images/ 配下）だけを画像として扱う
    return parsed.netloc.endswith("st-hatena.com") and "/images/" in parsed.path


def unwrap_image_links(soup):
    """画像だけを囲んでいる「元画像へのリンク」を外す（外部URLが残るのを防ぐ）。"""
    for link in soup.find_all("a", href=True):
        if (
            link.find("img")
            and not link.get_text(strip=True)
            and is_image_link(link["href"])
        ):
            link.unwrap()


def fill_image_alts(soup):
    """figure の figcaption を、alt が空の img の alt に使う。"""
    for figure in soup.find_all("figure"):
        caption = figure.find("figcaption")

        if caption is None:
            continue

        text = caption.get_text(strip=True)
        image = figure.find("img")

        if text and image is not None and not (image.get("alt") or "").strip():
            image["alt"] = text


def clean_hatena_elements(soup, remove_toc=True):
    preserve_embeds(soup)

    if remove_toc:
        for toc in soup.select(".table-of-contents"):
            toc.decompose()

    # Amazon商品表示は、商品へのリンクだけを残して置き換える
    for detail in soup.select(".hatena-asin-detail"):
        link = detail.find("a", href=True)

        if link:
            detail.replace_with(make_link_paragraph(soup, link["href"]))
        else:
            detail.decompose()

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


def html_to_markdown(html, cleanup=True, remove_toc=True):
    soup = BeautifulSoup(html, "html.parser")

    fill_image_alts(soup)

    if cleanup:
        soup = clean_hatena_elements(soup, remove_toc=remove_toc)
    else:
        for element in soup(["script", "style"]):
            element.decompose()

        # --no-cleanup でも埋め込みはリンクとして残す（markdownify が iframe を落とすため）
        preserve_embeds(soup)

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


def sniff_image_suffix(content):
    """中身の先頭バイトから画像形式を推定する。画像らしくなければ None。"""
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"

    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"

    if content.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"

    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"

    if content.startswith(b"BM"):
        return ".bmp"

    if content[4:8] == b"ftyp" and content[8:12] in (b"avif", b"avis"):
        return ".avif"

    if content.lstrip()[:1] == b"<" and b"<svg" in content[:1024]:
        return ".svg"

    return None


def download_image(url, folder, session, title=""):
    """
    画像を保存してファイル名を返す。失敗時は None。
    同名ファイルが既にあり、内容が同じなら再利用する（再実行しても増殖しない）。
    HTMLが返ってきた場合は画像ではないので失敗として扱う。
    """
    try:
        if url.startswith("//"):
            url = "https:" + url

        response = session.get(url, timeout=30)
        response.raise_for_status()
        content = response.content

        content_type = (
            response.headers.get("content-type", "").split(";")[0].strip().lower()
        )

        if content_type.startswith("text/html"):
            raise ValueError("画像ではなく HTML が返ってきました")

        parsed_url = urlparse(url)
        original_name = Path(unquote(parsed_url.path)).name
        original_name = re.sub(r"[^A-Za-z0-9._\-]", "_", original_name)

        if not original_name or original_name in (".", ".."):
            original_name = "image"

        name_path = Path(original_name)
        stem, suffix = name_path.stem, name_path.suffix

        # ヘッダが画像を名乗っていても、中身が画像でなければ失敗として扱う
        sniffed = sniff_image_suffix(content)

        if sniffed is None:
            raise ValueError("中身が画像ではありませんでした")

        if suffix.lower() not in IMAGE_EXTENSIONS:
            suffix = sniffed
        else:
            normalized = ".jpg" if suffix.lower() == ".jpeg" else suffix.lower()
            if sniffed != normalized:
                suffix = sniffed

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
        FAILED_IMAGES.append((title, url))
        warn("  ! 画像をダウンロードできませんでした: " + url + "（" + str(error) + "）")
        return None


def localize_images(markdown, post_dir, session, sleep_seconds, title=""):
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
            filename = download_image(url, post_dir, session, title=title)
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


def make_frontmatter(
    entry,
    date,
    is_draft,
    slug="",
    include_slug=False,
    include_author=False,
    author_name="",
):
    lines = [
        "---",
        "title: " + yaml_quote(entry.title),
        "pubDate: " + date.replace(microsecond=0).isoformat(),
    ]

    if include_slug and slug:
        lines.append("slug: " + yaml_quote(slug))

    author_value = author_name.strip() or (entry.author.strip() if include_author else "")
    if author_value:
        lines.append("author: " + yaml_quote(author_value))

    if entry.categories:
        tags = ", ".join(yaml_quote(category) for category in entry.categories)
        lines.append("tags: [" + tags + "]")

    if is_draft:
        lines.append("draft: true")

    lines.extend(["---", "", ""])

    return "\n".join(lines)


# ----------------------------------------------------------------------
# 対話モードと設定の解決
# ----------------------------------------------------------------------

def clean_path_text(text):
    """ドラッグ＆ドロップ等で付いた引用符と空白を取り除く。"""
    text = text.strip()

    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1]

    return text.strip()


def ask_text(question, default=""):
    suffix = " [" + default + "]" if default else ""
    try:
        answer = input(question + suffix + ": ")
    except (EOFError, KeyboardInterrupt):
        return None
    return answer.strip() or default


def ask_yes_no(question, default=False):
    hint = "[Y/n]" if default else "[y/N]"
    prompt = (question + " " if question else "") + hint + ": "
    while True:
        try:
            answer = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("y か n で答えてください。")


def ask_choice(question, options, default_index=0):
    print(question)
    for number, label in enumerate(options, 1):
        marker = "（既定）" if number - 1 == default_index else ""
        print("  " + str(number) + ". " + label + marker)
    while True:
        try:
            answer = input("番号を入力してください [" + str(default_index + 1) + "]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not answer:
            return default_index
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer) - 1
        print("1 から " + str(len(options)) + " の番号で答えてください。")


def prompt_settings():
    """対話モードで設定を聞く。キャンセル時は None。"""
    print("hatena-to-astro 対話モード")
    print("分からないところは、そのまま Enter を押せば既定値が使われます。")
    print()

    input_path = None
    while input_path is None:
        raw = ask_text("エクスポートファイル（.txt）のパス")
        if raw is None or not raw:
            return None
        candidate = Path(clean_path_text(raw)).expanduser()
        if candidate.is_file():
            input_path = candidate
        else:
            print("ファイルが見つかりません: " + str(candidate))
            print("もう一度入力してください。")

    output_dir = (Path.cwd() / OUTPUT_DIR_NAME).resolve()

    print()
    choice = ask_choice(
        "どの記事を変換しますか。",
        ["公開記事のみ", "下書きも含める", "下書きのみ"],
        0,
    )
    if choice is None:
        return None

    print()
    print("挿入されている目次を引き継ぎますか。")
    print("見出しから自動生成された目次です。")
    keep_toc = ask_yes_no("", False)
    if keep_toc is None:
        return None
    remove_toc = not keep_toc

    print()
    print("記事のURL名を引き継ぎますか。")
    print("元のURL名を slug としてフロントマターに書きます。")
    include_slug = ask_yes_no("", False)
    if include_slug is None:
        return None

    print()
    print("著者名を引き継ぎますか。")
    print("エクスポートの著者名を author としてフロントマターに書きます。")
    include_author = ask_yes_no("", False)
    if include_author is None:
        return None

    author_name = ""
    if include_author:
        print()
        print("author に設定する名前を入力してください。")
        print("空欄のまま Enter を押すと、エクスポートの著者名をそのまま使います。")
        author_name = ask_text("名前")
        if author_name is None:
            return None

    print()
    print("入力ファイル: " + str(input_path))
    print("出力先: " + str(output_dir))
    print("対象: " + ["公開記事のみ", "下書きも含める", "下書きのみ"][choice])
    print("目次: " + ("引き継ぐ" if keep_toc else "引き継がない（削除する）"))
    print("URL名: " + ("フロントマターにも引き継ぐ" if include_slug else "フォルダ名のみ"))
    if include_author:
        print("author: " + (author_name if author_name else "エクスポートの著者名を引き継ぐ"))
    else:
        print("author: 含めない")

    print()
    if not ask_yes_no("この内容で変換しますか", True):
        return None

    return argparse.Namespace(
        input_path=input_path.resolve(),
        output_dir=output_dir,
        include_drafts=choice == 1,
        drafts_only=choice == 2,
        sleep=0.2,
        no_cleanup=False,
        remove_toc=remove_toc,
        include_slug=include_slug,
        include_author=include_author,
        author_name=author_name,
        debug_date=False,
    )


def settings_from_args(args):
    """コマンドライン引数から設定を組み立てる。出力先は converted フォルダ固定。"""
    return argparse.Namespace(
        input_path=Path(args.input).expanduser().resolve(),
        output_dir=(Path.cwd() / OUTPUT_DIR_NAME).resolve(),
        include_drafts=args.include_drafts,
        drafts_only=args.drafts_only,
        sleep=args.sleep,
        no_cleanup=args.no_cleanup,
        remove_toc=not args.keep_toc,
        include_slug=args.set_slug,
        include_author=args.include_author or bool(args.author),
        author_name=args.author or "",
        debug_date=args.debug_date,
    )


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="はてなブログのエクスポートファイル（MT形式）を Astro 用の Markdown に変換します"
    )
    parser.add_argument("--input", help="はてなブログからエクスポートした .txt ファイル（省略すると対話モードが始まります）")
    parser.add_argument("--interactive", action="store_true", help="対話モードで起動する")
    parser.add_argument("--include-drafts", action="store_true", help="下書きも変換する（draft: true を付けて出力）")
    parser.add_argument("--drafts-only", action="store_true", help="下書きだけを変換する（draft: true を付けて出力）")
    parser.add_argument("--sleep", type=float, default=0.2, help="画像ダウンロード後の待機時間（秒）")
    parser.add_argument("--no-cleanup", action="store_true", help="はてな固有の要素の削除を一部無効化する")
    parser.add_argument("--keep-toc", action="store_true", help="見出しから自動生成された目次を引き継ぐ（既定では削除します）")
    parser.add_argument("--set-slug", action="store_true", help="slug をフロントマターにも書く")
    parser.add_argument("--include-author", action="store_true", help="author をフロントマターに書く（各記事の AUTHOR を使用）")
    parser.add_argument("--author", default=None, metavar="NAME", help="author として指定した名前を全記事に書く（--include-author を兼ねます）")
    parser.add_argument("--debug-date", action="store_true")
    args = parser.parse_args()

    if args.interactive or not args.input:
        settings = prompt_settings()

        if settings is None:
            print("キャンセルしました。")
            return 2
    else:
        settings = settings_from_args(args)

    if not settings.input_path.is_file():
        warn("入力ファイルが見つかりません: " + str(settings.input_path))
        return 2

    posts_dir = settings.output_dir
    posts_dir.mkdir(parents=True, exist_ok=True)

    entries = parse_entries(settings.input_path)
    print(str(len(entries)) + " 件の記事が見つかりました。")

    if settings.debug_date and entries:
        print("最初の記事の DATE:", repr(entries[0].date))
        print("解析した日付:", parse_date(entries[0].date, count_failure=False))

    session = requests.Session()
    session.headers["User-Agent"] = "hatena-to-astro/2.0"

    used_slugs = set()
    converted = 0
    overwrote = 0
    redirect_rows = []

    for index, entry in enumerate(entries, 1):
        is_draft = entry.status != "publish"

        if settings.drafts_only and not is_draft:
            print("[" + str(index) + "] 公開記事のためスキップしました: " + entry.title)
            continue

        if is_draft and not settings.include_drafts and not settings.drafts_only:
            print("[" + str(index) + "] 下書きのためスキップしました: " + entry.title)
            continue

        date = parse_date(entry.date, title=entry.title)

        # BASENAME -> タイトル -> 日時 の順でスラッグを決める
        base_slug = (
            slugify(entry.basename)
            or slugify(entry.title)
            or date.strftime("%Y%m%d%H%M%S")
        )
        slug = base_slug
        number = 2

        while slug in used_slugs:
            slug = base_slug + "-" + str(number)
            number += 1

        used_slugs.add(slug)

        print("[" + str(index) + "] 変換しています: " + entry.title)

        try:
            html = entry.body

            if entry.extended_body:
                html += "\n" + entry.extended_body

            markdown = html_to_markdown(
                html, cleanup=not settings.no_cleanup, remove_toc=settings.remove_toc
            )

            # 記事ごとにフォルダを切り（Leaf Bundle）、画像と index.md を同じ場所に置く
            post_dir = posts_dir / slug

            markdown = localize_images(
                markdown, post_dir, session, settings.sleep, title=entry.title
            )

            post_dir.mkdir(parents=True, exist_ok=True)
            output_path = post_dir / "index.md"

            if output_path.exists():
                overwrote += 1

            output_path.write_text(
                make_frontmatter(
                    entry,
                    date,
                    is_draft,
                    slug=slug,
                    include_slug=settings.include_slug,
                    include_author=settings.include_author,
                    author_name=settings.author_name,
                )
                + markdown,
                encoding="utf-8",
            )
        except Exception as error:
            FAILED_ENTRIES.append((entry.title, str(error)))
            warn(
                "  ! エラーが発生したため、この記事をスキップしました（"
                + str(error) + "）"
            )
            continue

        if entry.basename:
            redirect_rows.append([entry.basename, slug, entry.title])

        converted += 1

        try:
            shown_path = output_path.relative_to(Path.cwd())
        except ValueError:
            shown_path = output_path

        print("  -> " + str(shown_path))

    print("完了しました。" + str(converted) + " 件の記事を変換しました。")

    if overwrote:
        print(str(overwrote) + " 件の既存の記事を上書きしました。")

    if redirect_rows:
        map_path = posts_dir / "redirect-map.csv"
        with map_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["basename", "slug", "title"])
            writer.writerows(redirect_rows)
        print("リダイレクト対応表を出力しました: " + str(map_path))

    if STATS["blocks_skipped"]:
        warn(
            "! 全 " + str(STATS["blocks_total"]) + " ブロックのうち "
            + str(STATS["blocks_skipped"])
            + " 件は、記事として認識できなかったためスキップしました。"
        )

    if STATS["date_failed"]:
        warn("! " + str(STATS["date_failed"]) + " 件の記事で DATE を読み取れなかったため、現在の日時を使いました:")
        for failed_title in FAILED_DATE_TITLES:
            warn("    - " + failed_title)

    if FAILED_ENTRIES:
        warn("! " + str(len(FAILED_ENTRIES)) + " 件の記事でエラーが発生し、変換できませんでした:")
        for failed_title, reason in FAILED_ENTRIES:
            warn("    - " + failed_title + "（" + reason + "）")

    if STATS["image_failed"]:
        warn("! " + str(STATS["image_failed"]) + " 件の画像をダウンロードできませんでした（元の URL のまま残しています）:")
        for failed_title, failed_url in FAILED_IMAGES:
            if failed_title:
                warn("    - " + failed_title + ": " + failed_url)
            else:
                warn("    - " + failed_url)

    if STATS["foto_removed"]:
        warn(
            "! はてなフォトライフ記法（f:id:...）を " + str(STATS["foto_removed"])
            + " 件取り除きました。URL に変換できない画像のため、記事を確認してください。"
        )

    if STATS["date_failed"] or STATS["blocks_skipped"] or FAILED_ENTRIES:
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
