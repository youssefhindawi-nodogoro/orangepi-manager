#!/usr/bin/env python3
"""
opm - Orange Pi Manager
=======================
Read Orange Pi OS SD cards through USB card readers and copy selected files to
this PC, then (optionally) delete the verified source files from the card.

Per-card pipeline:
    detect -> select ext4 root -> mount READ-ONLY (safe) -> identify
           -> copy (rsync) -> verify (checksum) -> delete source -> unmount

Why it is more than `mount /dev/sda1`:
  * Device names (/dev/sda..sdd) are NOT stable across replugs, so physical
    "slot" identity comes from /dev/disk/by-path (the USB hub-port topology).
  * Cards from an unclean shutdown have a dirty ext4 journal: a plain `mount -o ro`
    refuses them and a normal mount WRITES (journal replay). We mount with
    `ro,noload` + `blockdev --setro` so the read phase never writes a byte.
  * Deletion needs a writable, consistent fs, so the delete phase unmounts,
    runs `e2fsck -p` if needed, remounts rw, and removes ONLY files that rsync
    re-confirms (checksum) are byte-identical on this PC.

Usage:
    python3 opm.py detect                 # preview detected cards (no root needed)
    sudo python3 opm.py run               # full pipeline (root required to mount)
    sudo python3 opm.py run --dry-run     # show what would happen; touch nothing
    sudo python3 opm.py run --no-delete   # copy + verify only, keep source
    sudo python3 opm.py run --yes         # skip the delete confirmation prompt
    [--config PATH]                       # default: ./config.yaml
"""

import argparse
import errno
import json
import os
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required:  pip install pyyaml  (or apt install python3-yaml)")

SCRIPT_DIR = Path(__file__).resolve().parent
PLACEHOLDER = "/REPLACE_ME"


# --------------------------------------------------------------------------- #
# small command helpers
# --------------------------------------------------------------------------- #
class Cancelled(Exception):
    """Raised inside a cancellable run() when a stop was requested mid-command."""


