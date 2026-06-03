"""
Bank Statement Transcriber
--------------------------
Run with:  python bank_statement_app.py
Then open: http://localhost:5000
"""

import io, csv, os, json, base64, uuid, threading
from pypdf import PdfReader, PdfWriter
import anthropic
from flask import Flask, request, send_file, jsonify

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
PAGES_PER_CHUNK   = 5

app = Flask(__name__)

# In-memory job store: { job_id: { status, progress, total, transactions, error } }
jobs = {}

HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bank Statement Transcriber</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #f5f5f5; color: #222; min-height: 100vh; padding: 2rem; }
  .card { background: white; border-radius: 12px; padding: 2rem;
          max-width: 620px; margin: 0 auto; box-shadow: 0 2px 12px rgba(0,0,0,.08); }
  h1 { font-size: 1.4rem; margin-bottom: .25rem; }
  .sub { color: #666; font-size: .9rem; margin-bottom: 1.75rem; }
  label { display: block; font-size: .85rem; font-weight: 600; margin-bottom: .4rem; color: #444; }
  .drop { border: 2px dashed #ccc; border-radius: 8px; padding: 2rem;
          text-align: center; cursor: pointer; transition: .15s;
          background: #fafafa; margin-bottom: 1rem; }
  .drop:hover, .drop.over { border-color: #4a7dff; background: #f0f5ff; }
  .drop input { display: none; }
  .drop .icon { font-size: 2rem; margin-bottom: .5rem; }
  .drop p { color: #666; font-size: .9rem; }
  .file-list { font-size: .82rem; color: #555; margin-top: .5rem; }
  .row { display: flex; gap: .75rem; align-items: flex-end; margin-bottom: 1.25rem; }
  .field { flex: 1; }
  input[type=number] { width: 100%; padding: .5rem .75rem; border: 1px solid #ddd; border-radius: 6px; font-size: .9rem; }
  button { background: #4a7dff; color: white; border: none; border-radius: 6px;
           padding: .6rem 1.4rem; font-size: .9rem; cursor: pointer; white-space: nowrap; height: 38px; }
  button:hover { background: #3366ee; }
  button:disabled { background: #aaa; cursor: not-allowed; }
  .status { margin-top: 1rem; font-size: .88rem; color: #555; min-height: 1.4rem; line-height: 1.4; }
  .status.error { color: #c0392b; }
  .status.done  { color: #27ae60; font-weight: 500; }
  .progress-wrap { margin-top: .75rem; display: none; }
  .progress-track { height: 8px; background: #eee; border-radius: 4px; overflow: hidden; }
  .progress-bar { height: 100%; background: #4a7dff; transition: width .4s; width: 0%; }
  .progress-label { font-size: .8rem; color: #888; margin-top: .3rem; }
</style>
</head>
<body>
<div class="card">
  <h1>📄 Bank Statement Transcriber</h1>
  <p class="sub">Upload one or more PDF bank statements — get a CSV with every transaction and running balance.</p>

  <div class="drop" id="drop">
    <input type="file" id="files" accept=".pdf" multiple>
    <div class="icon">📂</div>
    <p>Drop PDFs here or click to select</p>
    <p style="font-size:.8rem;margin-top:.25rem;">Multiple files OK — processed in order</p>
    <div class="file-list" id="fileList"></div>
  </div>

  <div class="row">
    <div class="field">
      <label for="opening">Opening balance ($) — optional</label>
      <input type="number" id="opening" placeholder="e.g. 5000.00" step="0.01">
    </div>
    <button id="btn" onclick="run()">Transcribe &amp; Download CSV</button>
  </div>

  <div class="progress-wrap" id="progressWrap">
    <div class="progress-track"><div class="progress-bar" id="bar"></div></div>
    <div class="progress-label" id="progressLabel">Starting...</div>
  </div>
  <div class="status" id="status"></div>
</div>

<script>
const drop = document.getElementById('drop');
const fileInput = document.getElementById('files');
const fileList = document.getElementById('fileList');
const status = document.getElementById('status');
const btn = document.getElementById('btn');
const bar = document.getElementById('bar');
const progressWrap = document.getElementById('progressWrap');
const progressLabel = document.getElementById('progressLabel');

drop.addEventListener('click', () => fileInput.click());
drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('over'); });
drop.addEventListener('dragleave', () => drop.classList.remove('over'));
drop.addEventListener('drop', e => { e.preventDefault(); drop.classList.remove('over'); fileInput.files = e.dataTransfer.files; showFiles(); });
fileInput.addEventListener('change', showFiles);

function showFiles() {
  fileList.textContent = [...fileInput.files].map(f => f.name).join(', ') || '';
}

async function run() {
  if (!fileInput.files.length) { setStatus('Please select at least one PDF.', 'error'); return; }
  btn.disabled = true;
  progressWrap.style.display = 'block';
  bar.style.width = '2%';
  setStatus('Uploading...');

  const form = new FormData();
  [...fileInput.files].forEach(f => form.append('files', f));
  const ob = document.getElementById('opening').value;
  if (ob) form.append('opening_balance', ob);

  try {
    const res = await fetch('/start', { method: 'POST', body: form });
    const data = await res.json();
    if (!res.ok) { setStatus('Error: ' + (data.error || res.statusText), 'error'); btn.disabled = false; return; }
    poll(data.job_id);
  } catch(e) {
    setStatus('Upload error: ' + e.message, 'error');
    btn.disabled = false;
  }
}

async function poll(jobId) {
  setStatus('Processing — this may take several minutes for large statements...');
  const interval = setInterval(async () => {
    try {
      const res = await fetch('/status/' + jobId);
      const data = await res.json();

      if (data.status === 'error') {
        clearInterval(interval);
        setStatus('Error: ' + data.error, 'error');
        btn.disabled = false;
        return;
      }

      if (data.total > 0) {
        const pct = Math.round((data.progress / data.total) * 100);
        bar.style.width = pct + '%';
        progressLabel.textContent = `Chunk ${data.progress} of ${data.total} processed...`;
      }

      if (data.status === 'done') {
        clearInterval(interval);
        bar.style.width = '100%';
        progressLabel.textContent = 'Done!';
        // Download
        const dlRes = await fetch('/download/' + jobId);
        const blob = await dlRes.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = 'bank_statement_transactions.csv'; a.click();
        URL.revokeObjectURL(url);
        setStatus('✓ Done! ' + data.count + ' transactions exported to CSV.', 'done');
        btn.disabled = false;
      }
    } catch(e) {
      // network hiccup, keep polling
    }
  }, 3000);
}

function setStatus(msg, cls='') {
  status.textContent = msg;
  status.className = 'status ' + cls;
}
</script>
</body>
</html>
"""


def pdf_chunk_bytes(reader, page_indices):
    writer = PdfWriter()
    for i in page_indices:
        writer.add_page(reader.pages[i])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def transcribe_chunk(client, pdf_bytes, running_balance, chunk_num, total_chunks):
    b64 = base64.b64encode(pdf_bytes).decode()
    prompt = f"""You are a precise financial data extractor. This is chunk {chunk_num} of {total_chunks}.

The running balance entering this chunk is ${running_balance:.2f}.

Extract EVERY transaction — do not skip, summarize, or truncate any.

Return ONLY a raw JSON array with no markdown fences, no explanation:
[
  {{
    "date": "MM/DD/YYYY",
    "description": "full description",
    "type": "credit or debit",
    "amount": 0.00,
    "running_balance": 0.00
  }}
]

Rules:
- amount is always a positive number
- type is "credit" for deposits/additions, "debit" for withdrawals/payments
- running_balance = previous + amount (credit) or - amount (debit)
- First transaction's starting balance is {running_balance:.2f}
- Preserve exact chronological order
- If no transactions on these pages, return []
- Do NOT include summary/header/footer lines as transactions"""

    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=8000,
        messages=[{
            "role": "user",
            "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}},
                {"type": "text", "text": prompt}
            ]
        }]
    )

    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    try:
        result = json.loads(raw)
    except Exception:
        start, end = raw.find("["), raw.rfind("]") + 1
        result = json.loads(raw[start:end]) if start != -1 else []

    return result if isinstance(result, list) else []


def process_job(job_id, files_data, opening_balance):
    job = jobs[job_id]
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        all_transactions = []
        running_balance = opening_balance
        total_chunks = 0

        # Count total chunks first
        parsed_files = []
        for filename, file_bytes in files_data:
            if not filename.lower().endswith(".pdf"):
                continue
            reader = PdfReader(io.BytesIO(file_bytes))
            total_pages = len(reader.pages)
            chunks = [list(range(i, min(i + PAGES_PER_CHUNK, total_pages)))
                      for i in range(0, total_pages, PAGES_PER_CHUNK)]
            parsed_files.append((filename, reader, chunks))
            total_chunks += len(chunks)

        job["total"] = total_chunks
        chunk_count = 0

        for filename, reader, chunks in parsed_files:
            for n, page_indices in enumerate(chunks, 1):
                chunk_pdf = pdf_chunk_bytes(reader, page_indices)
                txns = transcribe_chunk(client, chunk_pdf, running_balance, n, len(chunks))

                for t in txns:
                    amt = float(t.get("amount", 0) or 0)
                    if t.get("type") == "credit":
                        running_balance = round(running_balance + amt, 2)
                    else:
                        running_balance = round(running_balance - amt, 2)
                    t["running_balance"] = running_balance
                    all_transactions.append(t)

                chunk_count += 1
                job["progress"] = chunk_count

        # Sort chronologically — handle many date formats gracefully
        from datetime import datetime

        DATE_FORMATS = [
            "%m/%d/%Y", "%m/%d/%y",
            "%Y-%m-%d",
            "%B %d, %Y", "%b %d, %Y",
            "%B %d %Y",  "%b %d %Y",
            "%d %B %Y",  "%d %b %Y",
            "%m-%d-%Y",  "%m-%d-%y",
            "%d/%m/%Y",
        ]

        def parse_date(date_str):
            if not date_str:
                return datetime.max
            for fmt in DATE_FORMATS:
                try:
                    return datetime.strptime(date_str.strip(), fmt)
                except ValueError:
                    continue
            return datetime.max  # unparseable dates go to end

        all_transactions.sort(key=lambda t: (parse_date(t.get("date", "")), 0 if t.get("type") == "credit" else 1))

        # Recalculate running balance after sort
        balance = opening_balance
        for t in all_transactions:
            amt = float(t.get("amount", 0) or 0)
            if t.get("type") == "credit":
                balance = round(balance + amt, 2)
            else:
                balance = round(balance - amt, 2)
            t["running_balance"] = balance

        job["transactions"] = all_transactions
        job["status"] = "done"
        job["count"] = len(all_transactions)

    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


@app.route("/")
def index():
    return HTML


@app.route("/start", methods=["POST"])
def start():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files uploaded"}), 400

    try:
        opening = float(request.form.get("opening_balance", 0.0))
    except ValueError:
        opening = 0.0

    # Read file bytes immediately (can't pass file objects to thread)
    files_data = [(f.filename, f.read()) for f in files]

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "running", "progress": 0, "total": 0, "transactions": [], "error": None, "count": 0}

    thread = threading.Thread(target=process_job, args=(job_id, files_data, opening), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "progress": job["progress"],
        "total": job["total"],
        "count": job["count"],
        "error": job["error"]
    })


@app.route("/download/<job_id>")
def download(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Not ready"}), 404

    txns = job["transactions"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Description", "Amount", "Running Balance"])
    for t in txns:
        amt = float(t.get("amount", 0) or 0)
        if t.get("type") == "debit":
            amt = -amt
        w.writerow([
            t.get("date", ""),
            t.get("description", ""),
            f"{amt:.2f}",
            f"{float(t.get('running_balance', 0) or 0):.2f}",
        ])

    buf.seek(0)
    return send_file(
        io.BytesIO(buf.read().encode("utf-8")),
        mimetype="text/csv",
        as_attachment=True,
        download_name="bank_statement_transactions.csv"
    )


if __name__ == "__main__":
    import webbrowser
    from threading import Timer
    print("Starting Bank Statement Transcriber...")
    print("Opening browser at http://localhost:5000")
    Timer(1, lambda: webbrowser.open("http://localhost:5000")).start()
    app.run(port=5000, debug=False)
