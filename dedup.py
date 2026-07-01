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
removed. The keeper prefers a file at the target's top level, then one named
in the iPhone's own 'IMG_<digits>' convention (so legacy imports with
different naming don't get kept over it — see keeper()), then "cleanest
name": no trailing ' 1'/' 2' suffix if possible, then the shortest name, then
alphabetical.

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
from concurrent.futures import ThreadPoolExecutor

_SUFFIX_RE = re.compile(r" \d+$")            # trailing " 1", " 12", ...
_DEVICE_STEM_RE = re.compile(r"^IMG_\d+$")    # iPhone's own naming, pre-suffix
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


def suffix_index(name):
    """'IMG_123 4.MOV' -> 4; 'IMG_123.MOV' -> 0."""
    stem = os.path.splitext(name)[0]
    m = _SUFFIX_RE.search(stem)
    return int(m.group().strip()) if m else 0


def top_level_files(folder):
    """Names of regular files directly in `folder` (skip hidden/._ and dirs)."""
    out = []
    try:
        for e in os.scandir(folder):
            if e.name.startswith(".") or e.name.startswith("._"):
                continue
            try:
                if e.is_file(follow_symlinks=False):
                    out.append(e.name)
            except OSError:
                pass
    except OSError:
        pass
    return out


def plan_renumber(names):
    """Given top-level filenames, return [(old, new)] renames that make each
    base stem's ' N' suffixes contiguous: stem, 'stem 1', 'stem 2', ... with no
    gaps. Pure function (no disk access). Order within a stem is preserved by
    current suffix number so the bare/oldest name stays first."""
    groups = defaultdict(list)
    for n in names:
        stem, ext = os.path.splitext(n)
        base = _SUFFIX_RE.sub("", stem)
        groups[(base, ext)].append(n)

    pairs = []
    for (base, ext), ns in groups.items():
        ns.sort(key=lambda n: (suffix_index(n), n))
        desired = [f"{base}{ext}" if i == 0 else f"{base} {i}{ext}"
                   for i in range(len(ns))]
        pairs += [(o, d) for o, d in zip(ns, desired) if o != d]
    return pairs


def renumber(folder, pairs):
    """Apply [(old, new)] renames at the top level of `folder`. Uses a two-phase
    move through temp names so files can shift within a group without clobbering
    each other. Returns (done_pairs, errors)."""
    if not pairs:
        return [], 0
    errors = 0
    staged = []                                   # (temp_name, dest_name, old)
    for o, d in pairs:
        t = f".renum_tmp_{o}"
        try:
            os.replace(os.path.join(folder, o), os.path.join(folder, t))
            staged.append((t, d, o))
        except OSError as e:
            print(f"  renumber error on {o}: {e}")
            errors += 1
    done = []
    for t, d, o in staged:
        try:
            os.replace(os.path.join(folder, t), os.path.join(folder, d))
            done.append((o, d))
        except OSError as e:
            print(f"  renumber error placing {d}: {e}")
            errors += 1
    return done, errors


def keeper(paths, folder=None):
    """Pick the canonical file to keep from a group of identical files.

    Prefers, in order: a file at the target's top level over one in a
    subfolder; a name matching the iPhone's own 'IMG_<digits>' convention over
    any other naming (legacy imports/tools often rename files, e.g. 'IMG_O1234'
    or something unrelated entirely — keeping those instead silently breaks
    iphone_backup.py's incremental check, which only recognizes 'IMG_<digits>'
    stems, causing it to re-copy files that are already backed up); then no
    trailing ' 1'/' 2' suffix if possible; then the shortest name; then
    alphabetical.
    """
    def rank(p):
        name = os.path.basename(p)
        stem = os.path.splitext(name)[0]
        base = _SUFFIX_RE.sub("", stem)
        top_level = folder is not None and os.path.dirname(os.path.abspath(p)) == folder
        return (
            0 if top_level else 1,
            0 if _DEVICE_STEM_RE.match(base) else 1,
            1 if _SUFFIX_RE.search(stem) else 0,
            len(name),
            p,
        )
    return min(paths, key=rank)


def _emit(members, size, groups, folder):
    if len(members) > 1:
        k = keeper(members, folder)
        groups.append((k, [p for p in members if p != k], size))


