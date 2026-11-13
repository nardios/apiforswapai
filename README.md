# SwapAI Returns Analysis API

A small Flask service that turns a raw **e-commerce returns CSV** into a concise, structured **returns analysis**, powered by OpenAI.

Upload a CSV of returns for a store and month. The service summarises the data into five sections and stores the result in SQLite. It also formats each section as **BBCode** (`[list][*][b]Product[/b] ...[/list]`), so you can paste the output straight into a forum, CMS or report template.

| Section | What it tells you |
|---|---|
| `frequent_return_reasons` | The top reasons customers send items back |
| `size_issues` | Fit and sizing problems |
| `other_considerations` | Quality, shipping, expectations and similar |
| `label_issues` | Problems tied to specific product labels or tags |
| `noteworthy_products` | The 5-8 worst-offending products |

---

## Contents

- [How it works](#how-it-works)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [API reference](#api-reference)
- [Input CSV format](#input-csv-format)
- [How to test it](#how-to-test-it)
- [Admin UI](#admin-ui)
- [Deployment notes](#deployment-notes)
- [Known limitations](#known-limitations)
- [Project layout](#project-layout)

---

## How it works

```
 CSV upload ──► pandas ──► build prompt ──► OpenAI (analysis) ──► OpenAI (BBCode format)
                              │                                        │
                    trim level 0 → 1 → 2                       parse into 5 sections
                    (drops columns if the                              │
                     request fails, e.g.                               ▼
                     prompt too large)                         SQLite (output_data_more)
```

1. The CSV is read with pandas. The prompt has two parts:
   - **Return counts**: rows grouped by product, SKU, reason and sub-reason, with a count for each group.
   - **Customer comments**: the verbatim comments.
2. The prompt goes to OpenAI. If the request fails, for example because the prompt is too large, it retries with less detail. The trim levels are `0` (all columns), `1` (drops product labels) and `2` (drops SKUs and labels).
3. A second OpenAI call reformats the analysis as BBCode.
4. A regex splits the text into the five sections and saves them in SQLite under a unique `request_id`.

There are two ways to submit a file:

- **Async** (`/process/async`) returns a `request_id` immediately (HTTP 202) and processes in a background thread. This suits large files.
- **Sync** (`/process/sync`) waits for the analysis and returns it in the response.

---

## Quick start

### Option A: Docker (recommended)

```bash
git clone https://github.com/nardios/apiforswapai
cd apiforswapai

cp .env.example .env        # then edit .env and fill in the values
mkdir -p data               # SQLite volume

docker compose up --build -d
```

The API is now at **http://localhost:8001**. Check it:

```bash
curl http://localhost:8001/        # -> Hello, World!
```

`docker-compose.yml` also starts an `autoheal` container. It restarts the API if its healthcheck fails, and needs access to the Docker socket.

### Option B: Run locally with Python

Requires Python 3.12 or later.

```bash
git clone https://github.com/nardios/apiforswapai
cd apiforswapai

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # fill in the values
mkdir -p data

python -m flask --app main run --port 8000
# or, like production:
# gunicorn -c gunicorn.conf.py main:app
```

> **Important:** `DATABASE_PATH` defaults to `/app/data/output_data.db`, which only exists inside the container. For local runs, set `DATABASE_PATH=./data/output_data.db` in `.env`. `.env.example` already does this.

---

## Configuration

Set these as environment variables or in a `.env` file. The app **fails to start** if a required variable is missing.

| Variable | Required | Default | Description |
|---|---|---|---|
| `OPENAI_API_KEY` | yes | none | Your OpenAI API key |
| `FLASK_SECRET_KEY` | yes | none | Random string used for Flask sessions. Generate one with `python -c "import secrets; print(secrets.token_hex(32))"` |
| `ADMIN_USERNAME` | yes | none | Basic-auth username for `/admin` and `/test-auth` |
| `ADMIN_PASSWORD` | yes | none | Basic-auth password |
| `OPENAI_MODEL` | no | `gpt-5.4` | Model used for both the analysis and BBCode formatting |
| `DATABASE_PATH` | no | `/app/data/output_data.db` | Path to the SQLite file |

Never commit `.env`. It is listed in `.gitignore`.

---

## API reference

Base URL: `http://localhost:8001` (Docker) or `http://localhost:8000` (local).

### `GET /`
Health check. Returns the plain text `Hello, World!`.

### `POST /process/async`
Upload a CSV and process it in the background.

**Form fields** (`multipart/form-data`)

| Field | Required | Notes |
|---|---|---|
| `file` | yes | A `.csv` file, 10 MB at most |
| `store` | yes | Store name, e.g. `acme-eu` |
| `month` | yes | e.g. `10` or `October` |
| `year` | yes | e.g. `2026` |

**Responses**

| Code | Body |
|---|---|
| `202` | `{"status": "Success", "request_id": "<uuid>"}` |
| `400` | `{"error": "store, month, and year are required"}`, `"No file provided"`, `"No file selected"` or `"Only CSV files are accepted"` |
| `413` | `{"error": "File size exceeds the maximum limit"}` |

### `GET /process/status/<request_id>`
Poll the status of an async job.

| Code | Body |
|---|---|
| `200` | `{"request_id", "status": "success" or "error", "store", "month", "year"}` |
| `404` | `{"status": "not_found"}` |

> There is no `pending` state. A row is written only when processing **finishes** (successfully or not). Until then you get `404 not_found`, so treat `404` right after submitting as "still processing" and keep polling.

### `GET /read/entry/<request_id>`
Fetch the full analysis once the job has finished.

```json
{
  "request_id": "0b1c...",
  "status": "success",
  "store": "acme-eu",
  "month": "10",
  "year": "2026",
  "frequent_return_reasons": "FREQUENT RETURN REASONS:[list][*]...[/list]",
  "size_issues": "...",
  "other_considerations": "...",
  "label_issues": "...",
  "noteworthy_products": "NOTEWORTHY PRODUCTS:[list][*][b]Product - Color:[/b] ...[/list]"
}
```

If processing failed, `status` is `"error"` and the message is in `frequent_return_reasons` (`"Processing error: ..."`).

### `POST /process/sync`
Same upload as the async endpoint, but the request blocks until the analysis is done and returns it directly. The gunicorn timeout is 900 s, but clients and proxies often time out sooner, so prefer async for large files.

**Response `200`**
```json
{
  "status": "success",
  "store": "acme-eu", "month": "10", "year": "2026",
  "database_id": 1,
  "request_id": "<uuid>",
  "output": {
    "FREQUENT RETURN REASONS": "[list][*]...[/list]",
    "SIZE ISSUES": "...",
    "OTHER CONSIDERATIONS": "...",
    "COMMON ISSUES FOR SPECIFIC PRODUCT LABELS": "...",
    "NOTEWORTHY PRODUCTS": "..."
  }
}
```
`400` means a missing or non-CSV file. `500` means `{"error": "Failed to process file: ..."}`.

### `GET /test-auth` (basic auth)
Returns `Authenticated!` if the `ADMIN_USERNAME` and `ADMIN_PASSWORD` credentials are valid. Use it to verify them.

### `GET /debug`
A built-in web page (`debug_page.html`) for uploading a CSV and viewing results in the browser, with no `curl` needed.

---

## Input CSV format

The CSV needs a header row. The service uses these columns:

| Column | Description |
|---|---|
| `Product_Name` | Product name (analysis is done per product) |
| `Variance_Title` | Variant title, usually size or color |
| `Product_Tags` | Product labels or tags |
| `SKU` | SKU of the returned variant |
| `Return_Reason` | Main reason for the return |
| `Sub_Reason` | More specific reason (blank becomes `Unknown`) |
| `Comment` | Free-text customer comment (optional per row) |

Any missing column is treated as empty, so the call won't fail, but the analysis will be poorer. **Each row is one returned item.**

A ready-made example is in [`sample_returns.csv`](sample_returns.csv).

---

## How to test it

### 1. Smoke test (no OpenAI calls, no cost)

```bash
curl -i http://localhost:8001/
# HTTP/1.1 200 OK ... Hello, World!

curl -i -u "$ADMIN_USERNAME:$ADMIN_PASSWORD" http://localhost:8001/test-auth
# 200 Authenticated!

curl -i http://localhost:8001/test-auth
# 401 Unauthorized (basic auth is enforced)
```

### 2. Input validation (no OpenAI calls)

These fail before reaching OpenAI, so they are free and instant.

```bash
# Missing store/month/year -> 400
curl -s -X POST http://localhost:8001/process/async \
  -F "file=@sample_returns.csv"

# Not a CSV -> 400
echo "hello" > /tmp/x.txt
curl -s -X POST http://localhost:8001/process/async \
  -F "store=acme" -F "month=10" -F "year=2026" -F "file=@/tmp/x.txt"

# Unknown request id -> 404
curl -s -i http://localhost:8001/process/status/does-not-exist
```

### 3. End-to-end, async (uses your OpenAI key)

```bash
# 1) Submit
RID=$(curl -s -X POST http://localhost:8001/process/async \
  -F "store=acme-eu" -F "month=10" -F "year=2026" \
  -F "file=@sample_returns.csv" | python -c "import sys,json; print(json.load(sys.stdin)['request_id'])")
echo "request_id=$RID"

# 2) Poll until it stops returning 404 (usually under a minute)
until [ "$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8001/process/status/$RID)" = "200" ]; do
  echo "still processing..."; sleep 5
done

# 3) Read the result
curl -s http://localhost:8001/read/entry/$RID | python -m json.tool
```

**Pass criteria:** the status is `"success"` and all five section fields are non-empty BBCode lists. If the status is `"error"`, read `frequent_return_reasons` for the message. The most common causes are a bad API key, an unavailable `OPENAI_MODEL` or no credit.

### 4. End-to-end, sync

```bash
curl -s -X POST http://localhost:8001/process/sync \
  -F "store=acme-eu" -F "month=10" -F "year=2026" \
  -F "file=@sample_returns.csv" | python -m json.tool
```

### 5. Browser test

Open **http://localhost:8001/debug**, choose a CSV, fill in the store and month, and submit. Then open **http://localhost:8001/admin** (log in with `ADMIN_USERNAME` and `ADMIN_PASSWORD`) to see the stored row.

### 6. Size limit

```bash
head -c 11000000 /dev/zero > /tmp/big.csv      # 11 MB
curl -s -X POST http://localhost:8001/process/async \
  -F "store=a" -F "month=1" -F "year=2026" -F "file=@/tmp/big.csv;filename=big.csv"
# -> 413 {"error":"File size exceeds the maximum limit"}
```

### Test with Postman CLI

Import the endpoints above into a Postman collection and run them with `postman collection run`. The flow is: submit, poll `/process/status/{{request_id}}`, then read `/read/entry/{{request_id}}`. Assert that the final `status` is `success`.

---

## Admin UI

`/admin` serves a Flask-Admin table view of all analyses (`output_data_more`), protected by HTTP basic auth. You can browse, search and delete rows. Use a strong `ADMIN_PASSWORD` and put the service behind HTTPS, because basic auth sends credentials in every request.

---

## Deployment notes

- **Persistence:** the SQLite file lives in `./data` on the host (mounted to `/app/data`). Back up that folder.
- **Concurrency:** gunicorn runs 2 workers with 4 threads each. SQLite handles this load, but for heavy concurrent writes consider PostgreSQL.
- **Async jobs run in daemon threads inside the web process.** If the container restarts mid-job, that job is lost and its `request_id` will never appear. Re-submit it.
- **Ports:** the container listens on `8000`, published as `8001` by compose.
- **Cost:** each upload makes 2 or more OpenAI calls. The prompt contains all comments in the file, so large files cost more.

---

## Known limitations

- `/process/sync` does not validate `store`, `month` and `year` up front. If any is omitted, the database insert fails and you get a `500`. Always send all three.
- There is no `pending` or `processing` status for async jobs (see above).
- The API endpoints (`/process/*`, `/read/*`) are **not authenticated**. Only `/admin` and `/test-auth` use basic auth. Put the service behind a gateway or firewall if it is exposed publicly.
- The output depends on LLM responses, so section parsing can occasionally return fewer than five sections if the model deviates from the expected titles.

---

## Project layout

```
.
├── main.py               # Flask app, routes, OpenAI calls, DB model
├── debug_page.html       # Browser UI served at /debug
├── sample_returns.csv    # Example input for testing
├── requirements.txt
├── Dockerfile
├── docker-compose.yml    # API + autoheal
├── gunicorn.conf.py
└── .env.example          # Template for required environment variables
```