def run(cmd, check=True, stop=None, procs=None):
    """Run a command, capturing output. Raises RuntimeError on failure if `check`.
    If `stop` (a callable returning True to cancel) is given, run it as a tracked
    subprocess (registered in `procs` = (set, lock)) and terminate it if stop() turns
    true — raising Cancelled. rsync is safe to interrupt: fully-transferred files stay
    intact and in-flight files are temp files that are never renamed into place."""
    if stop is None:
        res = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if check and res.returncode != 0:
            raise RuntimeError(
                f"command failed (rc={res.returncode}): {' '.join(cmd)}\n{res.stderr.strip()}")
        return res
    p = subprocess.Popen(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if procs is not None:
        with procs[1]:
            procs[0].add(p)
    try:
        while True:
            try:
                out, err = p.communicate(timeout=0.3)
                break
            except subprocess.TimeoutExpired:
                if stop():
                    p.terminate()
                    try:
                        out, err = p.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        out, err = p.communicate()
                    raise Cancelled()
    finally:
        if procs is not None:
            with procs[1]:
                procs[0].discard(p)
    res = subprocess.CompletedProcess(cmd, p.returncode, out, err)
    if check and res.returncode != 0:
        raise RuntimeError(
            f"command failed (rc={res.returncode}): {' '.join(cmd)}\n{(err or '').strip()}")
    return res


def load_config(path):
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    # defaults
    cfg.setdefault("destination", str(SCRIPT_DIR / "data"))
    cfg.setdefault("copy_paths", [])
    cfg.setdefault("exclude", [])
    cfg.setdefault("identify", {})
    cfg["identify"].setdefault("mode", "both")
    cfg["identify"].setdefault("content_sources", ["/etc/hostname", "/etc/machine-id"])
    cfg.setdefault("mount", {})
    cfg["mount"].setdefault("base", "/mnt/opm")
    cfg["mount"].setdefault("fsck_dirty", True)
    cfg.setdefault("after_copy", {})
    cfg["after_copy"].setdefault("verify", True)
    cfg["after_copy"].setdefault("delete_source", True)
    cfg["after_copy"].setdefault("prune_empty_dirs", True)
    cfg.setdefault("copy", {})
    cfg["copy"].setdefault("parallel", True)
    cfg["copy"].setdefault("max_workers", 4)
    cfg["copy"].setdefault("flatten", True)
    cfg["copy"].setdefault("skip_empty", True)    # never copy/delete 0-byte files
    cfg.setdefault("require_confirm", True)
    cfg.setdefault("safety", {})
    cfg["safety"].setdefault("require_label", None)
    cfg["safety"].setdefault("min_size_gb", 0)
    cfg.setdefault("preview", {})
    cfg["preview"].setdefault("seconds", 20)      # first N seconds (fast); 0 = full recording.
                                                  # These are 4000x1200 HEVC, slow to decode —
                                                  # a cap reads only the start, so it's quick.
    cfg["preview"].setdefault("width", 480)       # downscale width for fast/small preview
    cfg["preview"].setdefault("fps", 15)          # lower fps = faster transcode + smaller
    cfg["preview"].setdefault("crf", 34)          # higher = lower quality, faster, smaller
    return cfg


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #
def system_disk():
    """The physical disk that backs '/', so we never touch it."""
    src = run(["findmnt", "-no", "SOURCE", "/"]).stdout.strip()  # e.g. /dev/nvme0n1p6
    pk = run(["lsblk", "-no", "PKNAME", src], check=False).stdout.strip()
    return f"/dev/{pk}" if pk else src


def bypath_map():
    """realpath(/dev/sdX) -> by-path symlink name (encodes the USB hub port)."""
    m, d = {}, "/dev/disk/by-path"
    if os.path.isdir(d):
        for name in os.listdir(d):
            if "-part" in name:  # we want whole-disk entries, not partitions
                continue
            m[os.path.realpath(os.path.join(d, name))] = name
    return m


def detect_cards(cfg):
    """Return a list of cards (one per USB SD reader holding a card), slot-ordered."""
    out = run(["lsblk", "-J", "-b", "-o",
               "NAME,PATH,TYPE,FSTYPE,SIZE,RM,HOTPLUG,TRAN,MODEL,SERIAL,LABEL"]).stdout
    blk = json.loads(out)["blockdevices"]
    sysdisk = system_disk()
    bpm = bypath_map()

    req = (cfg.get("safety") or {}).get("require_label")
    min_bytes = ((cfg.get("safety") or {}).get("min_size_gb") or 0) * (1024 ** 3)

    cards = []
    for dev in blk:
        if dev.get("type") != "disk" or dev.get("tran") != "usb":
            continue                       # only USB-attached disks (the readers)
        if dev.get("path") == sysdisk:
            continue                       # never the system disk
        card = {
            "disk": dev["path"],
            "model": (dev.get("model") or "").strip(),
            "serial": (dev.get("serial") or "").strip(),
            "bypath": bpm.get(os.path.realpath(dev["path"]), ""),
            "rootpart": None, "fstype": None, "size": None, "label": "",
            "mounted_at": None, "eligible": False, "error": None,
        }
        size = dev.get("size") or 0
        ext = [c for c in (dev.get("children") or [])
               if (c.get("fstype") or "").startswith("ext")]
        if size == 0 or size < min_bytes:
            card["error"] = "empty slot / no media"
        elif not ext:
            card["error"] = "no ext2/3/4 (Linux root) partition"
        else:
            ext.sort(key=lambda c: c.get("size") or 0, reverse=True)  # largest = root
            root = ext[0]
            label = root.get("label") or ""
            card.update(rootpart=root["path"], fstype=root.get("fstype"),
                        size=root.get("size"), label=label,
                        mounted_at=existing_mount(root["path"]))
            if req and not re.search(req, label):
                card["error"] = f"label {label or '(none)'!r} != require_label {req!r} (skipped)"
            else:
                card["eligible"] = True
        cards.append(card)

    cards.sort(key=lambda c: c["bypath"] or c["disk"])  # stable physical order
    for i, c in enumerate(cards, 1):
        c["slot"] = i
    return cards


# --------------------------------------------------------------------------- #
# identify
# --------------------------------------------------------------------------- #
def _read_first(mnt, rels):
    for rel in rels:
        try:
            v = (Path(mnt) / rel.lstrip("/")).read_text().strip()
            if v:
                return v
        except OSError:
            continue
    return ""


def _sanitize(name):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:64]


