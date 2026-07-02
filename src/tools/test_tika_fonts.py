# Diagnostic script: verify Tika extracts text from .docx files with multiple font styles/sizes.
# Usage:
#   cd c:/Users/hp570003259/RADAR
#   .venv/Scripts/python.exe src/tools/test_tika_fonts.py

import os
import sys
import requests

# Allow direct run without installing the package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
FONT_DOCS_DIR = os.path.join(ROOT, 'test_data', 'font-size-docs')
TIKA_PORTS = [9998, 9999, 10000, 10001]
OPENSEARCH_URL = 'http://localhost:9200'

# Target sample: spread across the size range
SAMPLE_SIZES = [50, 75, 100, 125, 150, 175, 200]


def check_services():
    """Check which Tika ports and OpenSearch are alive. Returns first healthy Tika port."""
    print("=" * 60)
    print("SERVICE STATUS")
    print("=" * 60)

    healthy_tika = None
    for port in TIKA_PORTS:
        try:
            r = requests.get(f'http://localhost:{port}/tika', timeout=3)
            status = 'UP' if r.status_code in (200, 405) else f'HTTP {r.status_code}'
            if r.status_code in (200, 405) and healthy_tika is None:
                healthy_tika = port
        except Exception:
            status = 'DOWN'
        print(f"  Tika  :{port}    {status}")

    try:
        r = requests.get(OPENSEARCH_URL, timeout=3)
        os_status = 'UP' if r.status_code == 200 else f'HTTP {r.status_code}'
    except Exception:
        os_status = 'DOWN'
    print(f"  OpenSearch :9200  {os_status}")
    print()

    return healthy_tika


def pick_sample_files():
    """Pick one file per target size; fall back to nearest available."""
    if not os.path.isdir(FONT_DOCS_DIR):
        print(f"ERROR: test data folder not found: {FONT_DOCS_DIR}")
        sys.exit(1)

    available = {}
    for fname in os.listdir(FONT_DOCS_DIR):
        if fname.endswith('.docx') and fname.startswith('font_styles_'):
            try:
                size = int(fname.replace('font_styles_', '').replace('px.docx', ''))
                available[size] = fname
            except ValueError:
                pass

    if not available:
        print(f"ERROR: no font_styles_Npx.docx files found in {FONT_DOCS_DIR}")
        sys.exit(1)

    sorted_sizes = sorted(available.keys())
    sample = []
    for target in SAMPLE_SIZES:
        # find nearest available size
        nearest = min(sorted_sizes, key=lambda s: abs(s - target))
        fname = available[nearest]
        full_path = os.path.join(FONT_DOCS_DIR, fname)
        if (fname, full_path) not in sample:
            sample.append((fname, full_path))

    return sample


def extract_with_tika(file_path, port):
    """Send file to Tika /rmeta/text and return extracted text or None."""
    url = f'http://localhost:{port}/rmeta/text'
    mime = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    try:
        with open(file_path, 'rb') as f:
            r = requests.put(
                url,
                data=f,
                headers={'Accept': 'application/json', 'Content-Type': mime},
                timeout=60,
            )
        if r.status_code == 200:
            docs = r.json()
            if docs and isinstance(docs, list):
                return docs[0].get('X-TIKA:content') or ''
        return None
    except Exception as e:
        return None


def run():
    tika_port = check_services()
    if tika_port is None:
        print("ERROR: No Tika instance is running.")
        print("       Run start_everything.bat first, then retry.")
        sys.exit(1)
    print(f"Using Tika on port {tika_port}\n")

    sample = pick_sample_files()

    print("=" * 60)
    print(f"FONT EXTRACTION TEST  ({len(sample)} files)")
    print("=" * 60)

    passed = 0
    failed = 0

    for fname, fpath in sample:
        text = extract_with_tika(fpath, tika_port)

        if text is None:
            status = 'ERROR'
            detail = 'Tika returned no response'
            failed += 1
        elif len(text.strip()) < 20:
            status = 'FAIL'
            detail = f'{len(text.strip())} chars — text missing or too short'
            failed += 1
        else:
            status = 'PASS'
            detail = f'{len(text.strip())} chars extracted'
            passed += 1

        marker = '[PASS]' if status == 'PASS' else f'[{status}]'
        print(f"\n{marker} {fname} — {detail}")

        if text and len(text.strip()) > 0:
            preview = text.strip()[:300].replace('\n', ' ')
            print(f"       Preview: \"{preview}\"")
        elif text is not None:
            print(f"       (empty response)")

    print()
    print("=" * 60)
    print(f"RESULT: {passed} passed, {failed} failed out of {len(sample)} files")
    if failed == 0:
        print("All font styles extracted successfully.")
    else:
        print("Some files failed — check Tika config or restart Tika with start_everything.bat")
    print("=" * 60)


if __name__ == '__main__':
    run()
