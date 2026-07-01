#!/usr/bin/env python3
"""
iphone_backup.py — incremental, read-only backup of iPhone photos/videos.

Reads the iPhone over USB via libimobiledevice's AFC service (the same channel
Finder uses) and copies every photo/video from the phone's DCIM into ONE flat
target folder. Files keep the phone's capture date (mtime). Backup is:

  * incremental — a file already present (same name + size) is skipped;
  * lossless    — when two different files would share a name (the iPhone's
                  IMG_#### counter wraps, so names repeat across DCIM folders),
                  later ones get a " 1", " 2", ... suffix instead of clobbering;
  * safe        — nothing on the phone or in the target is ever deleted or
                  overwritten.

Usage:
    python3 iphone_backup.py /Volumes/MySSD/iPhoneBackup
    python3 iphone_backup.py /Volumes/MySSD/iPhoneBackup --dry-run

Requirements:
    brew install libimobiledevice
    iPhone plugged in, unlocked, and "Trust This Computer" tapped.

Why AFC and not gphoto2/PTP: modern iOS (17+) no longer enumerates its photo
objects reliably over PTP, so gphoto2 reports an empty device even when photos
are present. AFC reads DCIM directly and works across current iOS versions.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

# Parse a line of `afcclient ls -l`, e.g.:
#   -rw-r--r--    1 mobile mobile    2368333 23 Nov 2024 14:32:09 IMG_9726.HEIC
#   drwxr-xr-x    2 mobile mobile      35296 07 Mar 2026 01:05:05 109APPLE
# Groups: (type, size, date, name). Dates are reported in the phone's local
# time (verified against st_mtime), so we parse them naive and let the OS treat
# them as local when stamping mtime.
_LS_RE = re.compile(
    r"^([d-])\S*\s+\d+\s+\S+\s+\S+\s+(\d+)\s+"
    r"(\d+\s+\w+\s+\d+\s+[\d:]+)\s+(.+?)\s*$"
)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_PROMPT_RE = re.compile(r"^afc:[^>]*>\s*")


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def require_tools():
    for tool in ("idevice_id", "idevicepair", "afcclient"):
        if shutil.which(tool) is None:
            sys.exit(
                f"{tool} not found. Install the toolkit with:\n"
                "  brew install libimobiledevice"
            )


def detect_iphone():
    """Return the UDID of the first connected, paired device."""
    r = run(["idevice_id", "-l"])
    udids = [u for u in r.stdout.split() if u.strip()]
    if not udids:
        sys.exit(
            "No iPhone detected over USB.\n"
            "  - Plug in the iPhone and UNLOCK it.\n"
            "  - Tap 'Trust This Computer' on the phone, then retry."
        )
    udid = udids[0]
    v = run(["idevicepair", "-u", udid, "validate"])
    if v.returncode != 0:
        sys.exit(
            "iPhone is connected but not paired/trusted.\n"
            "  - UNLOCK the phone and tap 'Trust This Computer'.\n"
            "  - Then run:  idevicepair pair\n\n"
            f"idevicepair said:\n{(v.stdout + v.stderr).strip()}"
        )
    return udid


def afc_lines(udid, command):
    """Run one afcclient command in batch mode; return cleaned output lines.

    Flags like `ls -l` are only understood by afcclient's interactive parser,
    so commands go in over stdin. Strip the `afc:/ >` prompt and ANSI codes.
    """
    r = run(["afcclient", "-u", udid], input=f"{command}\nquit\n")
    return [_PROMPT_RE.sub("", _ANSI_RE.sub("", line)).strip()
            for line in r.stdout.splitlines()]


def parse_mtime(datestr):
    """'23 Nov 2024 14:32:09' (phone local time) -> epoch seconds, or None."""
    try:
        return datetime.strptime(datestr, "%d %b %Y %H:%M:%S").timestamp()
    except ValueError:
        return None


def list_dcim(udid):
    """Return [(folder, name, size, mtime), ...] for every file under DCIM,
    ordered by folder then name for deterministic collision handling."""
    folders = []
    for line in afc_lines(udid, "ls -l DCIM"):
        m = _LS_RE.match(line)
        if m and m.group(1) == "d":
            folders.append(m.group(4))
    if not folders:
        sys.exit(
            "No photo folders found under DCIM.\n"
            "  - Make sure the phone is UNLOCKED and trusted, then retry."
        )

    out = []
    for folder in sorted(folders):
        files = []
        for line in afc_lines(udid, f"ls -l DCIM/{folder}"):
            m = _LS_RE.match(line)
            if m and m.group(1) == "-":
                files.append((m.group(4), int(m.group(2)), parse_mtime(m.group(3))))
        for name, size, mtime in sorted(files):
            out.append((folder, name, size, mtime))
    return out


def base_stem(name):
    """('IMG_5059 2.MOV') -> ('IMG_5059', '.MOV'); strips a trailing ' N'."""
    stem, ext = os.path.splitext(name)
    return re.sub(r" \d+$", "", stem), ext


def scan_target(target):
    """Index the files at the target's TOP LEVEL only (the flat backup root).

    We deliberately do NOT descend into subfolders such as the _duplicates/
    trash: that folder is temporary and meant to be deleted, so whether a photo
    counts as "already backed up" must be decided purely from what is actually
    kept at the root. (dedup.py renumbers survivors so a stem's suffixes stay
    contiguous, which keeps this check exact.)

    Returns (sizes_by_stem, top_names, count):
      sizes_by_stem : {(base_stem, ext): {size, ...}}  over root files
      top_names     : {filename}  at the root (for placing new files)
      count         : total root files seen
    """
    sizes_by_stem = {}
    top_names = set()
    count = 0
    try:
        entries = list(os.scandir(target))
    except OSError:
        return sizes_by_stem, top_names, count
    for e in entries:
        if e.name.startswith("._"):
            continue
        try:
            if not e.is_file(follow_symlinks=False):
                continue
            size = e.stat(follow_symlinks=False).st_size
        except OSError:
            continue
        sizes_by_stem.setdefault(base_stem(e.name), set()).add(size)
        top_names.add(e.name)
        count += 1
    return sizes_by_stem, top_names, count


def resolve_name(name, size, sizes_by_stem, top_names, used):
    """Decide whether a device photo is already backed up, and if not, pick a
    flat target filename for it.

    A photo is considered already present if ANY file sharing its base name
    (bare name or 'name 1', 'name 2', ...) at the target ROOT has the same size.
    So copying 'IMG_123 1.MOV' checks IMG_123.MOV, 'IMG_123 1.MOV',
    'IMG_123 2.MOV', ... at the root and skips if one matches its size.
    Otherwise it's placed at the next free top-level suffix. Returns
    (target_name_or_None, already_present).
    """
    key = base_stem(name)
    if size in sizes_by_stem.get(key, ()):
        return None, True

    stem, ext = key
    i = 0
    while True:
        cand = name if i == 0 else f"{stem} {i}{ext}"
        i += 1
        if cand in used or cand in top_names:
            continue
        used.add(cand)
        sizes_by_stem.setdefault(key, set()).add(size)  # so a later run/dup skips it
        return cand, False


class _SimpleBar:
    """Minimal byte progress bar used when tqdm isn't installed."""

    def __init__(self, total):
        self.total = total or 1
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
        gb = 1024 ** 3
        sys.stdout.write(
            f"\r  {self.n / self.total * 100:5.1f}%  "
            f"{self.n / gb:6.2f}/{self.total / gb:.2f} GB  "
            f"{rate / 1e6:5.1f} MB/s  ETA {int(eta // 60):d}m{int(eta % 60):02d}s   "
        )
        sys.stdout.flush()

    def write(self, msg):
        sys.stdout.write("\r" + " " * 78 + "\r")
        print(msg)

    def close(self):
        sys.stdout.write("\n")


