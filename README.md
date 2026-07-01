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
- **No duplicates added** — a file already present (same name + size) is
  skipped. When the iPhone's `IMG_####` counter wraps and two *different* photos
  share a name, the later one gets a ` 1`/` 2` suffix so nothing is lost.
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
trash folder you can review and delete when happy.

---

## Troubleshooting

- **No iPhone detected / not trusted** — unlock the phone, tap *Trust This
  Computer*; check with `idevice_id -l`, and if needed run `idevicepair pair`.
- **No photos found under DCIM** — the phone is locked; unlock and retry.
- **Stalls on a big first run** — disable Auto-Lock (Settings → Display &
  Brightness → Auto-Lock → Never) so the phone stays awake.
- **`._` files appear** — harmless macOS metadata sidecars on exFAT drives; both
  tools ignore them.