def card_id_for(mnt, cfg, slot):
    """Stable, unique-per-card folder name (no cross-card de-duplication). Prefers a
    user marker file, then hostname+machine-id (machine-id is unique per OS install
    even for dd-cloned cards, so it keeps each physical card in its own folder and is
    the SAME across re-copies), then hostname, then the physical slot."""
    mode = cfg["identify"].get("mode", "both")
    if mode == "slot":
        return f"slot{slot}"
    marker = (_read_first(mnt, [cfg["identify"]["marker_file"]])
              if cfg["identify"].get("marker_file") else "")
    if marker:
        return _sanitize(marker)
    host = _read_first(mnt, ["/etc/hostname"])
    mid = _read_first(mnt, ["/etc/machine-id"])
    if mid:
        return _sanitize(f"{host or 'opi'}-{mid[:8]}")
    if host:
        return _sanitize(host)
    return f"slot{slot}"


def identify(card, mnt, cfg, assigned, lock):
    """card_id_for + a within-run uniqueness guard (only triggers if two cards resolve
    to the same id, e.g. true clones that share a machine-id)."""
    cid = card_id_for(mnt, cfg, card["slot"])
    with lock:
        if cid in assigned:
            cid = f"{cid}-{card['slot']}"
        assigned.add(cid)
    return cid


# --------------------------------------------------------------------------- #
# mount / unmount
# --------------------------------------------------------------------------- #
def existing_mount(dev):
    """If the device is already mounted (e.g. the desktop auto-mounted it under
    /media), return that mountpoint, else None."""
    r = run(["findmnt", "-nro", "TARGET", "--source", dev], check=False)
    lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    return lines[0] if lines else None


def mount_ro(card, mnt):
    os.makedirs(mnt, exist_ok=True)
    run(["blockdev", "--setro", card["disk"]])
    run(["blockdev", "--setro", card["rootpart"]])
    run(["mount", "-o", "ro,noload", card["rootpart"], mnt])


def is_mounted(mnt):
    return subprocess.run(["mountpoint", "-q", mnt]).returncode == 0


def cleanup_mount(card, mnt):
    """Best-effort: unmount, restore writability, remove mountpoint."""
    if is_mounted(mnt):
        run(["umount", mnt], check=False)
    run(["blockdev", "--setrw", card["rootpart"]], check=False)
    run(["blockdev", "--setrw", card["disk"]], check=False)
    try:
        os.rmdir(mnt)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# copy / verify / delete
#   flatten=True  (default): copy the leaf dir only        -> dest/<leaf>/...
#   flatten=False          : recreate the full on-card path -> dest/a/b/<leaf>/...
# --------------------------------------------------------------------------- #
def _leaf(p):
    return os.path.basename(p.rstrip("/")) or p.strip("/")


def _rsync_src(mnt, p, flatten):
    """(source_arg, extra_rsync_flags) for copy_path `p`."""
    if flatten:
        return os.path.join(mnt, p.lstrip("/")), []        # copy the dir itself
    return f"{mnt}/./{p.lstrip('/')}", ["-R"]              # /./ pivot recreates full path


def dest_path_for(dest, p, flatten):
    """Where copy_path `p` lands under `dest`."""
    if flatten:
        return os.path.join(str(dest), _leaf(p))
    return os.path.join(str(dest), p.lstrip("/"))


def _excludes(cfg):
    out = []
    for e in cfg.get("exclude", []):
        out += ["--exclude", e]
    return out


