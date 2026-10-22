# CLAUDE.md — minzdrav tools

A set of tools for working with clinical diagnostic and treatment protocols
(клинические протоколы) published by the Ministry of Health of Belarus:
https://minzdrav.gov.by/ru/dlya-spetsialistov/standarty-obsledovaniya-i-lecheniya/

Each tool is a standalone Python 3 script in this directory. Git repository; script output (`protocols/`) is not committed.

## Tools

| Script | Purpose |
|---|---|
| `minzdrav_protocols_scrapper.py` | Incremental downloader of the protocol files from the site into `./protocols` |
| `protocol_chapter_extractor.py` | Extracts one chapter of a protocol PDF into Markdown + JSONL chunks for semantic search / RAG (`./chapters`) |

Downstream tools should treat `protocols/` (files + `manifest.json` + `index.csv`) as
their input and must not modify it — it is owned by the scraper.

---

## 1. `minzdrav_protocols_scrapper.py` — protocol downloader

Python 3 + `requests` (no other deps).

### Run

```
pip install requests
python minzdrav_protocols_scrapper.py                  # sync into ./protocols
python minzdrav_protocols_scrapper.py -o D:/kp -w 8    # other folder, 8 parallel downloads
python minzdrav_protocols_scrapper.py --dry-run        # report changes, download nothing
python minzdrav_protocols_scrapper.py --prune          # move files gone from the site to _removed/
python minzdrav_protocols_scrapper.py -v               # debug logging (retries etc.)
```

Exit code is 1 if any document failed to download. A full first sync is ~400 files /
~130 MB and takes ~2 min; a no-change re-run takes ~4 s.

### How it works

1. **Scrape catalogue.** The index page links to ~24 category pages
   (`/ru/dlya-spetsialistov/standarty-obsledovaniya-i-lecheniya/<slug>.php`, `index.php`
   excluded). Each category page lists protocols as `<section class="document">` cards:
   `<h4>` = protocol title (sometimes missing), two `<a>` (view + download) to the same
   file under `/upload/...` (mostly `/upload/dadvfiles/CProtokol/`, some `/upload/lcfiles/`).
   Parsed with stdlib `html.parser` (`_LinkParser`); category name comes from
   `<p class="content-title">`. Links outside cards that point to `/upload/*.pdf|doc|…`
   are picked up too.
2. **Dedupe by URL.** The same file can appear in several categories; it's downloaded once
   and stored in the folder of the first category, all categories are kept in the manifest.
   hrefs contain raw Cyrillic, spaces and sometimes trailing spaces — `normalize_url`
   strips and percent-encodes them.
3. **Conditional GET.** Files are static on nginx with `ETag` + `Last-Modified`; the script
   sends `If-None-Match` / `If-Modified-Since` from the previous run and gets a bodiless
   `304` when nothing changed. If the local file is missing, it's re-downloaded.
4. **Content check.** On `200` the body goes to `<file>.part`, length is checked against
   `Content-Length`, and `sha256` is compared with the stored one: same content → only
   metadata is updated; changed → old version moved to `_history/<category>/<name>.<timestamp>.<ext>`.
5. **Removed documents** (in manifest, not on site) get a `removed` timestamp;
   with `--prune` the file is moved to `_removed/`.

### Output layout (`--out`, default `./protocols`)

```
protocols/
  <Category name>/<original file name>   # current versions
  _history/…                             # previous versions of updated files
  _removed/…                             # files no longer on the site (--prune)
  manifest.json                          # sync state, keyed by URL
  index.csv                              # catalogue: category;title;file;last_modified;size;status;url
                                         # (UTF-8 with BOM, ';' separator — opens in Excel)
```

`manifest.json` entry: `path` (relative to out dir), `title`, `categories`, `etag`,
`last_modified`, `sha256`, `size`, `first_seen`, `downloaded`, `checked`, optional
`error` / `removed`. It is saved every 50 documents so an interrupted run resumes cheaply.
Don't hand-edit `path` values: they define where files are expected.

