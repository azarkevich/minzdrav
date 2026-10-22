#!/usr/bin/env python3
"""Incremental downloader of clinical protocols from minzdrav.gov.by.

Source: https://minzdrav.gov.by/ru/dlya-spetsialistov/standarty-obsledovaniya-i-lecheniya/

How it stays cheap on repeated runs:
  * category pages are scraped (25 small HTML pages) to get the current list of documents;
  * every document is requested with a conditional GET (If-None-Match / If-Modified-Since
    from the previous run), so unchanged files cost a bodiless 304 response;
  * a file is replaced only when its content actually changed (sha256), the previous
    version is kept in _history/;
  * documents that disappeared from the site are marked as removed in the manifest
    (and moved to _removed/ with --prune).

State lives in <out>/manifest.json; <out>/index.csv is a human-readable catalogue.

Usage:
  pip install requests
  python minzdrav_protocols_scrapper.py                 # download into ./protocols
  python minzdrav_protocols_scrapper.py -o D:/kp -w 8   # other folder, 8 parallel downloads
  python minzdrav_protocols_scrapper.py --dry-run       # only show what changed on the site
"""

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import requests

BASE_URL = "https://minzdrav.gov.by"
INDEX_URL = BASE_URL + "/ru/dlya-spetsialistov/standarty-obsledovaniya-i-lecheniya/"
CATEGORY_RE = re.compile(r"^/ru/dlya-spetsialistov/standarty-obsledovaniya-i-lecheniya/([\w-]+)\.php$")
DOC_EXTENSIONS = (".pdf", ".doc", ".docx", ".rtf", ".zip", ".rar", ".7z", ".xls", ".xlsx")
USER_AGENT = "Mozilla/5.0 (compatible; minzdrav-protocols-sync/1.0)"
MANIFEST_NAME = "manifest.json"
INDEX_NAME = "index.csv"
TIMEOUT = (15, 120)
RETRIES = 4

log = logging.getLogger("minzdrav")


# ---------------------------------------------------------------- HTML parsing