def _size_filter(cfg):
    """rsync flag that skips zero-byte files (they hold no data). Applied UNIFORMLY
    to copy / verify / missing_on_dest / delete_source so an empty file is never
    copied, never falsely fails verify (absent on dest), never blocks the pre-delete
    guard, and is never removed from the card. Toggle with copy.skip_empty (default
    on)."""
    return ["--min-size=1"] if cfg.get("copy", {}).get("skip_empty", True) else []


# Filesystems that can't store Unix ownership/permissions. Preserving them (-a)
# there makes rsync fail with rc=23 on chown ("Operation not permitted"), so we
# copy contents + mtimes only for these destinations.
_NONUNIX_FS = {"vfat", "fat", "fat12", "fat16", "fat32", "msdos", "exfat",
               "exfat-fuse", "ntfs", "ntfs3", "ntfs-3g", "fuseblk", "hfs",
               "hfsplus", "udf", "iso9660"}


def _flags_for_fs(fstype):
    """rsync mode flags appropriate for a destination filesystem type."""
    if (fstype or "").strip().lower() in _NONUNIX_FS:
        return ["-rt", "--no-links", "--modify-window=2"]   # contents + times only
    return ["-aHAX", "--numeric-ids"]                       # full preservation (ext4, …)


def _fs_type(path):
    p = os.path.abspath(str(path))
    while not os.path.exists(p) and p != "/":
        p = os.path.dirname(p)
    return run(["findmnt", "-nro", "FSTYPE", "--target", p], check=False).stdout.strip()


def archive_flags(dest):
    """rsync preservation flags appropriate for the destination filesystem."""
    return _flags_for_fs(_fs_type(dest))


def do_copy(mnt, dest, cfg, dry, stop=None, procs=None):
    if not dry:
        dest.mkdir(parents=True, exist_ok=True)
    flatten = cfg["copy"].get("flatten", True)
    af = archive_flags(dest)
    copied, missing = [], []
    for p in cfg["copy_paths"]:
        if not (Path(mnt) / p.lstrip("/")).exists():
            missing.append(p)
            continue
        src, rflags = _rsync_src(mnt, p, flatten)
        cmd = ["rsync"] + af + rflags + ["--info=stats1"] + _size_filter(cfg) + _excludes(cfg)
        if dry:
            cmd.append("--dry-run")
        cmd += [src, str(dest) + "/"]
        run(cmd, stop=stop, procs=procs)
        copied.append(p)
    return copied, missing


def verify(mnt, dest, cfg, paths, stop=None, procs=None):
    """Checksum dry-run; returns list of files that DON'T match (empty == verified)."""
    flatten = cfg["copy"].get("flatten", True)
    af = archive_flags(dest)
    failed = []
    for p in paths:
        src, rflags = _rsync_src(mnt, p, flatten)
        cmd = (["rsync"] + af + ["-c", "-n"] + rflags + ["--out-format=%i %n"]
               + _size_filter(cfg) + _excludes(cfg) + [src, str(dest) + "/"])
        for line in run(cmd, stop=stop, procs=procs).stdout.splitlines():
            code = line.split(" ", 1)[0]
            if code.startswith((">f", "<f", "cf")):   # a regular file would transfer => mismatch
                failed.append(line)
    return failed


def missing_on_dest(mnt, dest, cfg, paths, stop=None, procs=None):
    """Files NOT yet present (matching size/mtime) at the destination. Empty list
    means every source file is safely on the destination. Uses an rsync dry-run
    (respects excludes; size+mtime, no checksums) so it's a fast final guard run
    right before deletion."""
    flatten = cfg["copy"].get("flatten", True)
    af = archive_flags(dest)
    miss = []
    for p in paths:
        src, rflags = _rsync_src(mnt, p, flatten)
        cmd = (["rsync"] + af + ["-n", "--out-format=%i %n"] + rflags
               + _size_filter(cfg) + _excludes(cfg) + [src, str(dest) + "/"])
        for line in run(cmd, stop=stop, procs=procs).stdout.splitlines():
            parts = line.split(" ", 1)
            if parts[0].startswith((">f", "<f", "cf")):
                miss.append(parts[1] if len(parts) > 1 else parts[0])
    return miss


