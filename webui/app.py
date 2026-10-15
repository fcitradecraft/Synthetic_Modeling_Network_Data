import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, send_file, request

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = REPO_ROOT / "aml-env" / "bin" / "python3"
MAIN_PY = REPO_ROOT / "main.py"
RUNS_DIR = REPO_ROOT / "data" / "runs"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

app = Flask(__name__)


def _parse_int(form, name, default=None):
    raw = (form.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return "invalid"


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/generate", methods=["POST"])
def generate():
    form = request.form

    n_persons = _parse_int(form, "n_persons")
    n_companies = _parse_int(form, "n_companies")
    n_flagged = _parse_int(form, "n_flagged", default=0)
    seed = _parse_int(form, "seed")
    start_date = form.get("start_date") or "2025-01-01"
    end_date = form.get("end_date") or "2025-01-31"
    fmt = form.get("format", "csv")

    errors = []
    if not isinstance(n_persons, int) or n_persons < 1:
        errors.append("Number of persons must be a positive whole number.")
    if not isinstance(n_companies, int) or n_companies < 1:
        errors.append("Number of companies must be a positive whole number.")
    if not isinstance(n_flagged, int) or n_flagged < 0:
        errors.append("Number flagged must be zero or a positive whole number.")
    if seed == "invalid":
        errors.append("Seed must be a whole number (or left blank).")
    if fmt not in ("csv", "xlsx"):
        errors.append("Format must be csv or xlsx.")

    if errors:
        return jsonify({"errors": errors}), 400

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    args = [
        str(PYTHON), str(MAIN_PY),
        "--n_persons", str(n_persons),
        "--n_companies", str(n_companies),
        "--n_flagged", str(n_flagged),
        "--start_date", start_date,
        "--end_date", end_date,
        "--format", fmt,
        "--output", str(run_dir / "aml_dataset.csv"),
        "--customer_info", str(run_dir / "customer_info.csv"),
        "--answer_key", str(run_dir / "answer_key.csv"),
        "--combined_output", str(run_dir / "aml_dataset_combined.xlsx"),
    ]
    if isinstance(seed, int):
        args += ["--seed", str(seed)]

    result = subprocess.run(args, cwd=str(REPO_ROOT), capture_output=True, text=True)
    log_output = ANSI_RE.sub("", (result.stdout or "") + (result.stderr or "")).strip()

    if result.returncode != 0:
        shutil.rmtree(run_dir, ignore_errors=True)
        return jsonify({"errors": [log_output or "Generation failed with no output."]}), 400

    zip_base = RUNS_DIR / f"aml_exercise_{run_id}"
    zip_path = shutil.make_archive(str(zip_base), "zip", root_dir=str(run_dir))
    shutil.rmtree(run_dir, ignore_errors=True)

    return send_file(zip_path, as_attachment=True, download_name=f"aml_exercise_{run_id}.zip")


if __name__ == "__main__":
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host="127.0.0.1", port=5000, debug=False)
