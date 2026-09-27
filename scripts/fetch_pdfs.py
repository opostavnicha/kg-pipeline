"""Download the corpus PDFs listed in config/sources.json into pdfs/ and verify each SHA-256.

Existing files with the right hash are skipped; a file with the wrong hash is re-downloaded, and the
script exits non-zero if any download still doesn't match.

Usage: python scripts/fetch_pdfs.py [--only P175721,P510381] [--dest pdfs] [--check]
  --check  only verify files already in pdfs/; download nothing
"""

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "config" / "sources.json"
USER_AGENT = "Mozilla/5.0 (kg-pipeline fetch_pdfs.py)"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download(url: str, dest: Path) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as f:
        while block := resp.read(1 << 20):
            f.write(block)
    tmp.replace(dest)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", help="comma-separated project IDs")
    ap.add_argument("--dest", default=str(ROOT / "pdfs"))
    ap.add_argument("--check", action="store_true", help="verify existing files only")
    args = ap.parse_args()

    docs = json.loads(SOURCES.read_text())["documents"]
    if args.only:
        wanted = {p.strip() for p in args.only.split(",")}
        docs = [d for d in docs if d["project_id"] in wanted]
    dest_dir = Path(args.dest)
    dest_dir.mkdir(parents=True, exist_ok=True)

    failures = 0
    for d in docs:
        path = dest_dir / d["filename"]
        status = ""
        if path.exists() and sha256(path) == d["sha256"]:
            status = "ok (already present)"
        elif args.check:
            status = "MISSING" if not path.exists() else "HASH MISMATCH"
            failures += 1
        else:
            try:
                download(d["url"], path)
                status = "downloaded, hash ok" if sha256(path) == d["sha256"] else "DOWNLOADED BUT HASH MISMATCH"
            except Exception as e:  # network errors: report and continue with the rest
                status = f"DOWNLOAD FAILED: {e}"
            failures += not status.startswith("downloaded, hash ok")
        print(f"{d['project_id']}  {d['filename']:52} {status}")
    print(f"\n{len(docs) - failures}/{len(docs)} verified")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