def delete_source(card, mnt, cfg, dest, paths, owned=True, stop=None, procs=None):
    """Remove copied files from the card via rsync --remove-source-files. If we own a
    read-only mount, switch it to read-write first (with an e2fsck pass). If the
    desktop already has it mounted read-write (mnt under /media), delete from there.

    Speed: when verify() already checksum-confirmed the copy (the default), this does
    a fast size+mtime match before unlinking — no second full re-read of every byte.
    Only when verify is disabled does it fall back to a checksum (-c) compare here, so
    there is always exactly one checksum gate before any file is removed."""
    if owned:
        cleanup_mount(card, mnt)                    # drop our read-only mount
        os.makedirs(mnt, exist_ok=True)
        if cfg["mount"].get("fsck_dirty", True):
            rc = subprocess.run(["e2fsck", "-p", card["rootpart"]],
                                text=True, capture_output=True).returncode
            if rc >= 4:                             # uncorrected errors -> keep data, abort
                mount_ro(card, mnt)
                return {"deleted": False, "error": f"e2fsck rc={rc}; delete aborted, source kept"}
        run(["mount", "-o", "rw", card["rootpart"], mnt])
    # SAFETY: never delete unless every file is confirmed present on the destination
    miss = missing_on_dest(mnt, dest, cfg, paths, stop=stop, procs=procs)
    if miss:
        return {"deleted": False,
                "error": f"delete aborted - {len(miss)} file(s) not confirmed on "
                         f"destination (e.g. {miss[0]}); source kept"}
    flatten = cfg["copy"].get("flatten", True)
    af = archive_flags(dest)
    csum = [] if cfg["after_copy"].get("verify", True) else ["-c"]
    for p in paths:
        src, rflags = _rsync_src(mnt, p, flatten)
        cmd = (["rsync"] + af + csum + ["--remove-source-files"] + rflags
               + _size_filter(cfg) + _excludes(cfg) + [src, str(dest) + "/"])
        run(cmd, stop=stop, procs=procs)
        if cfg["after_copy"].get("prune_empty_dirs", True):
            target = Path(mnt) / p.lstrip("/")
            if target.is_dir():
                run(["find", str(target), "-mindepth", "1", "-type", "d",
                     "-empty", "-delete"], check=False)
    run(["sync"])
    return {"deleted": True, "error": None}


def chown_back(path):
    """If running under sudo, give copied data to the invoking user, not root."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if os.geteuid() == 0 and uid and gid:
        run(["chown", "-R", f"{uid}:{gid}", str(path)], check=False)


# --------------------------------------------------------------------------- #
# stats / disk space  (used by the web UI for progress + pre-copy checks)
# --------------------------------------------------------------------------- #
def dir_stats(path):
    """(total_bytes, file_count, dir_count) under `path`; zeros if missing.
    Empty (0-byte) files are NOT counted: opm never copies them (rsync --min-size=1
    / copy.skip_empty), so excluding them keeps the byte/file totals and the
    'already copied?' presence check consistent with what actually lands on the
    destination. Stat-only (no reads), so it's fast even for many GB."""
    tb = fc = dc = 0
    if not os.path.exists(path):
        return (0, 0, 0)
    for root, dirs, files in os.walk(path):
        dc += len(dirs)
        for f in files:
            try:
                sz = os.lstat(os.path.join(root, f)).st_size
            except OSError:
                continue
            if sz > 0:                 # skip empties — they're never transferred
                tb += sz
                fc += 1
    return (tb, fc, dc)


_SESSION_RE = re.compile(r"(\d{4})(\d{2})(\d{2})[-_](\d{2})(\d{2})(\d{2})")


