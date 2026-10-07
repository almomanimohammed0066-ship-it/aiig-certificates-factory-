"""
AI Interest Group || Hashemite University - Certificate Hub
Run: python app.py
Open: http://127.0.0.1:5000
"""

import csv
import io
import os
import re
import smtplib
import tempfile
import urllib.request
import uuid

from email.message import EmailMessage

from flask import Flask, Response, jsonify, request, send_from_directory
from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import HexColor
from reportlab.pdfgen import canvas


app = Flask(__name__)

WORK = tempfile.mkdtemp(prefix="certs_")
JOBS = {}


# =========================================================
# HELPERS
# =========================================================

def safe(name):
    return (
        re.sub(r"[^\w\- ]+", "", name)
        .strip()
        .replace(" ", "_")
        or "certificate"
    )


def sheet_to_csv(url):
    m = re.search(r"/spreadsheets/d/([\w-]+)", url)

    if not m:
        raise ValueError("Not a valid Google Sheets URL")

    gid = re.search(r"gid=(\d+)", url)

    export = (
        f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export"
        f"?format=csv&gid={gid.group(1) if gid else 0}"
    )

    with urllib.request.urlopen(export, timeout=20) as r:
        return r.read().decode("utf-8-sig")


def parse_rows(text):
    reader = csv.DictReader(io.StringIO(text))

    if not reader.fieldnames:
        raise ValueError("CSV has no headers")

    reader.fieldnames = [
        (h or "").strip().lower()
        for h in reader.fieldnames
    ]

    if not {"name", "email"} <= set(reader.fieldnames):
        raise ValueError(
            "CSV must contain 'name' and 'email' columns"
        )

    rows = []
    skipped = []

    for i, r in enumerate(reader, start=2):
        name = (r.get("name") or "").strip()
        email = (r.get("email") or "").strip()

        if name and re.match(
            r"[^@\s]+@[^@\s]+\.[^@\s]+$",
            email
        ):
            rows.append({
                "name": name,
                "email": email
            })
        else:
            skipped.append(
                f"row {i}: "
                f"{name or '(no name)'} / "
                f"{email or '(no email)'}"
            )

    return rows, skipped


def make_cert(
    template_path,
    out_path,
    name,
    x,
    y,
    size,
    color,
    align,
    font
):
    reader = PdfReader(template_path)

    if not reader.pages:
        raise ValueError("Certificate template contains no pages")

    page = reader.pages[0]

    width = float(page.mediabox.width)
    height = float(page.mediabox.height)

    buf = io.BytesIO()

    c = canvas.Canvas(
        buf,
        pagesize=(width, height)
    )

    c.setFont(font, size)
    c.setFillColor(HexColor(color))

    if align == "center":
        c.drawCentredString(x, y, name)

    elif align == "right":
        c.drawRightString(x, y, name)

    else:
        c.drawString(x, y, name)

    c.save()

    buf.seek(0)

    overlay = PdfReader(buf)

    page.merge_page(overlay.pages[0])

    writer = PdfWriter()
    writer.add_page(page)

    with open(out_path, "wb") as f:
        writer.write(f)


# =========================================================
# GENERATE CERTIFICATES
# =========================================================

@app.post("/generate")
def generate():

    try:
        form = request.form

        # -----------------------------
        # Template
        # -----------------------------

        if "template" not in request.files:
            raise ValueError(
                "Please upload a PDF certificate template first."
            )

        template_file = request.files["template"]

        if not template_file.filename:
            raise ValueError(
                "Please select a certificate PDF."
            )

        # -----------------------------
        # Data source
        # -----------------------------

        sheet_url = form.get(
            "sheet_url",
            ""
        ).strip()

        if sheet_url:

            text = sheet_to_csv(sheet_url)

        elif (
            "csv" in request.files
            and request.files["csv"].filename
        ):

            text = (
                request.files["csv"]
                .read()
                .decode("utf-8-sig")
            )

        else:

            raise ValueError(
                "Please upload a CSV file or provide a Google Sheet URL."
            )

        # -----------------------------
        # Parse recipients
        # -----------------------------

        rows, skipped = parse_rows(text)

        if not rows:
            raise ValueError(
                "No valid recipient rows were found."
            )

        # -----------------------------
        # Job folder
        # -----------------------------

        job = uuid.uuid4().hex

        directory = os.path.join(
            WORK,
            job
        )

        os.makedirs(directory)

        template_path = os.path.join(
            directory,
            "template.pdf"
        )

        template_file.save(template_path)

        # -----------------------------
        # Settings
        # -----------------------------

        x = float(form.get("x", 297))
        y = float(form.get("y", 184))
        size = float(form.get("size", 32))

        color = form.get(
            "color",
            "#000000"
        )

        align = form.get(
            "align",
            "center"
        )

        font = (
            "Helvetica-Bold"
            if form.get("bold") == "true"
            else "Helvetica"
        )

        # -----------------------------
        # Generate
        # -----------------------------

        used_names = set()

        for row in rows:

            base = safe(row["name"])

            filename = (
                f"{base}_certificate.pdf"
            )

            counter = 2

            while filename in used_names:

                filename = (
                    f"{base}_{counter}_certificate.pdf"
                )

                counter += 1

            used_names.add(filename)

            row["file"] = filename

            make_cert(
                template_path,
                os.path.join(
                    directory,
                    filename
                ),
                row["name"],
                x,
                y,
                size,
                color,
                align,
                font
            )

        JOBS[job] = {
            "dir": directory,
            "rows": rows
        }

        previews = []

        for i, row in enumerate(rows[:3]):

            previews.append({
                "name": row["name"],
                "url": f"/cert/{job}/{i}"
            })

        return jsonify(
            ok=True,
            job=job,
            count=len(rows),
            skipped=skipped,
            previews=previews
        )

    except Exception as e:

        return jsonify(
            ok=False,
            error=str(e)
        ), 400


# =========================================================
# VIEW CERTIFICATE
# =========================================================

@app.get("/cert/<job>/<int:i>")
def cert(job, i):

    job_data = JOBS.get(job)

    if not job_data:
        return "Job not found", 404

    rows = job_data["rows"]

    if i < 0 or i >= len(rows):
        return "Certificate not found", 404

    return send_from_directory(
        job_data["dir"],
        rows[i]["file"],
        mimetype="application/pdf"
    )


# =========================================================
# SEND EMAIL
# =========================================================

