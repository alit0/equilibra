"""Package dist/ into a deploy archive for Hostinger.

Run after scripts/build.ps1 and scripts/verify.ps1. The contents of dist/ land
at the archive root, which is what hosting_deployStaticWebsite expects.

Two build-internal files are excluded:
    build-manifest.json   declares itself internal and carries the absolute
                          source path of the build machine
    checksums.sha256      local integrity artefact, meaningless on the server

Entry names always use forward slashes because this writes the archive through
zipfile directly; Compress-Archive on PowerShell 5.1 does not guarantee that.

Exits non-zero if the archive would be missing a file the site needs.
"""

import argparse
import datetime
import os
import sys
import zipfile

EXCLUDE = {"build-manifest.json", "checksums.sha256"}

# Absent from the archive, the site breaks in a way that is not obvious from a
# 200 response: no index means the old cached page keeps serving, and no
# .htaccess silently drops the canonical redirects.
REQUIRED = {"index.html", ".htaccess"}

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fail(message):
    print(f"[pack] FATAL: {message}", file=sys.stderr)
    sys.exit(1)


def collect(dist_root):
    """Return (absolute_path, archive_name) pairs, sorted by archive name."""
    entries = []
    for root, _dirs, files in os.walk(dist_root):
        for name in files:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, dist_root).replace(os.sep, "/")
            if rel in EXCLUDE:
                continue
            entries.append((full, rel))
    entries.sort(key=lambda entry: entry[1])
    return entries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dist-root",
        default=os.path.join(REPO_ROOT, "dist"),
        help="directory to package (default: dist/ beside this repo)",
    )
    parser.add_argument(
        "--out-dir",
        default=os.path.join(REPO_ROOT, "deploy"),
        help="where to write the archive (default: deploy/, gitignored)",
    )
    args = parser.parse_args()

    dist_root = os.path.abspath(args.dist_root)
    if not os.path.isdir(dist_root):
        fail(f"dist root not found: {dist_root} (run scripts/build.ps1 first)")

    entries = collect(dist_root)
    if not entries:
        fail(f"nothing to package: {dist_root} is empty")

    names = {rel for _full, rel in entries}
    missing = REQUIRED - names
    if missing:
        fail(f"required files absent from dist/: {', '.join(sorted(missing))}")

    os.makedirs(args.out_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(os.path.abspath(args.out_dir), f"dist_{stamp}.zip")

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for full, rel in entries:
            archive.write(full, rel)

    # Read the archive back rather than trusting the write loop.
    with zipfile.ZipFile(out_path) as archive:
        written = archive.namelist()
    still_missing = REQUIRED - set(written)
    if still_missing:
        fail(f"archive is missing: {', '.join(sorted(still_missing))}")
    leaked = EXCLUDE & set(written)
    if leaked:
        fail(f"archive contains excluded files: {', '.join(sorted(leaked))}")

    print(f"[pack] {out_path}")
    print(f"[pack] {len(written)} entries, {os.path.getsize(out_path)} bytes")
    for name in written:
        print(f"[pack]    {name}")


if __name__ == "__main__":
    main()