def list_sessions(path):
    """Top-level subdirs of `path` as recording sessions. A name like
    20260615-205310 becomes date 2026-06-15 / time 20:53:10. Each entry also
    summarises size, file count and record types (file stems, e.g. 'stereo')."""
    out = []
    try:
        names = sorted(os.listdir(path))
    except OSError:
        return out
    for name in names:
        full = os.path.join(path, name)
        if not os.path.isdir(full):
            continue
        m = _SESSION_RE.match(name)
        date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""
        tm = f"{m.group(4)}:{m.group(5)}:{m.group(6)}" if m else ""
        b, fc, _ = dir_stats(full)
        types = {}
        try:
            for f in os.listdir(full):
                fp = os.path.join(full, f)
                try:                                           # skip empties + bad inodes,
                    if not (os.path.isfile(fp) and os.path.getsize(fp) > 0):
                        continue                               # so types match copied files
                except OSError:
                    continue
                stem = re.sub(r"(_\d+)?\.[^.]+$", "", f)       # stereo_000.mp4 -> stereo
                types[stem] = types.get(stem, 0) + 1
        except OSError:
            pass
        out.append({"name": name, "date": date, "time": tm,
                    "bytes": b, "files": fc, "types": types})
    return out


def oversized_files(root, limit):
    """Files at/over `limit` bytes under `root` (e.g. for FAT32's 4 GiB cap).
    Returns [(relative_path, size), …]. Stat-only, no reads."""
    out = []
    for d, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            try:
                sz = os.lstat(p).st_size
            except OSError:
                continue
            if sz >= limit:
                out.append((os.path.relpath(p, root), sz))
    return out


def disk_free(path):
    """(free_bytes, total_bytes) of the filesystem holding `path`, walking up to
    an existing parent if `path` does not exist yet."""
    p = os.path.abspath(path)
    while not os.path.exists(p) and p != "/":
        p = os.path.dirname(p)
    try:
        st = os.statvfs(p)
    except OSError:
        return (0, 0)
    return (st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize)


# Errno values that mean "this card's filesystem is damaged / can't be read",
# as opposed to a transient or permission problem. EUCLEAN is ext4's
# "Structure needs cleaning" (a dirty/corrupt fs that wants e2fsck).
_FS_DAMAGED = {errno.EUCLEAN, errno.EIO, errno.EROFS}


def _health_msg(e):
    base = getattr(e, "strerror", None) or str(e)
    if getattr(e, "errno", None) == errno.EUCLEAN:
        return (f"filesystem needs cleaning ({base}) — unplug and replug the card "
                f"(or run e2fsck), then re-check")
    return (f"card read error ({base}) — unplug and replug the card, then re-check")


def probe_health(mnt, paths, max_checks=4000):
    """Bounded, early-exiting read probe of a mounted card. Returns (ok, message):
    detects an ext4 filesystem that is corrupt / 'Structure needs cleaning'
    (EUCLEAN) or throwing I/O errors (EIO) BEFORE a copy starts, so the UI can tell
    the user to re-seat the card / run fsck instead of failing mid-copy or crashing
    a preview. Stat-only (no file reads) and capped at `max_checks` inode stats, so
    it's cheap on a healthy card and returns on the first bad inode on a sick one."""
    if not mnt or not os.path.isdir(mnt):
        return (False, "card is not mounted")
    try:
        os.statvfs(mnt)
    except OSError as e:
        return (False, _health_msg(e))
    checks = 0
    for p in paths:
        full = os.path.join(mnt, p.lstrip("/"))
        try:
            if not os.path.isdir(full):
                continue
            stack = [full]
            while stack and checks < max_checks:
                with os.scandir(stack.pop()) as it:
                    for de in it:
                        checks += 1
                        if checks >= max_checks:
                            break
                        if de.is_dir(follow_symlinks=False):
                            stack.append(de.path)
                        else:
                            de.stat()           # surfaces EUCLEAN on a corrupt inode
        except OSError as e:
            if getattr(e, "errno", None) in _FS_DAMAGED:
                return (False, _health_msg(e))
            # permission / transient errors are not a reason to block a copy
    return (True, None)