@app.post("/send_one")
def send_one():

    data = request.get_json(silent=True) or {}

    if data.get("confirm") is not True:

        return jsonify(
            ok=False,
            error="Sending not confirmed"
        ), 403

    job_data = JOBS.get(
        data.get("job")
    )

    if not job_data:

        return jsonify(
            ok=False,
            error="Unknown job session"
        ), 404

    try:

        index = int(data.get("idx", 0))

        if index < 0 or index >= len(job_data["rows"]):
            raise ValueError("Invalid recipient index")

        row = job_data["rows"][index]

        username = (
            data.get("user") or ""
        ).strip()

        password = (
            data.get("password") or ""
        ).strip()

        if not username or not password:
            raise ValueError(
                "SMTP username and password are required."
            )

        host = (
            data.get("host")
            or "smtp.gmail.com"
        ).strip()

        port = int(
            data.get("port") or 587
        )

        subject = (
            data.get("subject")
            or "Certificate"
        ).replace(
            "{name}",
            row["name"]
        )

        body = (
            data.get("body")
            or ""
        ).replace(
            "{name}",
            row["name"]
        )

        # ---------------------------------
        # From header
        # ---------------------------------

        display_name = (
            data.get("from") or ""
        ).strip()

        if display_name:

            from_header = (
                f"{display_name} <{username}>"
            )

        else:

            from_header = username

        # ---------------------------------
        # Build email
        # ---------------------------------

        msg = EmailMessage()

        msg["From"] = from_header
        msg["To"] = row["email"]
        msg["Subject"] = subject

        msg.set_content(body)

        certificate_path = os.path.join(
            job_data["dir"],
            row["file"]
        )

        with open(
            certificate_path,
            "rb"
        ) as fh:

            msg.add_attachment(
                fh.read(),
                maintype="application",
                subtype="pdf",
                filename=row["file"]
            )

        # ---------------------------------
        # SMTP
        # ---------------------------------

        if port == 465:

            with smtplib.SMTP_SSL(
                host,
                port,
                timeout=30
            ) as server:

                server.login(
                    username,
                    password
                )

                server.send_message(msg)

        else:

            with smtplib.SMTP(
                host,
                port,
                timeout=30
            ) as server:

                server.ehlo()

                server.starttls()

                server.ehlo()

                server.login(
                    username,
                    password
                )

                server.send_message(msg)

        return jsonify(
            ok=True,
            name=row["name"],
            email=row["email"]
        )

    except Exception as e:

        return jsonify(
            ok=False,
            name=(
                row["name"]
                if "row" in locals()
                else ""
            ),
            email=(
                row["email"]
                if "row" in locals()
                else ""
            ),
            error=str(e)
        ), 200


# =========================================================
# LOGO
# =========================================================

@app.get("/logo.img")
def serve_logo():

    images = [
        f
        for f in os.listdir(".")
        if f.lower().endswith(
            (".png", ".jpg", ".jpeg", ".webp")
        )
    ]

    # Prefer logo files
    for image in images:

        if (
            "logo" in image.lower()
            or "whatsapp" in image.lower()
        ):

            return send_from_directory(
                ".",
                image
            )

    if images:

        return send_from_directory(
            ".",
            images[0]
        )

    return "", 404


# =========================================================
# FRONTEND
# =========================================================

@app.get("/")
def index():

    return Response(
        HTML,
        mimetype="text/html"
    )


# =========================================================
# HTML
# =========================================================