def find_duplicates(folder, full=False, workers=8):
    """Return list of (keep_path, [dup_paths]) for identical-content groups.

    Files are first bucketed by size (free, from stat), so only files that share
    a size are ever read. Small files (and --full) are hashed whole; large files
    are prefiltered by their HEAD and only head+size matches pay for the extra
    middle/tail reads. Reads run on a thread pool because the per-file cost on
    external USB disks is mostly I/O *wait*, which overlaps well across threads.
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
          f"({human(total_bytes)} max to read, {'full hash' if full else 'sampled'}, "
          f"{workers} threads).")

    bar = make_bar(total_bytes, "  Hashing")
    groups = []

    def run(fn, items):
        """Stream fn over items on the pool (or serially if workers<=1),
        yielding results in order and shutting the pool down when done."""
        if workers <= 1:
            yield from map(fn, items)
            return
        ex = ThreadPoolExecutor(max_workers=workers)
        try:
            yield from ex.map(fn, items)
        finally:
            ex.shutdown(wait=False)

    # Phase 1: hash each candidate's head (large) or whole file (small/--full).
    def phase1(item):
        size, p = item
        try:
            if full or size <= _SAMPLE * 3:
                digest, read = full_hash(p)
                return (size, p, ("F", digest), read, None)
            digest, read = head_hash(p)
            return (size, p, ("H", digest), read, None)
        except OSError as e:
            return (size, p, None, to_read(size), e)

    items = [(size, p) for size, paths in candidates.items() for p in paths]
    first = defaultdict(list)
    for size, p, key, read, err in run(phase1, items):
        bar.update(read)
        if err:
            bar.write(f"  skipped {p}: {err}")
            continue
        first[(size, key)].append(p)

    # Small/--full buckets are already final; large 'head' buckets need confirming.
    confirm = []                                   # (size, head_digest, path)
    for (size, (kind, digest)), members in first.items():
        if kind == "F":
            _emit(members, size, groups, folder)
        elif len(members) < 2:
            bar.update(_SAMPLE * 2 * len(members))  # mid+tail we won't read
        else:
            confirm += [(size, digest, p) for p in members]

    # Phase 2: confirm same-head large files by their middle+tail.
    def phase2(item):
        size, hdig, p = item
        try:
            digest, read = midtail_hash(p, size)
            return (size, hdig, p, digest, read, None)
        except OSError as e:
            return (size, hdig, p, None, _SAMPLE * 2, e)

    second = defaultdict(list)                      # (size, head_dig, midtail_dig)
    for size, hdig, p, digest, read, err in run(phase2, confirm):
        bar.update(read)
        if err:
            bar.write(f"  skipped {p}: {err}")
            continue
        second[(size, hdig, digest)].append(p)
    for (size, _, _), members in second.items():
        _emit(members, size, groups, folder)
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
    ap.add_argument("--workers", type=int, default=8,
                    help="Parallel read threads (default 8; helps on high-latency "
                         "external/USB disks; use 1 to disable).")
    ap.add_argument("--no-renumber", action="store_true",
                    help="Don't make ' N' suffixes contiguous after removing "
                         "duplicates (default: renumber survivors so a stem's "
                         "files are name, 'name 1', 'name 2', ... with no gaps).")
    args = ap.parse_args()

    folder = os.path.abspath(args.folder)
    if not os.path.isdir(folder):
        sys.exit(f"Not a folder: {folder}")
    trash = os.path.abspath(args.trash) if args.trash else os.path.join(folder, "_duplicates")

    groups = find_duplicates(folder, full=args.full, workers=args.workers)
    dup_count = sum(len(d) for _, d, _ in groups)
    freed = sum(size * len(d) for _, d, size in groups)
    print(f"\nFound {len(groups)} duplicate group(s): "
          f"{dup_count} redundant file(s), {human(freed)} recoverable.\n")

    # Top-level dups that would disappear — used to preview post-dedup renumber.
    top_dups = {os.path.basename(d) for _, dups, _ in groups
                for d in dups if os.path.dirname(os.path.abspath(d)) == folder}

    for keep, dups, _ in groups[:15]:
        print(f"  keep {os.path.basename(keep)}")
        for d in dups:
            print(f"    dup  {os.path.relpath(d, folder)}")
    if len(groups) > 15:
        print(f"  ... and {len(groups) - 15} more group(s)")

    if not args.apply:
        if not args.no_renumber:
            survivors = [n for n in top_level_files(folder) if n not in top_dups]
            pairs = plan_renumber(survivors)
            if pairs:
                print(f"\nWould also renumber {len(pairs)} top-level file(s) so "
                      "each name's suffixes are contiguous, e.g.:")
                for o, d in pairs[:10]:
                    print(f"    {o}  ->  {d}")
                if len(pairs) > 10:
                    print(f"    ... and {len(pairs) - 10} more")
        if groups:
            print("\n(dry run) Re-run with --apply to move these to trash, "
                  "or --apply --delete to remove them.")
        elif args.no_renumber:
            print("Nothing to do.")
        else:
            print("\n(dry run) Re-run with --apply to renumber.")
        return

    removed = errors = 0
    if groups:
        action = "Deleting" if args.delete else f"Moving to {trash}"
        print(f"\n{action} {dup_count} duplicate(s)...")
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
                            os.replace(d, dest)      # fast path (same filesystem)
                        except OSError:
                            shutil.move(d, dest)      # fallback (cross-device trash)
                    removed += 1
                except OSError as e:
                    print(f"  error on {d}: {e}")
                    errors += 1
        print(f"Done. removed: {removed}   freed: {human(freed)}", end="")
        print(f"   errors: {errors}" if errors else "")
        if not args.delete:
            print(f"Duplicates are in {trash} — delete that folder when you're happy.")

    # Close ' N' suffix gaps left behind so a stem reads name, 'name 1', ...
    if not args.no_renumber:
        done, rerr = renumber(folder, plan_renumber(top_level_files(folder)))
        if done:
            print(f"\nRenumbered {len(done)} top-level file(s) so suffixes are "
                  "contiguous.")
        if rerr:
            print(f"  ({rerr} renumber error(s).)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