# --------------------------------------------------------------------------- #
# per-card driver
# --------------------------------------------------------------------------- #
def process_card(card, cfg, ts, dry, do_delete, assigned, lock, on_stage=None,
                 stop=None, procs=None, verify_copy=None):
    slot = card["slot"]

    # Whether to checksum-verify the copy. `None` => follow config (CLI default).
    # The web UI passes verify_copy=do_delete: a "keep source" copy skips the
    # expensive full re-read (the card is the backup), while "copy + delete"
    # always verifies every file before anything is removed.
    if verify_copy is None:
        verify_copy = cfg["after_copy"].get("verify", True)

    def stage(s, info=None):
        if on_stage:
            on_stage(slot, s, info)

    res = {"slot": slot, "card_id": f"slot{slot}", "status": "ok",
           "copied": [], "missing": [], "deleted": False, "error": None, "dest": None}
    if not card.get("rootpart"):
        res.update(status="skipped", error=card.get("error", "no root partition"))
        stage("skipped")
        return res
    if stop and stop():                              # cancelled before we started this card
        res.update(status="cancelled")
        stage("done")
        return res

    own_mnt = os.path.join(cfg["mount"]["base"], f"slot{slot}")
    pre = existing_mount(card["rootpart"])           # desktop or our browse-mount may have it
    base = cfg["mount"]["base"].rstrip("/")
    ours = bool(pre) and (pre == own_mnt or pre.startswith(base + "/"))
    owned = pre is None or ours                      # a mount we control => can remount rw to delete
    use_mnt = pre or own_mnt
    try:
        if pre is None:
            stage("mounting")
            mount_ro(card, own_mnt)                  # read-only: safe even in dry-run
        elif ours:
            stage("mounting")                        # reuse our own read-only browse mount
        else:
            stage("premounted")                      # reuse the desktop /media (read-write) mount
        res["card_id"] = identify(card, use_mnt, cfg, assigned, lock)
        # stable per-card folder (no timestamp) -> re-copies merge in and dedup
        dest = Path(cfg["destination"]) / res["card_id"]
        res["dest"] = str(dest)
        stage("copying", {"dest": str(dest), "src": use_mnt})
        res["copied"], res["missing"] = do_copy(use_mnt, dest, cfg, dry,
                                                stop=stop, procs=procs)

        if dry or not res["copied"]:
            return res
        chown_back(Path(cfg["destination"]) / res["card_id"])

        if verify_copy:
            stage("verifying")
            fails = verify(use_mnt, dest, cfg, res["copied"], stop=stop, procs=procs)
            if fails:
                res.update(status="verify-failed",
                           error=f"{len(fails)} file(s) did not match; source kept")
                return res

        if do_delete:
            stage("deleting")
            d = delete_source(card, use_mnt, cfg, dest, res["copied"], owned,
                              stop=stop, procs=procs)
            res["deleted"] = d["deleted"]
            if d["error"]:
                res.update(status="delete-skipped", error=d["error"])
    except Cancelled:                               # user cancelled mid-copy/verify
        res.update(status="cancelled", error="cancelled (no source data lost)")
    except Exception as e:                          # noqa: BLE001 - report, never crash the batch
        res.update(status="error", error=str(e))
    finally:
        if owned:
            cleanup_mount(card, use_mnt)             # unmount what we mounted / control
        stage("done")
    return res


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def _fmt_size(n):
    if not n:
        return "?"
    units = ["B", "K", "M", "G", "T"]
    f = float(n)
    for u in units:
        if f < 1024 or u == "T":
            return f"{f:.0f}{u}" if u == "B" else f"{f:.1f}{u}"
        f /= 1024


def cmd_detect(cfg, _args):
    cards = detect_cards(cfg)
    if not cards:
        print("No USB SD-card readers detected.\n"
              "Insert the cards into the hub and try again (this command needs no root).")
        return 0
    print(f"Detected {len(cards)} reader slot(s):\n")
    print(f"{'slot':<5}{'use':<4}{'device':<11}{'root':<11}{'fs':<6}{'size':<8}{'label':<12}{'hub-port (by-path)'}")
    print("-" * 100)
    for c in cards:
        print(f"{c['slot']:<5}{('yes' if c['eligible'] else 'no'):<4}{c['disk']:<11}"
              f"{(c['rootpart'] or '-'):<11}{(c['fstype'] or '-'):<6}{_fmt_size(c['size']):<8}"
              f"{(c['label'] or '-'):<12}{c['bypath'] or '-'}")
        if c["error"]:
            print(f"      .. {c['error']}")
    elig = sum(1 for c in cards if c["eligible"])
    print(f"\n{elig} eligible Orange Pi card(s)  "
          f"(eligibility = ext root + label matches safety.require_label).")
    return 0