HTML = r"""
<!doctype html>

<html lang="en">

<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1"
>

<title>
AI Interest Group | Certificate Hub
</title>

<link
    rel="preconnect"
    href="https://fonts.googleapis.com"
>

<link
    rel="preconnect"
    href="https://fonts.gstatic.com"
    crossorigin
>

<link
    href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap"
    rel="stylesheet"
>

<style>

:root {

    --bg: #0b0f19;
    --card-bg: #141c30;
    --card-border: #222d4a;

    --pri: #8b5cf6;
    --pri-glow: rgba(139,92,246,.45);

    --cyan: #38bdf8;
    --cyan-glow: rgba(56,189,248,.35);

    --tx: #f8fafc;
    --tx-dim: #94a3b8;
    --tx-muted: #64748b;

    --red: #ef4444;
    --green: #10b981;
}

* {
    box-sizing: border-box;
}

html {
    min-height: 100%;
}

body {

    margin: 0;

    background:
        radial-gradient(
            circle at 50% -20%,
            #1e1b4b 0%,
            #0b0f19 80%
        );

    color: var(--tx);

    font-family:
        'Plus Jakarta Sans',
        system-ui,
        -apple-system,
        sans-serif;

    min-height: 100vh;

    display: flex;
    flex-direction: column;
}


/* =========================================================
HEADER
========================================================= */

header {

    background:
        rgba(15,23,42,.85);

    backdrop-filter: blur(12px);

    border-bottom:
        1px solid var(--card-border);

    padding:
        14px 28px;

    display: flex;

    align-items: center;

    justify-content: space-between;

    position: sticky;

    top: 0;

    z-index: 100;
}

.brand {

    display: flex;

    align-items: center;

    gap: 14px;
}

.logo-box {

    height: 44px;

    min-width: 44px;

    border-radius: 12px;

    display: flex;

    align-items: center;

    justify-content: center;

    overflow: hidden;

    flex-shrink: 0;

    background: #fff;

    padding: 4px;
}

.logo-box img {

    height: 100%;

    width: auto;

    object-fit: contain;
}

.brand-title {

    font-size: 17px;

    font-weight: 700;

    letter-spacing: -.01em;

    line-height: 1.2;
}

.brand-sub {

    font-size: 13px;

    color: var(--cyan);

    font-weight: 500;

    margin-top: 3px;
}

.user-badge {

    display: flex;

    align-items: center;

    gap: 10px;

    background:
        rgba(255,255,255,.04);

    padding:
        6px 12px;

    border-radius: 20px;

    border:
        1px solid var(--card-border);

    font-size: 13px;

    color: var(--tx-dim);
}

.dot-online {

    width: 8px;

    height: 8px;

    background: var(--green);

    border-radius: 50%;

    box-shadow:
        0 0 8px var(--green);
}


/* =========================================================
MAIN
========================================================= */

main {

    flex: 1;

    max-width: 1280px;

    width: 100%;

    margin: 0 auto;

    padding:
        24px 20px 48px;
}


/* =========================================================
STEPS
========================================================= */

.steps-nav {

    display: flex;

    gap: 10px;

    margin-bottom: 24px;

    background:
        rgba(18,26,45,.7);

    padding: 8px;

    border-radius: 14px;

    border:
        1px solid var(--card-border);

    overflow-x: auto;
}

.step-pill {

    flex: 1;

    min-width: 180px;

    padding:
        10px 16px;

    border-radius: 10px;

    display: flex;

    align-items: center;

    gap: 10px;

    font-size: 13px;

    font-weight: 600;

    color: var(--tx-dim);

    background: transparent;

    border:
        1px solid transparent;

    cursor: pointer;

    transition: all .2s ease;

    user-select: none;
}

.step-pill:hover {

    color: #fff;

    background:
        rgba(255,255,255,.03);
}

.step-pill.active {

    background:
        rgba(30,41,69,.9);

    border-color:
        var(--cyan);

    box-shadow:
        0 0 18px var(--cyan-glow);

    color: #fff;
}

.step-pill.completed {

    color: var(--cyan);
}

.step-pill .s-num {

    width: 24px;

    height: 24px;

    border-radius: 6px;

    background:
        rgba(255,255,255,.08);

    display: flex;

    align-items: center;

    justify-content: center;

    font-size: 12px;

    font-weight: 700;
}

.step-pill.active .s-num {

    background:
        linear-gradient(
            135deg,
            var(--pri),
            var(--cyan)
        );

    color: #fff;
}

.step-pill.completed .s-num {

    background: var(--cyan);

    color: #0b0f19;
}


/* =========================================================
STEP CONTENT
========================================================= */

.step-content {

    display: none;

    animation:
        fadeIn .25s ease-out forwards;
}

.step-content.active {

    display: block;
}

@keyframes fadeIn {

    from {
        opacity: 0;
        transform: translateY(6px);
    }

    to {
        opacity: 1;
        transform: translateY(0);
    }
}


/* =========================================================
PANELS
========================================================= */

.panel {

    background:
        var(--card-bg);

    border:
        1px solid var(--card-border);

    border-radius: 16px;

    padding: 24px;

    box-shadow:
        0 8px 30px rgba(0,0,0,.35);
}

.panel-hdr {

    margin-bottom: 20px;
}

.subhead {

    text-transform: uppercase;

    font-size: 11px;

    font-weight: 800;

    color: var(--pri);

    letter-spacing: .08em;

    margin-bottom: 4px;
}

.panel-hdr h2 {

    margin: 0;

    font-size: 20px;

    font-weight: 700;

    color: #fff;
}

.panel-hdr p {

    margin: 4px 0 0;

    font-size: 13px;

    color: var(--tx-dim);
}


/* =========================================================
GRIDS
========================================================= */

.dual-grid {

    display: grid;

    grid-template-columns:
        380px 1fr;

    gap: 20px;

    align-items: start;
}

.row-2 {

    display: grid;

    grid-template-columns:
        1fr 1fr;

    gap: 12px;
}

@media(max-width:960px) {

    .dual-grid {

        grid-template-columns: 1fr;
    }

    .row-2 {

        grid-template-columns: 1fr;
    }
}


/* =========================================================
FIELDS
========================================================= */

.field {

    margin-bottom: 16px;
}

.field label {

    display: flex;

    justify-content: space-between;

    font-size: 12px;

    font-weight: 700;

    text-transform: uppercase;

    letter-spacing: .04em;

    color: var(--tx-dim);

    margin-bottom: 6px;
}

.field label span.unit {

    text-transform: lowercase;

    color: var(--tx-muted);
}

input[type=text],
input[type=number],
input[type=password],
select,
textarea {

    width: 100%;

    background:
        #0d1424;

    border:
        1px solid var(--card-border);

    border-radius: 10px;

    color: #fff;

    padding:
        10px 14px;

    font-size: 14px;

    font-family: inherit;

    outline: none;

    transition: all .2s;
}

input:focus,
select:focus,
textarea:focus {

    border-color:
        var(--cyan);

    box-shadow:
        0 0 10px var(--cyan-glow);
}

textarea {

    resize: vertical;
}


/* =========================================================
UPLOAD
========================================================= */

.drop-box {

    display: block;

    border:
        2px dashed #2b395d;

    border-radius: 14px;

    background:
        rgba(13,20,36,.6);

    padding:
        30px 20px;

    text-align: center;

    transition: all .2s;

    position: relative;
}

.drop-box:hover,
.drop-box.dragover {

    border-color:
        var(--cyan);

    background:
        rgba(56,189,248,.05);
}

.drop-box input {

    display: block;

    margin: 10px auto;

    font-size: 13px;

    color: var(--tx-dim);

    cursor: pointer;

    width: 100%;

    background: transparent;

    border: none;
}

.drop-box .icon {

    font-size: 32px;

    margin-bottom: 8px;
}

.drop-box b {

    display: block;

    font-size: 15px;

    color: #fff;
}

.drop-box span {

    font-size: 13px;

    color: var(--tx-muted);
}

.file-tag {

    margin-top: 10px;

    display: inline-block;

    padding:
        4px 10px;

    border-radius: 6px;

    background:
        rgba(16,185,129,.15);

    border:
        1px solid var(--green);

    color: var(--green);

    font-size: 12px;

    font-weight: 600;
}


/* =========================================================
SLIDER
========================================================= */

.slider-box {

    display: flex;

    align-items: center;

    gap: 12px;
}

input[type=range] {

    flex: 1;

    height: 6px;

    background: #0d1424;

    border-radius: 4px;

    outline: none;

    accent-color: var(--cyan);
}

.slider-num {

    width: 70px !important;
}


/* =========================================================
SUMMARY
========================================================= */

.summary-card {

    background:
        #0d1424;

    border:
        1px solid var(--card-border);

    border-radius: 12px;

    padding: 14px;

    margin-top: 20px;
}

.summary-card-title {

    font-size: 11px;

    font-weight: 800;

    color: var(--tx-muted);

    text-transform: uppercase;

    letter-spacing: .05em;

    margin-bottom: 10px;
}

.summary-item {

    display: flex;

    align-items: center;

    justify-content: space-between;

    padding:
        8px 10px;

    border-radius: 8px;

    background:
        rgba(255,255,255,.02);

    font-size: 13px;

    margin-bottom: 6px;
}

.summary-item-left {

    display: flex;

    align-items: center;

    gap: 8px;

    overflow: hidden;

    text-overflow: ellipsis;

    white-space: nowrap;
}


/* =========================================================
PREVIEW
========================================================= */

.preview-board {

    background:
        #090e1a;

    border:
        1px solid var(--card-border);

    border-radius: 14px;

    padding: 16px;

    position: relative;

    display: flex;

    flex-direction: column;

    align-items: center;

    min-height: 480px;
}

.preview-tip {

    font-size: 13px;

    color: var(--cyan);

    margin-bottom: 12px;

    display: flex;

    align-items: center;

    gap: 6px;
}

#cv-wrap {

    position: relative;

    overflow: hidden;

    border-radius: 8px;

    box-shadow:
        0 10px 40px rgba(0,0,0,.6);

    background: #111;

    display: inline-block;

    max-width: 100%;
}

#cv {

    display: block;

    cursor: crosshair;

    max-width: 100%;

    height: auto;
}

.cross-h {

    position: absolute;

    left: 0;

    width: 100%;

    height: 1px;

    background: var(--red);

    box-shadow:
        0 0 6px var(--red);

    pointer-events: none;

    display: none;
}

.cross-v {

    position: absolute;

    top: 0;

    height: 100%;

    width: 1px;

    background: var(--red);

    box-shadow:
        0 0 6px var(--red);

    pointer-events: none;

    display: none;
}

.cross-badge {

    position: absolute;

    background:
        #0d1424;

    border:
        1px solid var(--card-border);

    color: #fff;

    font-size: 11px;

    font-weight: 700;

    padding:
        3px 8px;

    border-radius: 6px;

    pointer-events: none;

    transform:
        translate(-50%,-130%);

    white-space: nowrap;

    display: none;

    box-shadow:
        0 4px 12px rgba(0,0,0,.5);
}


/* =========================================================
BUTTONS
========================================================= */

.btn {

    display: inline-flex;

    align-items: center;

    justify-content: center;

    gap: 8px;

    font-family: inherit;

    font-weight: 700;

    font-size: 14px;

    padding:
        12px 24px;

    border-radius: 10px;

    cursor: pointer;

    transition: all .2s ease;

    color: #fff;

    border: none;
}

.btn-pri {

    background:
        linear-gradient(
            135deg,
            var(--pri),
            #6366f1
        );

    box-shadow:
        0 0 20px var(--pri-glow);
}

.btn-cyan {

    background:
        linear-gradient(
            135deg,
            #0284c7,
            var(--cyan)
        );

    color: #0b0f19;

    box-shadow:
        0 0 20px var(--cyan-glow);
}

.btn-red {

    background:
        linear-gradient(
            135deg,
            #ef4444,
            #dc2626
        );

    box-shadow:
        0 0 15px rgba(239,68,68,.4);
}

.btn-outline {

    background: transparent;

    border:
        1px solid var(--card-border);

    color: var(--tx-dim);
}

.btn:hover {

    filter: brightness(1.1);

    transform: translateY(-1px);
}

.btn:disabled {

    opacity: .4;

    cursor: not-allowed;

    transform: none !important;

    filter: none !important;
}


/* =========================================================
ACTION BAR
========================================================= */

.action-bar {

    display: flex;

    justify-content: space-between;

    align-items: center;

    margin-top: 24px;

    padding-top: 20px;

    border-top:
        1px solid var(--card-border);
}


/* =========================================================
PREVIEW CARDS
========================================================= */

.pv-grid {

    display: grid;

    grid-template-columns:
        repeat(
            auto-fit,
            minmax(280px,1fr)
        );

    gap: 16px;

    margin-top: 16px;
}

.pv-item {

    background:
        #0d1424;

    border:
        1px solid var(--card-border);

    border-radius: 12px;

    padding: 12px;
}

.pv-item b {

    display: block;

    font-size: 13px;

    margin-bottom: 8px;

    color: var(--cyan);
}

.pv-item iframe {

    width: 100%;

    height: 220px;

    border: 0;

    border-radius: 8px;

    background: #fff;
}


/* =========================================================
LOG
========================================================= */

.log-box {

    max-height: 240px;

    overflow-y: auto;

    margin-top: 16px;
}

.log-row {

    display: flex;

    align-items: flex-start;

    gap: 10px;

    padding:
        8px 12px;

    border-radius: 8px;

    font-size: 13px;

    margin-bottom: 6px;
}

.log-row.ok {

    background:
        rgba(16,185,129,.1);

    border:
        1px solid rgba(16,185,129,.2);
}

.log-row.fail {

    background:
        rgba(239,68,68,.1);

    border:
        1px solid rgba(239,68,68,.2);
}

.badge-pill {

    font-size: 10px;

    font-weight: 800;

    padding:
        2px 6px;

    border-radius: 4px;

    color: #fff;

    flex-shrink: 0;
}

.ok .badge-pill {

    background:
        var(--green);
}

.fail .badge-pill {

    background:
        var(--red);
}


/* =========================================================
PROGRESS
========================================================= */

.progress-track {

    height: 8px;

    background:
        #0d1424;

    border-radius: 99px;

    overflow: hidden;

    margin: 16px 0;

    display: none;
}

.progress-bar {

    height: 100%;

    width: 0;

    background:
        linear-gradient(
            90deg,
            var(--pri),
            var(--cyan)
        );

    transition:
        width .2s ease;
}


/* =========================================================
NOTICE
========================================================= */

.notice {

    padding:
        12px 16px;

    border-radius: 10px;

    font-size: 13px;

    margin: 14px 0;

    display: none;
}

.notice.info {

    display: block;

    background:
        rgba(56,189,248,.1);

    border:
        1px solid var(--cyan);

    color: var(--cyan);
}

.notice.err {

    display: block;

    background:
        rgba(239,68,68,.1);

    border:
        1px solid var(--red);

    color: #fca5a5;
}

.notice.ok {

    display: block;

    background:
        rgba(16,185,129,.1);

    border:
        1px solid var(--green);

    color: #86efac;
}

</style>

</head>


<body>


<!-- =======================================================
HEADER
======================================================= -->

<header>

    <div class="brand">

        <div class="logo-box">

            <img
                src="/logo.img"
                alt="AI Interest Group Logo"
            >

        </div>

        <div>

            <div class="brand-title">
                AI Interest Group || Hashemite University
            </div>

            <div class="brand-sub">
                Certificate Hub
            </div>

        </div>

    </div>


    <div class="user-badge">

        <span class="dot-online"></span>

        <span>
            Ready
        </span>

    </div>

</header>



<main>


<!-- =======================================================
STEP NAVIGATION
======================================================= -->

<div class="steps-nav">

    <button
        type="button"
        class="step-pill active"
        id="pill-1"
    >

        <span class="s-num">
            1
        </span>

        <span>
            Upload Data &amp; Template
        </span>

    </button>


    <button
        type="button"
        class="step-pill"
        id="pill-2"
    >

        <span class="s-num">
            2
        </span>

        <span>
            Position &amp; Customize Text
        </span>

    </button>


    <button
        type="button"
        class="step-pill"
        id="pill-3"
    >

        <span class="s-num">
            3
        </span>

        <span>
            Email Settings
        </span>

    </button>


    <button
        type="button"
        class="step-pill"
        id="pill-4"
    >

        <span class="s-num">
            4
        </span>

        <span>
            Review &amp; Launch
        </span>

    </button>

</div>



<!-- =======================================================
STEP 1
======================================================= -->

<div
    class="step-content active"
    id="step-1"
>

<div class="panel">

    <div class="panel-hdr">

        <div class="subhead">
            Step 1
        </div>

        <h2>
            Upload Data &amp; Certificate Template
        </h2>

        <p>
            Provide your participant list via CSV or Google Sheet,
            and the certificate template PDF.
        </p>

    </div>


    <div
        class="row-2"
        style="gap:24px"
    >


        <!-- CSV -->

        <div>

            <label class="field">

                <span style="font-weight:700">
                    1. PARTICIPANT LIST
                </span>

            </label>


            <label
                class="drop-box"
                id="dzcsv"
            >

                <div class="icon">
                    📊
                </div>

                <b>
                    Select your CSV file
                </b>

                <span>
                    Must contain "name" and "email" headers
                </span>

                <input
                    type="file"
                    id="csv"
                    accept=".csv"
                >

                <div id="csvname"></div>

            </label>


            <div style="margin-top:16px">

                <div
                    style="
                        font-size:12px;
                        color:var(--tx-muted);
                        text-transform:uppercase;
                        font-weight:700;
                        margin-bottom:6px
                    "
                >
                    Or Link a Google Sheet
                </div>


                <input
                    type="text"
                    id="sheet"
                    placeholder="https://docs.google.com/spreadsheets/d/..."
                >


                <span
                    style="
                        font-size:12px;
                        color:var(--tx-muted);
                        display:block;
                        margin-top:4px
                    "
                >
                    Sheet must be set to
                    "Anyone with the link can view".
                </span>

            </div>

        </div>



        <!-- TEMPLATE -->

        <div>

            <label class="field">

                <span style="font-weight:700">
                    2. CERTIFICATE PDF TEMPLATE
                </span>

            </label>


            <label
                class="drop-box"
                id="dztpl"
            >

                <div class="icon">
                    📜
                </div>

                <b>
                    Select your Certificate PDF
                </b>

                <span>
                    Single page landscape or portrait template
                </span>

                <input
                    type="file"
                    id="tpl"
                    accept=".pdf"
                >

                <div id="tplname"></div>

            </label>

        </div>

    </div>



    <div class="action-bar">

        <div></div>

        <button
            type="button"
            class="btn btn-cyan"
            id="next-step-1"
        >

            <span>
                Next: Position &amp; Customize Text
            </span>

            <span>
                →
            </span>

        </button>

    </div>

</div>

</div>



<!-- =======================================================
STEP 2
======================================================= -->

<div
    class="step-content"
    id="step-2"
>

<div class="dual-grid">


    <!-- LEFT -->

    <div class="panel">

        <div class="panel-hdr">

            <div class="subhead">
                Left Panel
            </div>

            <h2>
                Design &amp; Positioning
            </h2>

        </div>


        <!-- FONT SIZE -->

        <div class="field">

            <label>

                Font Size

                <span class="unit">
                    (pt)
                </span>

            </label>


            <div class="slider-box">

                <input
                    type="range"
                    id="size-slider"
                    min="12"
                    max="96"
                    value="32"
                >

                <input
                    type="number"
                    id="size"
                    class="slider-num"
                    value="32"
                >

            </div>

        </div>



        <!-- COLOR + STYLE -->

        <div class="row-2">

            <div class="field">

                <label>
                    Font Color
                </label>

                <input
                    type="color"
                    id="color"
                    value="#000000"
                    style="
                        padding:2px;
                        height:42px;
                        cursor:pointer
                    "
                >

            </div>


            <div class="field">

                <label>
                    Font Style
                </label>

                <select id="bold">

                    <option value="false">
                        Regular
                    </option>

                    <option value="true">
                        Bold
                    </option>

                </select>

            </div>

        </div>



        <!-- X Y -->

        <div class="row-2">

            <div class="field">

                <label>

                    X Coordinate

                    <span class="unit">
                        (pt)
                    </span>

                </label>

                <input
                    type="number"
                    id="x"
                    value="297"
                >

            </div>


            <div class="field">

                <label>

                    Y Coordinate

                    <span class="unit">
                        (pt)
                    </span>

                </label>

                <input
                    type="number"
                    id="y"
                    value="184"
                >

            </div>

        </div>



        <!-- ALIGN -->

        <div class="field">

            <label>
                Text Alignment at X
            </label>

            <select id="align">

                <option value="center">
                    Center
                </option>

                <option value="left">
                    Left
                </option>

                <option value="right">
                    Right
                </option>

            </select>

        </div>



        <!-- SUMMARY -->

        <div class="summary-card">

            <div class="summary-card-title">
                Upload Summary
            </div>


            <div class="summary-item">

                <div class="summary-item-left">

                    <span>
                        📄
                    </span>

                    <span id="sum-tpl-name">
                        No template selected
                    </span>

                </div>

            </div>


            <div class="summary-item">

                <div class="summary-item-left">

                    <span>
                        👥
                    </span>

                    <span id="sum-csv-name">
                        No data source linked
                    </span>

                </div>

            </div>

        </div>



        <!-- BUTTONS -->

        <div
            style="
                margin-top:20px;
                display:flex;
                gap:10px
            "
        >

            <button
                type="button"
                class="btn btn-outline"
                id="back-step-2"
            >
                Back
            </button>


            <button
                type="button"
                class="btn btn-cyan"
                style="flex:1"
                id="btn-gen"
            >

                Generate &amp; Continue →

            </button>

        </div>


        <div
            id="genmsg"
            class="notice"
        ></div>

    </div>



    <!-- RIGHT -->

    <div class="panel">

        <div
            class="panel-hdr"
            style="
                display:flex;
                justify-content:space-between;
                align-items:center
            "
        >

            <div>

                <div class="subhead">
                    Right Panel
                </div>

                <h2>
                    Interactive Preview
                </h2>

            </div>


            <span class="preview-tip">

                🎯 Click on the template to set
                the name placement

            </span>

        </div>


        <div class="preview-board">


            <div id="cv-wrap">

                <canvas id="cv"></canvas>

                <div
                    id="line-x"
                    class="cross-h"
                ></div>

                <div
                    id="line-y"
                    class="cross-v"
                ></div>

                <div
                    id="point-badge"
                    class="cross-badge"
                >
                    Clicked Point X: 297, Y: 184
                </div>

            </div>


            <p
                id="no-tpl-msg"
                style="
                    color:var(--tx-muted);
                    font-size:14px;
                    margin-top:140px
                "
            >
                Please upload a PDF template in Step 1
                to interactively click and calibrate coordinates.
            </p>


        </div>

    </div>

</div>

</div>



<!-- =======================================================
STEP 3
======================================================= -->

<div
    class="step-content"
    id="step-3"
>

<div class="panel">

    <div class="panel-hdr">

        <div class="subhead">
            Step 3
        </div>

        <h2>
            Email &amp; SMTP Configuration
        </h2>

        <p>
            Provide the sending mailbox credentials.
            Passwords are sent securely and never stored.
        </p>

    </div>



    <div
        class="row-2"
        style="gap:20px"
    >

        <div class="field">

            <label>
                SMTP Host
            </label>

            <input
                type="text"
                id="host"
                value="smtp.gmail.com"
            >

        </div>


        <div class="field">

            <label>
                Port
                (587 for TLS, 465 for SSL)
            </label>

            <input
                type="number"
                id="port"
                value="587"
            >

        </div>

    </div>



    <div
        class="row-2"
        style="gap:20px"
    >

        <div class="field">

            <label>
                Username / Email
            </label>

            <input
                type="text"
                id="user"
                placeholder="your_email@gmail.com"
            >

        </div>


        <div class="field">

            <label>
                App Password
            </label>

            <input
                type="password"
                id="pass"
                placeholder="16-character Google App Password"
            >

        </div>

    </div>



    <div class="field">

        <label>
            From Display Name (Optional)
        </label>

        <input
            type="text"
            id="from"
            placeholder="AI Interest Group || Hashemite University"
        >

    </div>



    <div class="field">

        <label>
            Email Subject
            (Supports {name} variable)
        </label>

        <input
            type="text"
            id="subject"
            value="Certificate of Completion: {name}"
        >

    </div>



    <div class="field">

        <label>
            Email Body
            (Supports {name} variable)
        </label>

        <textarea
            id="body"
            rows="6"
        >Dear {name},

Congratulations on your active participation with the AI Interest Group at Hashemite University!

Your verified certificate is attached to this email.

Best regards,
Artificial Intelligence Interest Group
Hashemite University</textarea>

    </div>



    <div class="action-bar">

        <button
            type="button"
            class="btn btn-outline"
            id="back-step-3"
        >
            ← Back to Positioning
        </button>


        <button
            type="button"
            class="btn btn-cyan"
            id="next-step-3"
        >
            Next: Review &amp; Launch →
        </button>

    </div>

</div>

</div>



<!-- =======================================================
STEP 4
======================================================= -->

<div
    class="step-content"
    id="step-4"
>

<div class="panel">

    <div class="panel-hdr">

        <div class="subhead">
            Step 4
        </div>

        <h2>
            Review &amp; Launch Distribution
        </h2>

        <p>
            Inspect sample certificates before initiating batch dispatch.
        </p>

    </div>



    <div id="pv-container">

        <div
            style="
                color:var(--tx-muted);
                font-size:14px
            "
            id="pv-empty"
        >

            No generated certificates available yet.
            Return to Step 2 and click
            <b>Generate &amp; Continue</b>.

        </div>


        <div
            class="pv-grid"
            id="pv"
        ></div>

    </div>



    <div
        style="
            margin-top:24px;
            padding:16px;
            background:#0d1424;
            border-radius:12px;
            border:1px solid var(--card-border)
        "
    >

        <label
            style="
                display:flex;
                align-items:center;
                gap:12px;
                cursor:pointer
            "
        >

            <input
                type="checkbox"
                id="confirm"
                style="
                    width:20px;
                    height:20px;
                    accent-color:var(--cyan)
                "
                disabled
            >

            <span
                style="
                    font-size:14px;
                    font-weight:600
                "
            >
                I have inspected the sample certificate
                layout and confirm batch delivery to all recipients.
            </span>

        </label>

    </div>



    <div class="action-bar">

        <button
            type="button"
            class="btn btn-outline"
            id="back-step-4"
        >
            ← Back to Settings
        </button>


        <button
            type="button"
            class="btn btn-red"
            id="btn-send"
            disabled
        >
            🚀 Dispatch All Certificates
        </button>

    </div>



    <div
        class="progress-track"
        id="barwrap"
    >

        <div
            class="progress-bar"
            id="bar"
        ></div>

    </div>


    <div
        id="sum"
        style="
            font-size:14px;
            font-weight:700;
            margin-top:10px;
            color:var(--cyan)
        "
    ></div>


    <div
        class="log-box"
        id="log"
    ></div>

</div>

</div>


</main>



<!-- =======================================================
PDF.JS
======================================================= -->

<script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>



<!-- =======================================================
JAVASCRIPT
======================================================= -->

<script>

"use strict";


/* =========================================================
GLOBAL STATE
========================================================= */

const $ = function(id) {
    return document.getElementById(id);
};


let pageW = 0;
let pageH = 0;
let scale = 1;

let currentJob = null;
let totalCount = 0;


/* =========================================================
STEP NAVIGATION
========================================================= */

function switchStep(step) {

    console.log("Switching to step:", step);

    for (let i = 1; i <= 4; i++) {

        const content = $("step-" + i);
        const pill = $("pill-" + i);

        if (content) {

            content.classList.toggle(
                "active",
                i === step
            );

        }

        if (pill) {

            pill.classList.toggle(
                "active",
                i === step
            );

            if (i < step) {

                pill.classList.add(
                    "completed"
                );

            } else {

                pill.classList.remove(
                    "completed"
                );
            }
        }
    }

    window.scrollTo({
        top: 0,
        behavior: "smooth"
    });
}


/* =========================================================
SETUP DROP ZONE
========================================================= */

function setupDrop(
    dzId,
    inputId,
    labelId,
    isTemplate
) {

    const dz = $(dzId);
    const input = $(inputId);
    const label = $(labelId);

    if (!dz || !input || !label) {

        console.error(
            "Drop zone element missing:",
            dzId,
            inputId,
            labelId
        );

        return;
    }


    input.addEventListener(
        "change",
        function() {

            if (!input.files || !input.files[0]) {
                return;
            }

            const file = input.files[0];

            label.innerHTML =
                '<span class="file-tag">✔ '
                + escapeHtml(file.name)
                + '</span>';


            if (isTemplate) {

                $("sum-tpl-name").textContent =
                    file.name;

                loadPdfPreview(file);

            } else {

                $("sum-csv-name").textContent =
                    file.name;
            }
        }
    );


    ["dragenter", "dragover"].forEach(
        function(eventName) {

            dz.addEventListener(
                eventName,
                function(event) {

                    event.preventDefault();

                    dz.classList.add(
                        "dragover"
                    );
                }
            );
        }
    );


    ["dragleave", "drop"].forEach(
        function(eventName) {

            dz.addEventListener(
                eventName,
                function(event) {

                    event.preventDefault();

                    dz.classList.remove(
                        "dragover"
                    );
                }
            );
        }
    );


    dz.addEventListener(
        "drop",
        function(event) {

            if (
                event.dataTransfer &&
                event.dataTransfer.files.length
            ) {

                try {

                    input.files =
                        event.dataTransfer.files;

                    input.dispatchEvent(
                        new Event("change")
                    );

                } catch (error) {

                    console.error(
                        "Could not set dropped files:",
                        error
                    );
                }
            }
        }
    );
}


/* =========================================================
ESCAPE HTML
========================================================= */

function escapeHtml(value) {

    return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}


/* =========================================================
UPDATE CROSSHAIRS
========================================================= */

function updateCrosshairs() {

    if (!pageW || !pageH) {
        return;
    }

    const xVal =
        parseFloat($("x").value) || 0;

    const yVal =
        parseFloat($("y").value) || 0;


    const px =
        xVal * scale;

    const py =
        (pageH - yVal) * scale;


    $("line-x").style.display =
        "block";

    $("line-x").style.top =
        py + "px";


    $("line-y").style.display =
        "block";

    $("line-y").style.left =
        px + "px";


    const badge =
        $("point-badge");


    badge.style.display =
        "block";

    badge.style.left =
        px + "px";

    badge.style.top =
        py + "px";


    badge.textContent =
        "Clicked Point X: "
        + Math.round(xVal)
        + ", Y: "
        + Math.round(yVal);
}


/* =========================================================
PDF PREVIEW
========================================================= */

async function loadPdfPreview(file) {

    try {

        if (
            typeof pdfjsLib ===
            "undefined"
        ) {

            console.error(
                "PDF.js is not loaded."
            );

            return;
        }


        const buffer =
            await file.arrayBuffer();


        const pdf =
            await pdfjsLib
                .getDocument({
                    data: buffer
                })
                .promise;


        const page =
            await pdf.getPage(1);


        const originalViewport =
            page.getViewport({
                scale: 1
            });


        pageW =
            originalViewport.width;

        pageH =
            originalViewport.height;


        scale =
            Math.min(
                1,
                720 / pageW
            );


        const viewport =
            page.getViewport({
                scale: scale
            });


        const canvas =
            $("cv");


        canvas.width =
            viewport.width;

        canvas.height =
            viewport.height;


        const context =
            canvas.getContext("2d");


        await page.render({

            canvasContext:
                context,

            viewport:
                viewport

        }).promise;


        $("no-tpl-msg").style.display =
            "none";


        if (
            !$("x").value ||
            $("x").value === "297"
        ) {

            $("x").value =
                Math.round(pageW / 2);
        }


        if (
            !$("y").value ||
            $("y").value === "184"
        ) {

            $("y").value =
                Math.round(pageH / 2);
        }


        updateCrosshairs();


    } catch (error) {

        console.error(
            "PDF Preview Error:",
            error
        );

        $("no-tpl-msg").textContent =
            "Unable to preview this PDF. "
            + "You can still enter coordinates manually.";

        $("no-tpl-msg").style.display =
            "block";
    }
}


/* =========================================================
GENERATE CERTIFICATES
========================================================= */

async function generateCertificates() {

    const template =
        $("tpl").files[0];

    if (!template) {

        alert(
            "Please select your PDF template in Step 1 first."
        );

        switchStep(1);

        return;
    }


    const csvFile =
        $("csv").files[0];

    const sheetUrl =
        $("sheet").value.trim();


    if (!csvFile && !sheetUrl) {

        alert(
            "Please upload a CSV file or provide a Google Sheet URL."
        );

        switchStep(1);

        return;
    }


    const formData =
        new FormData();


    formData.append(
        "template",
        template
    );


    if (csvFile) {

        formData.append(
            "csv",
            csvFile
        );
    }


    formData.append(
        "sheet_url",
        sheetUrl
    );


    formData.append(
        "x",
        $("x").value
    );

    formData.append(
        "y",
        $("y").value
    );

    formData.append(
        "size",
        $("size").value
    );

    formData.append(
        "align",
        $("align").value
    );

    formData.append(
        "color",
        $("color").value
    );

    formData.append(
        "bold",
        $("bold").value
    );


    const message =
        $("genmsg");


    const button =
        $("btn-gen");


    message.className =
        "notice info";

    message.textContent =
        "Processing and generating certificates...";


    button.disabled = true;


    try {

        const response =
            await fetch(
                "/generate",
                {
                    method: "POST",
                    body: formData
                }
            );


        const data =
            await response.json();


        button.disabled =
            false;


        if (!data.ok) {

            message.className =
                "notice err";

            message.textContent =
                "Error: " + data.error;

            return;
        }


        currentJob =
            data.job;

        totalCount =
            data.count;


        message.className =
            "notice ok";

        message.textContent =
            "Generated "
            + data.count
            + " certificates successfully!";


        $("pv-empty").style.display =
            "none";


        let html = "";


        for (
            let i = 0;
            i < data.previews.length;
            i++
        ) {

            const preview =
                data.previews[i];


            html +=
                '<div class="pv-item">'

                + '<b>'
                + escapeHtml(preview.name)
                + '</b>'

                + '<iframe src="'
                + escapeHtml(preview.url)
                + '"></iframe>'

                + '</div>';
        }


        $("pv").innerHTML =
            html;


        $("confirm").disabled =
            false;


        /*
         * IMPORTANT:
         * After generation we intentionally
         * move to STEP 3.
         */

        switchStep(3);


    } catch (error) {

        console.error(
            "Generate error:",
            error
        );


        button.disabled =
            false;


        message.className =
            "notice err";

        message.textContent =
            "Connection error. Is the Flask server running?";
    }
}


/* =========================================================
SEND ALL
========================================================= */

async function sendAllCertificates() {

    const username =
        $("user").value.trim();

    const password =
        $("pass").value.trim();


    if (!username || !password) {

        alert(
            "Please provide your SMTP username and App Password in Step 3."
        );

        switchStep(3);

        return;
    }


    if (!currentJob) {

        alert(
            "No generated certificates were found."
        );

        switchStep(2);

        return;
    }


    const confirmed =
        window.confirm(
            "Are you sure you want to dispatch "
            + totalCount
            + " certificates now?"
        );


    if (!confirmed) {
        return;
    }


    $("btn-send").disabled =
        true;

    $("confirm").disabled =
        true;

    $("log").innerHTML =
        "";

    $("barwrap").style.display =
        "block";

    $("bar").style.width =
        "0%";


    let successCount = 0;
    let failCount = 0;


    for (
        let i = 0;
        i < totalCount;
        i++
    ) {

        $("sum").textContent =
            "Dispatching certificate "
            + (i + 1)
            + " of "
            + totalCount
            + "...";


        let result;


        try {

            const response =
                await fetch(
                    "/send_one",
                    {
                        method: "POST",

                        headers: {
                            "Content-Type":
                                "application/json"
                        },

                        body: JSON.stringify({

                            job:
                                currentJob,

                            idx:
                                i,

                            confirm:
                                true,

                            host:
                                $("host").value,

                            port:
                                $("port").value,

                            user:
                                $("user").value,

                            password:
                                $("pass").value,

                            from:
                                $("from").value,

                            subject:
                                $("subject").value,

                            body:
                                $("body").value
                        })
                    }
                );


            result =
                await response.json();


        } catch (error) {

            console.error(
                "Email error:",
                error
            );


            result = {

                ok: false,

                error:
                    "Network transfer error"
            };
        }


        const row =
            document.createElement(
                "div"
            );


        row.className =
            "log-row "
            + (
                result.ok
                    ? "ok"
                    : "fail"
            );


        const badge =
            document.createElement(
                "span"
            );


        badge.className =
            "badge-pill";

        badge.textContent =
            result.ok
                ? "SENT"
                : "FAILED";


        const information =
            document.createElement(
                "div"
            );


        const recipientName =
            result.name ||
            "Recipient " + (i + 1);


        const recipientEmail =
            result.email ||
            "";


        information.innerHTML =
            "<b>"
            + escapeHtml(recipientName)
            + "</b> &lt;"
            + escapeHtml(recipientEmail)
            + "&gt;";


        if (!result.ok) {

            const error =
                document.createElement(
                    "div"
                );


            error.style.color =
                "var(--red)";

            error.style.fontSize =
                "12px";

            error.style.marginTop =
                "2px";

            error.textContent =
                result.error ||
                "Unknown error";


            information.appendChild(
                error
            );
        }


        row.appendChild(
            badge
        );

        row.appendChild(
            information
        );


        $("log").appendChild(
            row
        );


        if (result.ok) {

            successCount++;

        } else {

            failCount++;
        }


        $("bar").style.width =
            (
                (
                    (i + 1)
                    /
                    totalCount
                )
                * 100
            )
            + "%";
    }


    $("sum").textContent =
        "Delivery completed: "
        + successCount
        + " sent successfully, "
        + failCount
        + " failed.";
}


/* =========================================================
INITIALIZE APP
========================================================= */

document.addEventListener(
    "DOMContentLoaded",
    function() {

        console.log(
            "Certificate Hub initialized."
        );


        /* -----------------------------------------
           PDF.js
        ----------------------------------------- */

        if (
            typeof pdfjsLib !==
            "undefined"
        ) {

            pdfjsLib.GlobalWorkerOptions.workerSrc =
                "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";

        } else {

            console.warn(
                "PDF.js is not available yet."
            );
        }


        /* -----------------------------------------
           DROP ZONES
        ----------------------------------------- */

        setupDrop(
            "dzcsv",
            "csv",
            "csvname",
            false
        );


        setupDrop(
            "dztpl",
            "tpl",
            "tplname",
            true
        );


        /* -----------------------------------------
           GOOGLE SHEET
        ----------------------------------------- */

        $("sheet").addEventListener(
            "input",
            function(event) {

                if (
                    event.target.value.trim()
                ) {

                    $("sum-csv-name").textContent =
                        "Google Sheet URL Linked";

                } else {

                    $("sum-csv-name").textContent =
                        "No data source linked";
                }
            }
        );


        /* -----------------------------------------
           X/Y COORDINATES
        ----------------------------------------- */

        $("x").addEventListener(
            "input",
            updateCrosshairs
        );

        $("y").addEventListener(
            "input",
            updateCrosshairs
        );


        /* -----------------------------------------
           FONT SIZE
        ----------------------------------------- */

        $("size-slider").addEventListener(
            "input",
            function(event) {

                $("size").value =
                    event.target.value;
            }
        );


        $("size").addEventListener(
            "input",
            function(event) {

                $("size-slider").value =
                    event.target.value;
            }
        );


        /* -----------------------------------------
           CANVAS CLICK
        ----------------------------------------- */

        $("cv").addEventListener(
            "click",
            function(event) {

                if (!pageW || !pageH) {
                    return;
                }


                const rect =
                    $("cv").getBoundingClientRect();


                const clickX =
                    event.clientX -
                    rect.left;


                const clickY =
                    event.clientY -
                    rect.top;


                $("x").value =
                    Math.round(
                        clickX / scale
                    );


                $("y").value =
                    Math.round(
                        pageH -
                        (
                            clickY / scale
                        )
                    );


                updateCrosshairs();
            }
        );


        /* =================================================
           STEP 1 → STEP 2

           THIS IS THE IMPORTANT FIX
        ================================================= */

        $("next-step-1").addEventListener(
            "click",
            function() {

                console.log(
                    "NEXT STEP 1 clicked"
                );

                switchStep(2);
            }
        );


        /* -----------------------------------------
           STEP 2 → STEP 1
        ----------------------------------------- */

        $("back-step-2").addEventListener(
            "click",
            function() {

                switchStep(1);
            }
        );


        /* -----------------------------------------
           GENERATE
        ----------------------------------------- */

        $("btn-gen").addEventListener(
            "click",
            generateCertificates
        );


        /* -----------------------------------------
           STEP 3 → STEP 2
        ----------------------------------------- */

        $("back-step-3").addEventListener(
            "click",
            function() {

                switchStep(2);
            }
        );


        /* -----------------------------------------
           STEP 3 → STEP 4
        ----------------------------------------- */

        $("next-step-3").addEventListener(
            "click",
            function() {

                console.log(
                    "NEXT STEP 3 clicked"
                );

                if (!currentJob) {

                    alert(
                        "Please generate the certificates in Step 2 first."
                    );

                    switchStep(2);

                    return;
                }


                switchStep(4);
            }
        );


        /* -----------------------------------------
           STEP 4 → STEP 3
        ----------------------------------------- */

        $("back-step-4").addEventListener(
            "click",
            function() {

                switchStep(3);
            }
        );


        /* -----------------------------------------
           CONFIRM CHECKBOX
        ----------------------------------------- */

        $("confirm").addEventListener(
            "change",
            function(event) {

                $("btn-send").disabled =
                    !event.target.checked;
            }
        );


        /* -----------------------------------------
           SEND
        ----------------------------------------- */

        $("btn-send").addEventListener(
            "click",
            sendAllCertificates
        );


        /* -----------------------------------------
           STEP PILLS
           Allow navigation by clicking them
        ----------------------------------------- */

        $("pill-1").addEventListener(
            "click",
            function() {

                switchStep(1);
            }
        );


        $("pill-2").addEventListener(
            "click",
            function() {

                switchStep(2);
            }
        );


        $("pill-3").addEventListener(
            "click",
            function() {

                if (currentJob) {

                    switchStep(3);

                } else {

                    alert(
                        "Please generate certificates first."
                    );
                }
            }
        );


        $("pill-4").addEventListener(
            "click",
            function() {

                if (currentJob) {

                    switchStep(4);

                } else {

                    alert(
                        "Please generate certificates first."
                    );
                }
            }
        );


        console.log(
            "All Certificate Hub event listeners loaded successfully."
        );

    }
);

</script>


</body>

</html>
"""


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    print("")
    print("=" * 60)
    print("AI Interest Group || Hashemite University")
    print("Certificate Hub")
    print("=" * 60)
    print("")
    print("Server running at:")
    print("http://127.0.0.1:5000")
    print("")
    print("Press CTRL+C to stop the server.")
    print("")

    app.run(
        debug=False,
        host="127.0.0.1",
        port=5000
    )