### Gotchas

- **Keep-alive matters.** A new TLS connection to the site costs ~3 s, a `304` on an open
  one ~10 ms. Never `close()` a successful/`304` streamed response — read `r.content`
  (empty) or fully consume `iter_content` so the connection returns to the pool.
  Closing them once made a no-change run take 65 s instead of 4 s.
- One `requests.Session` per worker thread (`threading.local`).
- Retries: `http_get` retries 5xx/429/network errors when opening a request; `sync_one`
  additionally retries the whole download, since read timeouts happen mid-body.
- File names are sanitized for Windows (`safe_name`: illegal chars → `_`, length capped);
  category folder names are Russian.
- If the site layout changes and no categories are found, the script fails loudly
  instead of marking everything as removed. Zero documents in a category is not checked.

### Testing changes

No test suite. Verify against the live site with `--dry-run`, or copy `protocols/` to a
scratch folder and tamper with its `manifest.json` (drop `etag`/`last_modified` and set a
fake `sha256` → expect `[UPDATED]` + `_history`; add a fake URL entry → expect
`[REMOVED FROM SITE]`). Run Python with `-I` when reading downloaded files.

---

## 2. `protocol_chapter_extractor.py` — chapter → RAG chunks

Python 3 stdlib + `pdftotext` on PATH (the Git-for-Windows `/mingw64/bin/pdftotext`
is xpdf 4.00, not poppler). Text PDFs only — no OCR.

### Run

```
python -I protocol_chapter_extractor.py "protocols/<Category>/<file>.pdf"               # chapter whose title starts with ДИАГНОСТИКА
python -I protocol_chapter_extractor.py "<file>.pdf" --chapter 6                        # by number
python -I protocol_chapter_extractor.py "<file>.pdf" --title ЛЕЧЕНИЕ --max-chars 2000
```

If the chapter isn't found, it exits listing the available `ГЛАВА N` titles.
`--title` picks the first chapter whose title starts with the given text
(e.g. «ДИАГНОСТИКА АГ», not «ДИАГНОСТИКА И ЛЕЧЕНИЕ … КРИЗОМ»).

### Output (`--out-dir`, default `./chapters`, not committed)

```
chapters/<pdf name>/
  chapterNN.md      # clean chapter text, one paragraph per line
  chapterNN.jsonl   # chunks: one per top-level clause (11., 12., …)
  glossary.json     # abbreviations of the whole document: {"СМАД": "суточном мониторировании АД"}
```

Chunk fields: `id`, `source`, `document`, `chapter`, `chapter_title`, `clause`, `page`,
`text`, `embedding_text` (context header «Документ. Глава N. Пункт X.» + text +
expanded abbreviations — this is what to embed), `abbreviations`, `appendix_refs`.
A clause longer than `--max-chars` is split by its sub-clauses (11.1, 11.2…), each
prefixed with the clause's intro sentence so it stays self-contained.

### How it works / gotchas

- `pdftotext -layout`; pages split on `\f` (gives `page`). Page footer (page number) and
  the «Национальный правовой Интернет-портал…» header are dropped by regex.
- Paragraphs are rebuilt from indentation: body paragraphs start with ~7 spaces,
  wrapped lines with 0. Headings are centered (indent > 15). A line ending in `-` is
  glued without a space (only real compound hyphens occur in these documents).
- xpdf on Windows can't open non-ASCII paths and can't read stdin, so the PDF is
  copied to a temp `input.pdf` first.
- Glossary is heuristic: for each «(далее – X)» it matches X's letters against initials
  of the preceding words, falling back to "back up to the nearest word starting with
  X's first letter". Expansions keep the case form from the text; a few are noisy
  (ИОАГ, ИААГ); multi-word abbreviations (ХС ЛПВП) are not captured.
- Appendices (приложения) referenced from the chapter are not extracted.
