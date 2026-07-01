#!/usr/bin/env python3
"""
dedup.py — find and remove duplicate files in a folder.

Two files are treated as duplicates when they have the same size AND matching
content. By default matching is judged from head/middle/tail content samples
(fast, and reliable for photos/videos, which differ throughout); pass --full to
require an exact byte-for-byte whole-file match. Files that merely share a name
but differ in content — e.g. the iPhone's counter-wrap photos IMG_5059.MOV vs
'IMG_5059 1.MOV' when they are different videos — are NOT touched.

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
import shutil
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


def head_hash(path, sample=_SAMPLE):
    """SHA-1 of the first `sample` bytes (one sequential read, no seek)."""
    h = hashlib.sha1()
    with open(path, "rb", buffering=0) as f:
        data = f.read(sample)
    h.update(data)
    return h.hexdigest(), len(data)


def midtail_hash(path, size, sample=_SAMPLE):
    """SHA-1 of the middle + tail samples (used to confirm head matches)."""
    h = hashlib.sha1()
    with open(path, "rb", buffering=0) as f:
        f.seek(size // 2)
        h.update(f.read(sample))
        f.seek(-sample, os.SEEK_END)
        h.update(f.read(sample))
    return h.hexdigest(), sample * 2


def gather(folder):
    """Return {size: [paths]} for regular files, skipping hidden/._ files,
    symlinks, and the _duplicates trash folder. Uses scandir so each entry is
    stat'd once (not twice), which matters on slow external disks."""
    by_size = defaultdict(list)
    stack = [folder]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.name.startswith(".") or e.name.startswith("._"):
                        continue
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if e.name != "_duplicates":
                                stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            by_size[e.stat(follow_symlinks=False).st_size].append(e.path)
                    except OSError:
                        pass
        except OSError:
            pass
    return by_size


def keeper(paths):
    """Pick the canonical file to keep from a group of identical files."""
    def rank(p):
        stem = os.path.splitext(os.path.basename(p))[0]
        return (1 if _SUFFIX_RE.search(stem) else 0, len(os.path.basename(p)), p)
    return min(paths, key=rank)


def _emit(members, size, groups):
    if len(members) > 1:
        k = keeper(members)
        groups.append((k, [p for p in members if p != k], size))


def find_duplicates(folder, full=False):
    """Return list of (keep_path, [dup_paths]) for identical-content groups.

    Files are first bucketed by size (free, from stat), so only files that share
    a size are ever read. Then, to minimise seeks on spinning disks, large files
    are prefiltered by a single sequential read of their HEAD; only files whose
    head (and size) already match pay for the extra middle/tail seeks. Small
    files (and --full) are hashed whole in one read.
    """
    by_size = gather(folder)
    candidates = {s: ps for s, ps in by_size.items() if len(ps) > 1}

    def to_read(size):
        return size if (full or size <= _SAMPLE * 3) else _SAMPLE * 3

    total_bytes = sum(to_read(s) * len(ps) for s, ps in candidates.items())
    total_files = sum(len(ps) for ps in candidates.values())
    all_files = sum(len(v) for v in by_size.values())
    unique = all_files - total_files
    print(f"Scanned {all_files} files: {unique} have a unique size (skipped "
          f"without reading); {total_files} share a size and get checked "
          f"({human(total_bytes)} max to read, {'full hash' if full else 'sampled'}).")

    bar = make_bar(total_bytes, "  Hashing")
    groups = []
    for size, paths in candidates.items():
        if full or size <= _SAMPLE * 3:
            # One sequential read of the whole (small) file — exact.
            buckets = defaultdict(list)
            for p in paths:
                try:
                    digest, read = full_hash(p)
                except OSError as e:
                    bar.write(f"  skipped {p}: {e}")
                    bar.update(to_read(size))
                    continue
                buckets[digest].append(p)
                bar.update(read)
            for members in buckets.values():
                _emit(members, size, groups)
            continue

        # Large files: prefilter by head (1 seek), confirm with mid+tail.
        head = defaultdict(list)
        for p in paths:
            try:
                digest, read = head_hash(p)
            except OSError as e:
                bar.write(f"  skipped {p}: {e}")
                bar.update(to_read(size))
                continue
            head[digest].append(p)
            bar.update(read)
        for hpaths in head.values():
            if len(hpaths) < 2:
                bar.update(_SAMPLE * 2 * len(hpaths))   # skipped mid+tail reads
                continue
            confirmed = defaultdict(list)
            for p in hpaths:
                try:
                    digest, read = midtail_hash(p, size)
                except OSError as e:
                    bar.write(f"  skipped {p}: {e}")
                    bar.update(_SAMPLE * 2)
                    continue
                confirmed[digest].append(p)
                bar.update(read)
            for members in confirmed.values():
                _emit(members, size, groups)
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
    dup_count = sum(len(d) for _, d, _ in groups)
    freed = sum(size * len(d) for _, d, size in groups)
    print(f"\nFound {len(groups)} duplicate group(s): "
          f"{dup_count} redundant file(s), {human(freed)} recoverable.\n")

    if not groups:
        return
    for keep, dups, _ in groups[:15]:
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
    for _, dups, _ in groups:
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
                    try:
                        os.replace(d, dest)          # fast path (same filesystem)
                    except OSError:
                        shutil.move(d, dest)          # fallback (cross-device trash)
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
