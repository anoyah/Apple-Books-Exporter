#!/usr/bin/env python3
"""
Apple Books Exporter
- Scans common macOS Apple Books storage locations.
- Exports imported EPUB/PDF files without modifying the source library.
- Repackages Apple-style *.epub directories as valid EPUB archives.
- Extracts EPUB images and PDF-embedded raster images.
- Writes catalog.json and catalog.csv.

Requires Python 3.10+.
For PDF metadata and image extraction: python -m pip install pymupdf
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import posixpath
import re
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote
import xml.etree.ElementTree as ET

try:
    import pymupdf as fitz
except ImportError:
    try:
        import fitz  # type: ignore
    except ImportError:
        fitz = None


HOME = Path.home()
DEFAULT_ROOTS = [
    HOME / "Library/Containers/com.apple.BKAgentService/Data/Documents/iBooks/Books",
    HOME / "Library/Mobile Documents/iCloud~com~apple~iBooks/Documents",
    HOME / "Library/Containers/com.apple.BKBookstore/Data/Documents/iBooks/Books",
]
DEFAULT_OUTPUT = HOME / "BooksExport" / "AppleBooks-Export"

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp",
    ".tif", ".tiff", ".avif", ".heic"
}


def log_setup(output: Path) -> None:
    log_dir = output / "Logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_dir / "export.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def stable_id(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:10]


def safe_name(value: str, limit: int = 100) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    value = value[:limit].strip(" .")
    return value or "Untitled"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def scan_sources(roots: list[Path], output: Path) -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    seen: set[str] = set()

    for root in roots:
        if not root.exists():
            logging.info("目录不存在，跳过：%s", root)
            continue

        for current, dirs, files in os.walk(root):
            current_path = Path(current)

            # Do not scan the export folder if the user placed it under a source root.
            dirs[:] = [
                d for d in dirs
                if not is_inside(current_path / d, output)
                and d not in {".Trash"}
            ]

            # Apple/iCloud may store an EPUB as a directory named "Book.epub".
            for dirname in list(dirs):
                candidate = current_path / dirname
                if dirname.lower().endswith(".epub"):
                    key = str(candidate.resolve())
                    if key not in seen:
                        found.append(("epub_dir", candidate))
                        seen.add(key)
                    dirs.remove(dirname)

            for filename in files:
                candidate = current_path / filename
                suffix = candidate.suffix.lower()
                if suffix not in {".epub", ".pdf"}:
                    continue
                key = str(candidate.resolve())
                if key in seen:
                    continue
                if is_inside(candidate, output):
                    continue
                found.append(("epub_file" if suffix == ".epub" else "pdf", candidate))
                seen.add(key)

    return sorted(found, key=lambda item: (item[0], str(item[1]).lower()))


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def epub_members(source: Path) -> list[str]:
    if source.is_dir():
        result = []
        for item in source.rglob("*"):
            if item.is_file() and not item.is_symlink():
                result.append(item.relative_to(source).as_posix())
        return result
    with zipfile.ZipFile(source, "r") as zf:
        return zf.namelist()


def read_epub_member(source: Path, member: str) -> bytes:
    if source.is_dir():
        safe = PurePosixPath(member)
        if safe.is_absolute() or ".." in safe.parts:
            raise ValueError(f"非法 EPUB 内部路径：{member}")
        return (source / Path(*safe.parts)).read_bytes()
    with zipfile.ZipFile(source, "r") as zf:
        return zf.read(member)


def parse_epub_metadata(source: Path) -> tuple[dict[str, Any], str | None, list[str]]:
    names = epub_members(source)
    lower_names = {name.lower(): name for name in names}
    opf_path = None

    container_name = lower_names.get("meta-inf/container.xml")
    if container_name:
        try:
            root = ET.fromstring(read_epub_member(source, container_name))
            for node in root.iter():
                if local_name(node.tag) == "rootfile":
                    opf_path = node.attrib.get("full-path")
                    if opf_path:
                        break
        except Exception:
            pass

    if not opf_path:
        opf_path = next((n for n in names if n.lower().endswith(".opf")), None)

    meta: dict[str, Any] = {
        "title": None, "creators": [], "language": None, "identifier": None,
        "publisher": None, "date": None, "description": None, "subjects": [],
    }
    image_names: list[str] = []

    if not opf_path:
        return meta, None, image_names

    opf_path = opf_path.replace("\\", "/")
    try:
        opf_root = ET.fromstring(read_epub_member(source, opf_path))
    except Exception:
        return meta, opf_path, image_names

    metadata_node = next((n for n in opf_root.iter() if local_name(n.tag) == "metadata"), opf_root)
    for node in metadata_node.iter():
        tag = local_name(node.tag)
        value = (node.text or "").strip()
        if not value:
            continue
        if tag == "title" and not meta["title"]:
            meta["title"] = value
        elif tag == "creator":
            meta["creators"].append(value)
        elif tag == "language" and not meta["language"]:
            meta["language"] = value
        elif tag == "identifier" and not meta["identifier"]:
            meta["identifier"] = value
        elif tag == "publisher" and not meta["publisher"]:
            meta["publisher"] = value
        elif tag == "date" and not meta["date"]:
            meta["date"] = value
        elif tag == "description" and not meta["description"]:
            meta["description"] = value
        elif tag == "subject":
            meta["subjects"].append(value)

    cover_id = None
    manifest: dict[str, tuple[str, str]] = {}
    for node in opf_root.iter():
        if local_name(node.tag) == "meta":
            if node.attrib.get("name", "").lower() == "cover":
                cover_id = node.attrib.get("content")
        if local_name(node.tag) == "item":
            item_id = node.attrib.get("id")
            href = node.attrib.get("href")
            media = node.attrib.get("media-type", "").lower()
            props = node.attrib.get("properties", "").lower().split()
            if item_id and href:
                manifest[item_id] = (href, media)
                if "cover-image" in props:
                    cover_id = item_id

    opf_dir = posixpath.dirname(opf_path)
    for item_id, (href, media) in manifest.items():
        decoded = unquote(href.split("#", 1)[0].split("?", 1)[0])
        resolved = posixpath.normpath(posixpath.join(opf_dir, decoded))
        ext = PurePosixPath(resolved).suffix.lower()
        if media.startswith("image/") or ext in IMAGE_EXTS:
            if not resolved.startswith("../") and not resolved.startswith("/"):
                image_names.append(resolved)

    if cover_id and cover_id in manifest:
        href = unquote(manifest[cover_id][0].split("#", 1)[0].split("?", 1)[0])
        cover = posixpath.normpath(posixpath.join(opf_dir, href))
        meta["cover_path"] = cover

    return meta, opf_path, sorted(set(image_names))


def package_epub_directory(source: Path, destination: Path) -> None:
    names = epub_members(source)
    lower = {n.lower(): n for n in names}
    mimetype_name = lower.get("mimetype")
    if not mimetype_name:
        raise ValueError("EPUB 目录缺少根目录 mimetype 文件，可能尚未完整下载")

    mimetype = read_epub_member(source, mimetype_name)
    if mimetype.strip() != b"application/epub+zip":
        raise ValueError("EPUB 的 mimetype 内容不正确")

    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w") as zf:
        zf.writestr("mimetype", mimetype, compress_type=zipfile.ZIP_STORED)
        for name in sorted(names):
            if name == mimetype_name:
                continue
            pure = PurePosixPath(name)
            if pure.is_absolute() or ".." in pure.parts:
                continue
            # Remove only Apple bookkeeping at the package root.
            if len(pure.parts) == 1:
                base = pure.name.lower()
                if base == "itunesmetadata.plist" or base.startswith("bookmarks"):
                    continue
            if pure.name == ".DS_Store":
                continue
            data = read_epub_member(source, name)
            zf.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)


def safe_asset_path(base: Path, member: str) -> Path | None:
    pure = PurePosixPath(member)
    if pure.is_absolute() or ".." in pure.parts:
        return None
    target = base.joinpath(*pure.parts)
    if not is_inside(target, base):
        return None
    return target


def extract_epub_images(source: Path, asset_root: Path, image_names: list[str]) -> list[str]:
    extracted: list[str] = []
    for member in image_names:
        target = safe_asset_path(asset_root / "EPUB", member)
        if target is None:
            continue
        try:
            data = read_epub_member(source, member)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            extracted.append(str(target))
        except Exception as exc:
            logging.warning("EPUB 图片提取失败 %s：%s", member, exc)
    return extracted


def read_pdf(source: Path) -> tuple[dict[str, Any], int | None, list[str]]:
    metadata: dict[str, Any] = {}
    page_count = None
    image_xrefs: list[str] = []

    if fitz is None:
        return metadata, page_count, image_xrefs

    try:
        with fitz.open(source) as doc:
            metadata = doc.metadata or {}
            page_count = doc.page_count
    except Exception as exc:
        logging.warning("PDF 元数据读取失败 %s：%s", source, exc)

    return metadata, page_count, image_xrefs


def extract_pdf_images(source: Path, asset_root: Path) -> list[str]:
    if fitz is None:
        return []
    extracted: list[str] = []
    seen_xrefs: set[int] = set()
    try:
        with fitz.open(source) as doc:
            if doc.needs_pass:
                logging.warning("PDF 已加密，跳过图片提取：%s", source)
                return []
            for page_index in range(doc.page_count):
                page = doc.load_page(page_index)
                for image in page.get_images(full=True):
                    xref = image[0]
                    if xref in seen_xrefs:
                        continue
                    seen_xrefs.add(xref)
                    try:
                        item = doc.extract_image(xref)
                        data = item.get("image")
                        ext = item.get("ext", "bin")
                        if not data:
                            continue
                        target = asset_root / "PDF" / f"image_{len(extracted)+1:04d}.{ext}"
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(data)
                        extracted.append(str(target))
                    except Exception as exc:
                        logging.warning("PDF 图片提取失败 xref=%s：%s", xref, exc)
    except Exception as exc:
        logging.warning("PDF 图片提取失败 %s：%s", source, exc)
    return extracted


def write_catalog(output: Path, records: list[dict[str, Any]]) -> None:
    metadata_dir = output / "Metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)

    json_path = metadata_dir / "catalog.json"
    json_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "count": len(records),
                "books": records,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    csv_path = metadata_dir / "catalog.csv"
    columns = [
        "id", "format", "title", "authors", "language", "publisher",
        "date", "identifier", "description", "subjects", "page_count",
        "image_count", "source_path", "exported_path", "sha256", "status",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            row = dict(record)
            row["authors"] = "; ".join(row.get("creators") or [])
            row["subjects"] = "; ".join(row.get("subjects") or [])
            writer.writerow(row)

    logging.info("清单已写入：%s", json_path)
    logging.info("清单已写入：%s", csv_path)


def export_one(kind: str, source: Path, output: Path, force: bool, extract_images: bool) -> dict[str, Any]:
    book_id = stable_id(source)
    record: dict[str, Any] = {
        "id": book_id,
        "format": "EPUB" if kind.startswith("epub") else "PDF",
        "source_path": str(source),
        "creators": [],
        "subjects": [],
        "image_count": 0,
        "status": "pending",
    }

    epub_meta: dict[str, Any] = {}
    epub_images: list[str] = []
    pdf_meta: dict[str, Any] = {}
    page_count = None

    if kind.startswith("epub"):
        try:
            epub_meta, _, epub_images = parse_epub_metadata(source)
        except Exception as exc:
            logging.warning("EPUB 元数据读取失败 %s：%s", source, exc)
        title = epub_meta.get("title") or source.stem
        ext = ".epub"
    else:
        pdf_meta, page_count, _ = read_pdf(source)
        title = pdf_meta.get("title") or source.stem
        ext = ".pdf"

    filename = f"{safe_name(str(title))}--{book_id}{ext}"
    category = "EPUB" if kind.startswith("epub") else "PDF"
    destination = output / category / filename
    asset_root = output / "Images" / f"{safe_name(str(title))}--{book_id}"

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not force:
        logging.info("已存在，跳过文件复制：%s", destination)
        record["status"] = "already_exists"
    else:
        if kind == "epub_dir":
            package_epub_directory(source, destination)
        elif kind == "epub_file":
            if not zipfile.is_zipfile(source):
                raise ValueError("EPUB 文件不是有效 ZIP/EPUB，可能文件损坏或未完整下载")
            with zipfile.ZipFile(source, "r") as zf:
                bad = zf.testzip()
                if bad:
                    raise ValueError(f"EPUB 压缩包校验失败：{bad}")
            shutil.copy2(source, destination)
        else:
            shutil.copy2(source, destination)
        record["status"] = "exported"

    extracted: list[str] = []
    if extract_images:
        if kind.startswith("epub"):
            extracted = extract_epub_images(source, asset_root, epub_images)
        elif fitz is not None:
            extracted = extract_pdf_images(source, asset_root)
        else:
            logging.warning("未安装 PyMuPDF，无法提取 PDF 图片")

    record.update({
        "title": str(title),
        "creators": epub_meta.get("creators") or ([pdf_meta.get("author")] if pdf_meta.get("author") else []),
        "language": epub_meta.get("language"),
        "identifier": epub_meta.get("identifier"),
        "publisher": epub_meta.get("publisher"),
        "date": epub_meta.get("date"),
        "description": epub_meta.get("description") or pdf_meta.get("subject"),
        "subjects": epub_meta.get("subjects") or ([pdf_meta.get("keywords")] if pdf_meta.get("keywords") else []),
        "page_count": page_count,
        "pdf_metadata": pdf_meta if kind == "pdf" else None,
        "image_count": len(extracted),
        "images": [str(Path(p).relative_to(output)) for p in extracted],
        "exported_path": str(destination.relative_to(output)) if destination.exists() else None,
        "sha256": sha256_file(destination) if destination.exists() else None,
    })
    return record


def main() -> int:
    parser = argparse.ArgumentParser(
        description="批量导出 Apple Books 中自己导入的 EPUB/PDF，并提取图片和元数据。"
    )
    parser.add_argument(
        "--roots", nargs="+", type=Path, default=DEFAULT_ROOTS,
        help="自定义扫描目录；可以指定多个目录",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="导出目录")
    parser.add_argument("--dry-run", action="store_true", help="只扫描和列出文件，不写出资源")
    parser.add_argument("--force", action="store_true", help="覆盖本工具导出目录中已存在的同名结果")
    parser.add_argument("--no-images", action="store_true", help="不提取 EPUB/PDF 图片")
    args = parser.parse_args()

    roots = [p.expanduser() for p in args.roots]
    output = args.output.expanduser()

    if not args.dry_run:
        output.mkdir(parents=True, exist_ok=True)
        log_setup(output)

    sources = scan_sources(roots, output)
    print(f"扫描到 {len(sources)} 个候选资源。")
    for kind, path in sources:
        print(f"[{kind}] {path}")

    if args.dry_run:
        print("\nDry run：未复制、解包或修改任何文件。")
        return 0

    if not sources:
        logging.warning("没有找到 EPUB/PDF。请检查 --roots 路径，或先在 Books 中下载 iCloud 图书。")
        return 0

    if fitz is None:
        logging.warning("未检测到 PyMuPDF：PDF 仍会复制，但不会提取 PDF 图片或读取完整 PDF 元数据。")

    records: list[dict[str, Any]] = []
    for index, (kind, source) in enumerate(sources, start=1):
        logging.info("[%s/%s] 处理：%s", index, len(sources), source)
        try:
            record = export_one(kind, source, output, args.force, not args.no_images)
            records.append(record)
            logging.info("完成：%s（%s）", record.get("title", source.name), record["status"])
        except Exception as exc:
            logging.exception("处理失败：%s", source)
            records.append({
                "id": stable_id(source),
                "format": "EPUB" if kind.startswith("epub") else "PDF",
                "title": source.stem,
                "source_path": str(source),
                "status": "error",
                "error": str(exc),
            })

    write_catalog(output, records)
    exported = sum(1 for r in records if r.get("status") == "exported")
    errors = sum(1 for r in records if r.get("status") == "error")
    print(f"\n完成：共处理 {len(records)} 项；新导出 {exported} 项；失败 {errors} 项。")
    print(f"导出位置：{output}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