class _LinkParser(HTMLParser):
    """Collects <a href> links, page title, and protocol cards (<section class="document">)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []          # all hrefs on the page
        self.documents = []      # [(title, href)]
        self.page_title = ""
        self._in_doc = False
        self._doc_title = []
        self._doc_hrefs = []
        self._in_h4 = False
        self._in_content_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get("class") or "").split()
        if tag == "section" and "document" in cls:
            self._flush_doc()
            self._in_doc = True
        elif tag == "h4" and self._in_doc:
            self._in_h4 = True
        elif tag == "p" and "content-title" in cls:
            self._in_content_title = True
        elif tag == "a" and a.get("href"):
            href = a["href"].strip()
            self.links.append(href)
            if self._in_doc:
                self._doc_hrefs.append(href)

    def handle_endtag(self, tag):
        if tag == "h4":
            self._in_h4 = False
        elif tag == "p":
            self._in_content_title = False
        elif tag == "section" and self._in_doc:
            self._flush_doc()

    def handle_data(self, data):
        if self._in_h4:
            self._doc_title.append(data)
        elif self._in_content_title:
            self.page_title += data

    def close(self):
        super().close()
        self._flush_doc()

    def _flush_doc(self):
        if self._in_doc:
            title = " ".join("".join(self._doc_title).split())
            for href in dict.fromkeys(self._doc_hrefs):  # dedupe "view" + "download"
                if _is_document(href):
                    self.documents.append((title, href))
        self._in_doc = False
        self._doc_title = []
        self._doc_hrefs = []


def _is_document(href):
    path = urlsplit(href).path.lower().rstrip()
    return "/upload/" in path and path.endswith(DOC_EXTENSIONS)


def _parse(html):
    p = _LinkParser()
    p.feed(html)
    p.close()
    return p


def normalize_url(href, base=BASE_URL):
    """Absolute URL with a properly percent-encoded path (hrefs contain Cyrillic and spaces)."""
    parts = urlsplit(urljoin(base, href.strip()))
    path = quote(unquote(parts.path), safe="/")
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


# ---------------------------------------------------------------- HTTP

_local = threading.local()


def _session():
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers["User-Agent"] = USER_AGENT
        _local.session = s
    return s


def http_get(url, headers=None, stream=False):
    last_exc = None
    for attempt in range(RETRIES):
        try:
            r = _session().get(url, headers=headers or {}, timeout=TIMEOUT, stream=stream)
            if r.status_code >= 500 or r.status_code == 429:
                raise requests.HTTPError("HTTP %s" % r.status_code, response=r)
            return r
        except requests.RequestException as e:
            last_exc = e
            delay = 2 ** attempt
            log.debug("retry %s in %ss: %s", url, delay, e)
            time.sleep(delay)
    raise last_exc


# ---------------------------------------------------------------- scraping

def scrape_catalogue():
    """Returns {url: {"title", "categories": [..]}} for every document on the site."""
    r = http_get(INDEX_URL)
    r.raise_for_status()
    index = _parse(r.text)

    categories = []
    for href in index.links:
        m = CATEGORY_RE.match(urlsplit(href).path)
        if m and m.group(1) != "index":
            url = normalize_url(href)
            if url not in categories:
                categories.append(url)
    if not categories:
        raise RuntimeError("No categories found on %s, has the page layout changed?" % INDEX_URL)

    docs = {}
    for cat_url in categories:
        r = http_get(cat_url)
        r.raise_for_status()
        page = _parse(r.text)
        cat_name = " ".join(page.page_title.split()) or urlsplit(cat_url).path.rsplit("/", 1)[-1]
        found = list(page.documents)
        # documents outside of <section class="document"> cards, just in case
        known = {h for _, h in found}
        found += [("", h) for h in page.links if _is_document(h) and h not in known]

        for title, href in found:
            url = normalize_url(href)
            d = docs.setdefault(url, {"title": title, "categories": []})
            if not d["title"] and title:
                d["title"] = title
            if cat_name not in d["categories"]:
                d["categories"].append(cat_name)
        log.info("%-45s %3d docs", cat_name, len(found))
    return docs


# ---------------------------------------------------------------- local storage

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(name, max_len=150):
    name = _BAD_CHARS.sub("_", name).strip().rstrip(".")
    if len(name) > max_len:
        stem, ext = os.path.splitext(name)
        name = stem[: max_len - len(ext)].rstrip() + ext
    return name or "_"


def local_path_for(url, category):
    filename = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    return os.path.join(safe_name(category, 80), safe_name(filename))


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(out_dir):
    path = os.path.join(out_dir, MANIFEST_NAME)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"documents": {}}


def save_manifest(out_dir, manifest):
    path = os.path.join(out_dir, MANIFEST_NAME)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


def write_index(out_dir, manifest):
    path = os.path.join(out_dir, INDEX_NAME)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["category", "title", "file", "last_modified", "size", "status", "url"])
        rows = sorted(manifest["documents"].items(),
                      key=lambda kv: (kv[1].get("categories", [""])[0], kv[1].get("title", "")))
        for url, d in rows:
            w.writerow([" | ".join(d.get("categories", [])), d.get("title", ""), d.get("path", ""),
                        d.get("last_modified", ""), d.get("size", ""),
                        "removed" if d.get("removed") else "ok", url])


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _stamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


# ---------------------------------------------------------------- sync

def sync_one(url, info, entry, out_dir, dry_run):
    """Returns (status, new_entry). status: new | updated | unchanged | error."""
    rel_path = entry.get("path") or local_path_for(url, info["categories"][0])
    abs_path = os.path.join(out_dir, rel_path)
    have_file = os.path.exists(abs_path)

    headers = {}
    if have_file:
        if entry.get("etag"):
            headers["If-None-Match"] = entry["etag"]
        if entry.get("last_modified"):
            headers["If-Modified-Since"] = entry["last_modified"]

    new_entry = dict(entry, title=info["title"], categories=info["categories"], path=rel_path)
    new_entry.pop("removed", None)

    if dry_run:
        # HEAD-like check without downloading the body
        r = http_get(url, headers=headers, stream=True)
        if r.status_code == 304:
            r.content  # empty body; reading it returns the connection to the keep-alive pool
            return "unchanged", new_entry
        if r.status_code != 200:
            return "error", dict(new_entry, error="HTTP %s" % r.status_code)
        r.close()  # do not download the body
        return ("updated" if have_file else "new"), new_entry

    for attempt in range(RETRIES):
        try:
            return _download(url, headers, entry, new_entry, out_dir, rel_path, abs_path, have_file)
        except requests.RequestException as e:  # connection dropped / read timeout mid-body
            if attempt == RETRIES - 1:
                raise
            log.debug("retry %s: %s", url, e)
            time.sleep(2 ** attempt)


def _download(url, headers, entry, new_entry, out_dir, rel_path, abs_path, have_file):
    part = abs_path + ".part"
    r = http_get(url, headers=headers, stream=True)
    try:
        if r.status_code != 200:
            r.content  # small/empty body; reading it keeps the connection alive
        if r.status_code == 304:
            new_entry["checked"] = _now()
            return "unchanged", new_entry
        if r.status_code != 200:
            new_entry["error"] = "HTTP %s" % r.status_code
            return "error", new_entry

        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        h = hashlib.sha256()
        size = 0
        with open(part, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
                h.update(chunk)
                size += len(chunk)
    except BaseException:
        r.close()
        if os.path.exists(part):
            os.remove(part)
        raise

    expected = r.headers.get("Content-Length")
    if expected and int(expected) != size:
        os.remove(part)
        new_entry["error"] = "truncated download (%s of %s bytes)" % (size, expected)
        return "error", new_entry

    digest = h.hexdigest()
    new_entry.update(etag=r.headers.get("ETag"), last_modified=r.headers.get("Last-Modified"),
                     checked=_now())
    new_entry.pop("error", None)

    if have_file and (entry.get("sha256") or sha256_of(abs_path)) == digest:
        os.remove(part)  # server metadata changed, content did not
        return "unchanged", new_entry

    status = "new"
    if have_file:
        status = "updated"
        hist = os.path.join(out_dir, "_history", os.path.dirname(rel_path))
        os.makedirs(hist, exist_ok=True)
        stem, ext = os.path.splitext(os.path.basename(rel_path))
        shutil.move(abs_path, os.path.join(hist, "%s.%s%s" % (stem, _stamp(), ext)))
    os.replace(part, abs_path)
    new_entry.update(sha256=digest, size=size, downloaded=_now())
    new_entry.setdefault("first_seen", _now())
    return status, new_entry


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", default="protocols", help="output folder (default: ./protocols)")
    ap.add_argument("-w", "--workers", type=int, default=4, help="parallel downloads (default: 4)")
    ap.add_argument("--dry-run", action="store_true", help="report changes, download nothing")
    ap.add_argument("--prune", action="store_true",
                    help="move files that are no longer on the site to _removed/")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    manifest = load_manifest(out_dir)
    entries = manifest["documents"]

    log.info("Scanning %s", INDEX_URL)
    site = scrape_catalogue()
    log.info("Found %d unique documents", len(site))

    stats = {"new": 0, "updated": 0, "unchanged": 0, "error": 0}
    lock = threading.Lock()
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(sync_one, url, info, entries.get(url, {}), out_dir, args.dry_run): url
                   for url, info in site.items()}
        for fut in as_completed(futures):
            url = futures[fut]
            try:
                status, entry = fut.result()
            except Exception as e:  # network failure after retries etc.
                status, entry = "error", dict(entries.get(url, {}), error=str(e))
            with lock:
                done += 1
                stats[status] += 1
                if not args.dry_run:
                    entries[url] = entry
                    if done % 50 == 0:
                        save_manifest(out_dir, manifest)  # survive interruption
            if status != "unchanged":
                log.info("[%s] %s%s", status.upper(), entry.get("path") or url,
                         " - " + entry["error"] if status == "error" else "")

    gone = [u for u, e in entries.items() if u not in site and not e.get("removed")]
    for url in gone:
        e = entries[url]
        log.info("[REMOVED FROM SITE] %s", e.get("path"))
        if args.dry_run:
            continue
        e["removed"] = _now()
        src = os.path.join(out_dir, e.get("path", ""))
        if args.prune and e.get("path") and os.path.exists(src):
            dst = os.path.join(out_dir, "_removed", e["path"])
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            e["path"] = os.path.join("_removed", e["path"])

    if not args.dry_run:
        manifest["last_run"] = _now()
        save_manifest(out_dir, manifest)
        write_index(out_dir, manifest)

    log.info("Done: %d new, %d updated, %d unchanged, %d removed, %d errors%s",
             stats["new"], stats["updated"], stats["unchanged"], len(gone), stats["error"],
             " (dry run)" if args.dry_run else "")
    return 1 if stats["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
