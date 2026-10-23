# Deploying the website to Render (free)

Files involved: `render.yaml` (blueprint), `requirements.txt`, `deploy/` (the runtime knowledge base,
~28 MB, made by `make_deploy.py`), `kb_web.py` (`--host`, `$PORT`, `$KB_WEB_PASSWORD`, `/healthz`).

## One time

1. **GitHub repo.** Create an empty *private* repository on github.com (no README), then in this folder:
   ```
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin master
   ```
   (GitHub asks for a login / personal access token with the `repo` scope.) `deploy/` is committed;
   `protocols/`, `parsed/`, `kb_all/`, `reference/` are not.
2. **Render account.** render.com → sign up with GitHub (no card in the free flow as far as the docs say).
3. **Blueprint.** Dashboard → New + → Blueprint → pick the repository → Render reads `render.yaml`.
   It asks for two secrets:
   - `KB_OPENROUTER_API_KEY` — the OpenRouter key (without it the site works, but a diagnosis that is
     not an exact name — «болезнь Аддисона» — is not recognized; every answer then shows an error for
     the LLM step only when it was needed);
   - `KB_WEB_PASSWORD` — the site password; the browser asks for a login (any) and this password.
4. **Apply.** The first build takes a few minutes. The address is `https://minzdrav-<id>.onrender.com`.

## Questions and ratings → Google Sheet

`sheet_log.py` sends every question (sheet «Запросы») and every rating (sheet «Оценки») to a Google
Sheet through an Apps Script web app. Personal data are masked before sending and before writing
`kb/feedback.jsonl` (`sheet_log.mask`: ФИО with initials or a patronymic, «пациент Иванов», phones,
e-mail, dates, passport and personal numbers, «полис / договор № …», 6+ digit runs).

1. Create a Google Sheet (e.g. «minzdrav — запросы и оценки»), share it with the people who review answers.
2. Extensions → Apps Script → replace the code with `web/sheet_logger.gs`, set `TOKEN` to a long random
   string → Save.
3. Deploy → New deployment → type «Web app», Execute as «Me», Who has access «Anyone» → Deploy →
   allow access → copy the Web app URL (`https://script.google.com/macros/s/…/exec`).
4. Render → the service → Environment: `KB_SHEET_URL` = that URL, `KB_SHEET_TOKEN` = the same TOKEN →
   Save (the service restarts). The startup log line shows `sheet on`.

Columns are created from the first row; a new field from a later version becomes a new column. After
editing the script: Deploy → Manage deployments → edit → Version «New version» (the URL stays). A row that
can't be sent is printed to the log as `SHEET_FAIL {json}`.

## Behaviour of the free plan

- The service sleeps after 15 minutes without requests; the first request after that waits up to ~1 minute.
- The disk is throwaway: `kb/feedback.jsonl` and `kb/llm_usage.jsonl` vanish on restart. Ratings are
  also printed as `FEEDBACK {json}` lines: Dashboard → the service → Logs → search `FEEDBACK`
  (the log retention is limited — copy the lines out regularly).
- 750 free instance hours per month (enough for one always-awake service).

## Updating the knowledge base

```
python -I kb_build.py --out kb_all     # or only kb_pack.py kb_all/kb.json
python -I make_deploy.py
git add deploy && git commit -m "Update knowledge base" && git push
```
Render redeploys on every push. Each rebuilt `kb.items.bin` (18 MB) stays in git history.

## Local check of the same layout

```
set KB_WEB_PASSWORD=secret
python -I kb_web.py --kb deploy/kb.idx.json --reference deploy/reference
```