def cmd_run(cfg, args):
    if os.geteuid() != 0:
        sys.exit("`run` must mount filesystems and needs root.\n"
                 "Re-run as:  sudo python3 opm.py run")
    bad = [p for p in cfg["copy_paths"] if PLACEHOLDER in p]
    if bad or not cfg["copy_paths"]:
        sys.exit(f"Edit config.yaml: set real copy_paths (still contains {PLACEHOLDER!r}).")

    cards = detect_cards(cfg)
    usable = [c for c in cards if c.get("eligible")]
    if not usable:
        print("No eligible Orange Pi cards (need ext root + matching label).\n")
        return cmd_detect(cfg, args)

    dry = args.dry_run
    do_delete = (cfg["after_copy"].get("delete_source", False)
                 and not args.no_delete and not dry)

    print(f"Cards: {len(cards)}  usable: {len(usable)}  "
          f"copy_paths: {len(cfg['copy_paths'])}  dest: {cfg['destination']}")
    print(f"mode: {'DRY-RUN (no changes)' if dry else 'LIVE'}   "
          f"delete source after verify: {'YES' if do_delete else 'no'}\n")

    if do_delete and cfg.get("require_confirm", True) and not args.yes:
        ans = input(f"This will DELETE verified files from {len(usable)} card(s) "
                    f"after copying. Proceed? [y/N] ").strip().lower()
        if ans != "y":
            print("Delete disabled for this run; doing copy + verify only.")
            do_delete = False

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    assigned, lock = set(), threading.Lock()

    if cfg["copy"].get("parallel", True) and len(usable) > 1:
        results = []
        with ThreadPoolExecutor(max_workers=cfg["copy"].get("max_workers", 4)) as ex:
            futs = [ex.submit(process_card, c, cfg, ts, dry, do_delete, assigned, lock)
                    for c in usable]
            for f in as_completed(futs):
                results.append(f.result())
        results.sort(key=lambda r: r["slot"])
    else:
        results = [process_card(c, cfg, ts, dry, do_delete, assigned, lock) for c in usable]

    print("\n=== summary ===")
    print(f"{'slot':<5}{'card-id':<24}{'status':<16}{'copied':<8}{'missing':<8}{'deleted'}")
    print("-" * 75)
    for r in results:
        print(f"{r['slot']:<5}{r['card_id'][:23]:<24}{r['status']:<16}"
              f"{len(r['copied']):<8}{len(r['missing']):<8}{'yes' if r['deleted'] else 'no'}")
        if r["error"]:
            print(f"      !! {r['error']}")
        if r["missing"]:
            print(f"      (not on card: {', '.join(r['missing'])})")
    failures = [r for r in results if r["status"] != "ok"]
    print(f"\n{len(results) - len(failures)}/{len(results)} card(s) ok. "
          f"Data under: {cfg['destination']}")
    return 1 if failures else 0


def main():
    ap = argparse.ArgumentParser(prog="opm", description="Orange Pi SD-card copy manager")
    ap.add_argument("--config", default=str(SCRIPT_DIR / "config.yaml"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("detect", help="list detected SD-card readers (no root)")
    pr = sub.add_parser("run", help="copy (+verify, +delete) from all cards")
    pr.add_argument("--dry-run", action="store_true",
                    help="mount read-only and preview the file list; write/delete nothing")
    pr.add_argument("--no-delete", action="store_true", help="copy + verify only")
    pr.add_argument("--yes", action="store_true", help="skip the delete confirmation")
    args = ap.parse_args()

    cfg = load_config(args.config)
    return {"detect": cmd_detect, "run": cmd_run}[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
