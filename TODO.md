# TODO

What is unfinished or worth doing, roughly by priority. State as of 2026-10-24.

## Knowledge-base quality

- [ ] **LLM cross-check of the knowledge base** (`kb_llm.py crosscheck`). Done: 9 endocrinology
  chapters (71 verified, 17 uncertain, 37 llm_only, 0 rejected). Remaining: rest of endocrinology
  (~10 p.p. of the 5-hour limit on the subscription via `KB_LLM_PROVIDER=claude-cli`), whole corpus
  ~200 p.p. (about three 5-hour windows). Ideas: batch several chapters per call, Sonnet instead of
  Opus, or OpenRouter instead of the subscription. Stop at 85% of the limit (see memory).
- [ ] **OCR for the 9 scanned protocols** (`stats.kind == "scan"`: ДВС, антикоагулянтная терапия,
  хронический миелолейкоз, множественная миелома, 3 реабилитационных, эндовенозная лазерная
  коагуляция, гемофилия). Options: Tesseract (rus, local, weak on tables) or a vision model via
  OpenRouter (cheap). Mark such excerpts in answers as "recognized by OCR, check against the scan".
- [ ] **Narrow-column tables** not extracted by `table_extract.py` (child endocrinology 2019, ЛОР,
  2005 orders); nephrology rows without blank separators get merged. 55 docs are `kind: table`.
- [ ] **50 parsed documents with no lists found**: other wordings, or lists in appendices
  («согласно приложению N») that are not extracted.
- [ ] 17 documents without «ГЛАВА N» structure (`no_chapters`) get only partial handling.
- [ ] Protocol updates: re-run scraper → `protocol_parser.py` → `kb_build.py --out kb_all`; the
  chain is manual, no scheduling and no diff of what changed in the protocols.

## Recognition and matching

- [ ] «Болезнь Аддисона»-type cases (eponyms and colloquial names absent from the МКБ names and
  protocols) are solved only with `--llm`. Consider a curated eponym/synonym list for the common
  ones so that rules-only mode works too.
- [ ] Evaluation sets are small (88 questions, mostly written by the assistant). Get real questions
  from insurance staff and measure on them; the blind-run failures were all diagnosis recognition.
- [ ] Service resolution against decree №123 for lab complexes and compound services
  (e.g. «анализ мочи общий» vs. component tests) is heuristic.
- [ ] «Допускается ли услуга» also depends on conditions (age, severity, «по показаниям»);
  the answer quotes the condition but does not evaluate it.
- [ ] Population detection (adult / child) is a heuristic plus an LLM hint.

## Product / UI

- [ ] **Website without authentication** (`kb_web.py`, `run_web.bat`): anyone with the Cloudflare
  tunnel link can spend the OpenRouter credit. Add basic auth from an env var (and `--host`).
- [ ] **Permanent hosting.** Oracle Cloud Always Free did not work out; Hugging Face Spaces: Docker
  is paid, only Static is free. Options: own machine + Cloudflare Tunnel (current),
  the KB is now packed (`kb_pack.py`: ~50 MB RAM, 22 MB on disk), so 256–512 MB free PaaS
  tiers fit memory-wise (payment / region is the remaining blocker); or Pyodide in the browser (needs a per-user OpenRouter key).
- [ ] Web UI: history is only in the browser; no export of an answer (PDF / Excel) for the case file,
  no batch mode (many questions from a spreadsheet).
- [ ] Feedback (3 stars + tags + comment → `kb/feedback.jsonl`) is collected but there is no viewer
  and no script turning low-rated answers into `eval/` cases; ratings carry no user identity.
- [ ] Per-call LLM usage is logged to `kb/llm_usage.jsonl`; no dashboard or spending cap in the app.
- [ ] `kb_mcp.py` is tested only by a smoke test; not yet connected to Claude Desktop in practice.

## Process

- [ ] **Nothing is committed**: `kb_*.py`, `table_extract.py`, `protocol_parser.py`,
  `reference_builder.py`, `kb_mcp.py`, `kb_web.py`, `web/`, `data/`, `eval/`, `run_web.bat`,
  `EXPERT_SYSTEM_DESIGN.md`, `CLAUDE.md`, this file. `/reference/`, `/parsed/`, `/kb/`,
  `/kb_all/` are in `.gitignore` (rebuildable).
- [ ] No automated tests beyond `kb.py eval`; add the eval run to a script / CI.
- [ ] Legal/product caveat: the system is a reference aid built on automatic extraction, no doctor
  reviewed the knowledge base (decision by the user). Decide how this is stated to the users.
- [ ] Rotate / delete the temporary OpenRouter key (`KB_OPENROUTER_API_KEY`) when the work is done.
- [ ] Update the stage table in `EXPERT_SYSTEM_DESIGN.md` (stages 4–5 partial, web UI added).