def make_bar(total):
    try:
        from tqdm import tqdm
        return tqdm(total=total, unit="B", unit_scale=True, unit_divisor=1024,
                    desc="  Copying", smoothing=0.1)
    except ImportError:
        return _SimpleBar(total)


def download(udid, target, jobs, bar):
    """Batch-download jobs [(folder, devname, target_name, size, mtime), ...]
    in one AFC session, then atomically move each into place and stamp mtime.

    afcclient writes staging/0, staging/1, ... in order, so we poll the staging
    dir while it runs and advance `bar` (by bytes) as each file completes.

    Staging uses space-free numeric names (afcclient's stdin parser splits on
    spaces, and target names may contain spaces); Python does the final rename.
    """
    staging = ".incoming"
    staging_abs = os.path.join(target, staging)
    # Start clean: a leftover staging folder from an interrupted run could hold
    # higher-numbered files than this chunk and throw off progress accounting.
    shutil.rmtree(staging_abs, ignore_errors=True)
    os.makedirs(staging_abs, exist_ok=True)

    script = "".join(
        f"get -f DCIM/{folder}/{devname} {staging}/{idx}\n"
        for idx, (folder, devname, _, _, _) in enumerate(jobs)
    ) + "quit\n"
    proc = subprocess.Popen(
        ["afcclient", "-u", udid], cwd=target, text=True,
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    proc.stdin.write(script)
    proc.stdin.close()

    # Live progress: every index below the highest-present one is fully written.
    counted = 0
    while proc.poll() is None:
        try:
            present = [int(n) for n in os.listdir(staging_abs) if n.isdigit()]
        except FileNotFoundError:
            present = []
        hi = min(max(present), len(jobs) - 1) if present else -1
        while counted < hi:
            bar.update(jobs[counted][3])
            counted += 1
        time.sleep(0.3)
    proc.wait()

    copied = failed = 0
    for idx, (_, devname, target_name, size, mtime) in enumerate(jobs):
        src = os.path.join(staging_abs, str(idx))
        dst = os.path.join(target, target_name)
        if os.path.exists(src) and os.path.getsize(src) == size:
            try:
                os.replace(src, dst)
                if mtime is not None:
                    os.utime(dst, (mtime, mtime))
                copied += 1
            except OSError as e:
                bar.write(f"    failed to place {target_name}: {e}")
                failed += 1
        else:
            bar.write(f"    incomplete download: {devname}")
            failed += 1

    # Count bytes for the tail of the chunk not yet advanced during polling.
    while counted < len(jobs):
        bar.update(jobs[counted][3])
        counted += 1

    shutil.rmtree(staging_abs, ignore_errors=True)
    return copied, failed


def main():
    ap = argparse.ArgumentParser(description="Incremental iPhone photo/video backup.")
    ap.add_argument("target", help="Destination folder (e.g. /Volumes/MySSD/iPhoneBackup)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be copied without downloading.")
    args = ap.parse_args()

    require_tools()
    target = os.path.abspath(args.target)
    if not args.dry_run:
        os.makedirs(target, exist_ok=True)

    udid = detect_iphone()
    print("Scanning iPhone...")
    device = list_dcim(udid)
    print(f"Found {len(device)} files on device.")

    sizes_by_stem, top_names, tcount = scan_target(target)
    print(f"Target already holds {tcount} files.\n")

    used = set()
    jobs = []          # files to download this run
    skipped = 0
    for folder, devname, size, mtime in device:
        target_name, present = resolve_name(devname, size, sizes_by_stem, top_names, used)
        if present:
            skipped += 1
        else:
            jobs.append((folder, devname, target_name, size, mtime))

    total_bytes = sum(size for _, _, _, size, _ in jobs)
    print(f"{len(jobs)} new ({total_bytes / 1024**3:.1f} GB), "
          f"{skipped} already present.\n")

    copied = failed = 0
    if args.dry_run:
        for folder, devname, target_name, size, _ in jobs[:50]:
            tag = "" if target_name == devname else f"  (as {target_name})"
            print(f"    would copy {folder}/{devname} ({size:,} bytes){tag}")
        if len(jobs) > 50:
            print(f"    ... and {len(jobs) - 50} more")
        copied = len(jobs)
    else:
        # Download in chunks so staging stays bounded; one bar spans the run.
        CHUNK = 300
        bar = make_bar(total_bytes)
        for i in range(0, len(jobs), CHUNK):
            c, f = download(udid, target, jobs[i:i + CHUNK], bar)
            copied += c
            failed += f
        bar.close()

    print("\n" + "=" * 40)
    verb = "would copy" if args.dry_run else "copied"
    print(f"Done. {verb}: {copied}   skipped (already present): {skipped}", end="")
    print(f"   failed: {failed}" if failed else "")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted. Re-run to resume — already-copied files are skipped.")
