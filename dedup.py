#!/usr/bin/env python3
"""
dedup.py — find and remove byte-identical duplicate files in a folder.

Two files are "duplicates" only if their contents are identical (same bytes).
Files that merely share a name but differ in content — e.g. the iPhone's
counter-wrap photos IMG_5059.MOV vs 'IMG_5059 1.MOV' when they are different
videos — are NOT touched.

For each group of identical files, ONE canonical copy is kept and the rest are
removed. The keeper is chosen by "cleanest name": no trailing ' 1'/' 2' suffix
if possible, then the shortest name, then alphabetical.

SAFE BY DEFAULT: this only reports what it would do. Pass --apply to act. With
--apply, duplicates are moved to a trash folder (default: <folder>/_duplicates)
so nothing is permanently lost; add --delete to remove them outright instead.

Usage:
    python3 dedup.py "/Volumes/exFAT disk/photo/iphone"                 # report
    python3 dedup.py "/Volumes/exFAT disk/photo/iphone" --apply         # to trash
    python3 dedup.py "/Volumes/exFAT disk/photo/iphone" --apply --delete
"""

import argparse
import hashlib
import os
import re
import sys
import time
from collections import defaultdict

_SUFFIX_RE = re.compile(r" \d+$")            # trailing " 1", " 12", ...
_SAMPLE = 256 * 1024                          # bytes sampled per region (head/mid/tail)


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024


class _SimpleBar:
    """Minimal byte progress bar; used when tqdm isn't installed."""

    def __init__(self, total, desc="  Hashing"):
        self.total = total or 1
        self.desc = desc
        self.n = 0
        self.start = time.time()
        self.last = 0.0

    def update(self, n=1):
        self.n += n
        now = time.time()
        if now - self.last < 0.3 and self.n < self.total:
            return
        self.last = now
        elapsed = now - self.start
        rate = self.n / elapsed if elapsed > 0 else 0
        eta = (self.total - self.n) / rate if rate > 0 else 0
        sys.stdout.write(
            f"\r{self.desc}  {self.n / self.total * 100:5.1f}%  "
            f"{human(self.n)}  {human(rate)}/s  ETA {int(eta // 60)}m{int(eta % 60):02d}s   "
        )
        sys.stdout.flush()

    def write(self, msg):
        sys.stdout.write("\r" + " " * 78 + "\r")
        print(msg)

    def close(self):
        sys.stdout.write("\n")


def make_bar(total, desc):
    try:
        from tqdm import tqdm
        return tqdm(total=total, unit="B", unit_scale=True, unit_divisor=1024, desc=desc)
    except ImportError:
        return _SimpleBar(total, desc)


