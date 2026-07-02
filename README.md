# iPhone incremental backup

Read-only tools to back up every photo and video from an iPhone to a folder on
disk (e.g. an external SSD).

- **`iphone_backup.py`** — the backup. Copies the iPhone's camera roll into one
  flat folder, preserving each file's original capture date. It is
  **incremental and duplicate-aware**: re-run it any time and it copies only
  what's new, skipping anything already there. It never overwrites, deletes, or
  creates a second copy of a file it already backed up.
- **`dedup.py`** — an **optional** cleanup tool. Only useful if your target
  folder *already* contains duplicate files from some earlier import or tool.
  The backup script never adds duplicates, so **if you're backing up to a fresh
  folder you don't need this at all.**

---

## Requirements

```sh
brew install libimobiledevice        # provides idevice_id, idevicepair, afcclient
pip3 install tqdm                     # optional, nicer progress bar
```

macOS, Python 3.8+, iPhone connected over USB.

> Modern iOS (17+, incl. iOS 26) doesn't expose photos over PTP, so `gphoto2`
> sees an empty device. These tools use **AFC** via `libimobiledevice` — the
> same channel Finder uses. Nothing is mounted; no macFUSE needed.

---

## Backup

```sh
python3 iphone_backup.py "/Volumes/YourSSD/iPhoneBackup"          # copy
python3 iphone_backup.py "/Volumes/YourSSD/iPhoneBackup" --dry-run # preview only
```

1. Plug in the iPhone and **unlock** it (tap *Trust This Computer* if asked).
2. Run the command. Keep the phone unlocked until it finishes.

```
Found 24812 files on device.
Target already holds 34850 files.

10295 new (39.0 GB), 14517 already present.
  Copying  63.2%  24.66/39.02 GB  31.4 MB/s  ETA 7m38s
```

What you get:

- **Flat folder** — all photos/videos directly in the target, no subfolders.
- **Original dates** — each file's modified-time is set to when the photo was
  taken.
- **No duplicates added** — a photo is treated as already backed up if *any*
  file sharing its base name (`IMG_1234`, `IMG_1234 1`, `IMG_1234 2`, …) at the
  target **root** has the same size. (It looks only at the root, not the
  temporary `_duplicates/` trash, since that folder is meant to be deleted.)
  When the iPhone's `IMG_####` counter wraps and two *different* photos share a
  name, they have different sizes and each is kept (the later one gets a ` 1`/
  ` 2` suffix). Run `dedup.py` after a backup so it can renumber survivors and
  keep these suffixes contiguous — that keeps this check exact.
- **Safe & resumable** — never deletes or overwrites; if interrupted, just run
  it again to continue.

Notes: Live Photos come as paired `.HEIC` + `.MOV`. Tiny `.AAE` files are edit
sidecars (kept so edits survive). If Settings → Photos shows *Optimize iPhone
Storage*, some originals live only in iCloud and can't be copied — choose
*Download and Keep Originals* to get everything.

---

## Optional cleanup (`dedup.py`)

Use this **only** if your folder already had duplicates before you started (from
a previous export or another tool). It finds **byte-identical** files, keeps one
clean-named copy, and sets the rest aside. It never touches files that merely
share a name but differ in content.

```sh
python3 dedup.py "/Volumes/YourSSD/iPhoneBackup"                  # report only (safe)
python3 dedup.py "/Volumes/YourSSD/iPhoneBackup" --apply          # move dupes to _duplicates/
python3 dedup.py "/Volumes/YourSSD/iPhoneBackup" --apply --delete # delete them outright
```

Safe by default (only reports). `--apply` moves duplicates into a `_duplicates/`
trash folder you can review and delete when happy. After removing duplicates it
also **renumbers** the survivors so each name's ` N` suffixes are contiguous
(`IMG_123`, `IMG_123 1`, `IMG_123 2`, … — no gaps); pass `--no-renumber` to skip
that. Keeping suffixes contiguous is what lets the backup's root-only check stay
exact, so you can safely delete `_duplicates/` afterwards.

**Tombstones.** The phone itself often stores identical content under two
different names — iOS keeps an `IMG_O####.AAE` "Original" sidecar next to
`IMG_####.AAE` for edited photos, AirDrop saves get random names like
`GRCD5290.JPG`, and camera imports keep their `DSC_####.JPG` names. The backup
copies every name; dedup removes the redundant copy — and without a record of
that, the next backup would see the removed name as missing and re-copy it,
forever. So `--apply` records every file it removes from the target root in
`<target>/.dedup_tombstones` (base name, extension, size), and
`iphone_backup.py` treats tombstoned entries as already backed up. Deleting the
manifest is safe but causes a one-time re-copy/re-dedup cycle. If you have a
`_duplicates/` folder from runs that predate tombstones, backfill it once with
`--seed-tombstones`.

**Speed.** It only reads files that share a size, and for each it hashes just a
head/middle/tail **sample** rather than the whole file — so a 125 MB video is
identified from ~768 KB, not 125 MB. This is reliable for photos/videos (they
differ throughout). Add `--full` to hash entire files for exact byte-for-byte
certainty (much slower on large libraries).

Reads run in parallel (`--workers`, default 8), which matters a lot on external
USB disks where each file access is mostly I/O *wait* — on a spinning USB drive
this was ~5× faster than single-threaded. Use `--workers 1` to disable. Tip: run
dedup **after** the backup finishes, not alongside it — sharing one USB disk
between reads and writes slows both to a crawl.

---

## Troubleshooting

- **No iPhone detected / not trusted** — unlock the phone, tap *Trust This
  Computer*; check with `idevice_id -l`, and if needed run `idevicepair pair`.
- **No photos found under DCIM** — the phone is locked; unlock and retry.
- **Stalls on a big first run** — disable Auto-Lock (Settings → Display &
  Brightness → Auto-Lock → Never) so the phone stays awake.
- **`._` files appear** — harmless macOS metadata sidecars on exFAT drives; both
  tools ignore them.