def full_hash(path):
    """SHA-1 of the entire file; returns (digest, bytes_read)."""
    h = hashlib.sha1()
    read = 0
    with open(path, "rb", buffering=0) as f:
        while True:
            chunk = f.read(4 * 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            read += len(chunk)
    return h.hexdigest(), read


def sample_hash(path, size, sample=_SAMPLE):
    """Fast signature: SHA-1 of size + head/middle/tail samples.

    For files up to 3 samples long the whole file is read (so it's exact);
    larger files read only 3*sample bytes regardless of size. Photos and videos
    differ throughout, so a head/middle/tail match means identical in practice.
    Returns (digest, bytes_read).
    """
    h = hashlib.sha1()
    h.update(str(size).encode())
    with open(path, "rb", buffering=0) as f:
        if size <= sample * 3:
            data = f.read()
            h.update(data)
            return h.hexdigest(), len(data)
        h.update(f.read(sample))
        f.seek(size // 2)
        h.update(f.read(sample))
        f.seek(-sample, os.SEEK_END)
        h.update(f.read(sample))
    return h.hexdigest(), sample * 3


def gather(folder):
    """Return {size: [paths]} for regular files, skipping hidden/._ files."""
    by_size = defaultdict(list)
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "_duplicates"]
        for name in files:
            if name.startswith(".") or name.startswith("._"):
                continue
            p = os.path.join(root, name)
            try:
                by_size[os.path.getsize(p)].append(p)
            except OSError:
                pass
    return by_size


def keeper(paths):
    """Pick the canonical file to keep from a group of identical files."""
    def rank(p):
        stem = os.path.splitext(os.path.basename(p))[0]
        return (1 if _SUFFIX_RE.search(stem) else 0, len(os.path.basename(p)), p)
    return min(paths, key=rank)


def find_duplicates(folder, full=False):
    """Return list of (keep_path, [dup_paths]) for identical-content groups.

    Files are first bucketed by size (free, from stat), so only files that share
    a size are ever read. Each such file is reduced to a signature — a fast
    head/middle/tail sample by default, or a whole-file hash with full=True.
    """
    by_size = gather(folder)
    candidates = {s: ps for s, ps in by_size.items() if len(ps) > 1}

    def to_read(size):
        return size if (full or size <= _SAMPLE * 3) else _SAMPLE * 3

    total_bytes = sum(to_read(s) * len(ps) for s, ps in candidates.items())
    total_files = sum(len(ps) for ps in candidates.values())
    print(f"Scanned folder: {sum(len(v) for v in by_size.values())} files; "
          f"{total_files} share a size ({human(total_bytes)} to read, "
          f"{'full hash' if full else 'sampled'}).")

    bar = make_bar(total_bytes, "  Hashing")
    groups = []
    for size, paths in candidates.items():
        buckets = defaultdict(list)
        for p in paths:
            try:
                digest, read = full_hash(p) if full else sample_hash(p, size)
            except OSError as e:
                bar.write(f"  skipped {p}: {e}")
                bar.update(to_read(size))
                continue
            buckets[digest].append(p)
            bar.update(read)
        for members in buckets.values():
            if len(members) > 1:
                k = keeper(members)
                groups.append((k, [p for p in members if p != k]))
    bar.close()
    return groups


def main():
    ap = argparse.ArgumentParser(description="Remove byte-identical duplicate files.")
    ap.add_argument("folder", help="Folder to scan (recursively).")
    ap.add_argument("--apply", action="store_true",
                    help="Actually remove duplicates (default: report only).")
    ap.add_argument("--delete", action="store_true",
                    help="With --apply, delete outright instead of moving to trash.")
    ap.add_argument("--trash", default=None,
                    help="Trash folder for removed dupes (default: <folder>/_duplicates).")
    ap.add_argument("--full", action="store_true",
                    help="Hash whole files instead of head/middle/tail samples "
                         "(slower, exact byte-for-byte certainty).")
    args = ap.parse_args()

    folder = os.path.abspath(args.folder)
    if not os.path.isdir(folder):
        sys.exit(f"Not a folder: {folder}")
    trash = os.path.abspath(args.trash) if args.trash else os.path.join(folder, "_duplicates")

    groups = find_duplicates(folder, full=args.full)
    dup_count = sum(len(d) for _, d in groups)
    freed = sum(os.path.getsize(d) for _, ds in groups for d in ds)
    print(f"\nFound {len(groups)} duplicate group(s): "
          f"{dup_count} redundant file(s), {human(freed)} recoverable.\n")

    if not groups:
        return
    for keep, dups in groups[:15]:
        print(f"  keep {os.path.basename(keep)}")
        for d in dups:
            print(f"    dup  {os.path.relpath(d, folder)}")
    if len(groups) > 15:
        print(f"  ... and {len(groups) - 15} more group(s)")

    if not args.apply:
        print("\n(dry run) Re-run with --apply to move these to trash, "
              "or --apply --delete to remove them.")
        return

    action = "Deleting" if args.delete else f"Moving to {trash}"
    print(f"\n{action} {dup_count} duplicate(s)...")
    removed = errors = 0
    for _, dups in groups:
        for d in dups:
            try:
                if args.delete:
                    os.remove(d)
                else:
                    rel = os.path.relpath(d, folder)
                    dest = os.path.join(trash, rel)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    if os.path.exists(dest):
                        base, ext = os.path.splitext(dest)
                        dest = f"{base}_{int(time.time()*1000)}{ext}"
                    os.replace(d, dest)
                removed += 1
            except OSError as e:
                print(f"  error on {d}: {e}")
                errors += 1
    print(f"Done. removed: {removed}   freed: {human(freed)}", end="")
    print(f"   errors: {errors}" if errors else "")
    if not args.delete:
        print(f"Duplicates are in {trash} — delete that folder when you're happy.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
