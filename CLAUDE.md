# CLAUDE.md — working notes for `orangepi_manager` (`opm`)

Tool that copies recordings off **Orange Pi OS SD cards** read via a USB
card-reader hub, verifies them, and optionally deletes the originals. CLI +
local web UI over one shared engine. Read `README.md` for the user-facing view.

## Files (this is the whole project)
- `opm.py` — engine **and** CLI. Single file by design. Stages live here:
  `detect_cards`, `existing_mount`, `mount_ro`/`cleanup_mount`, `identify`,
  `do_copy`, `verify`, `missing_on_dest`, `delete_source`, `process_card`, plus
  stats helpers `dir_stats`/`list_sessions`/`disk_free` and path helpers
  `_leaf`/`_rsync_src`/`dest_path_for`.
- `webui.py` — stdlib `http.server` (no Flask). Background job + `sampler_loop`
  thread; routes `/api/{detect,config,precheck,run,status,eject}`. Reuses
  `opm.process_card` via an `on_stage(slot, stage, info)` callback.
- `web/index.html` — single-page UI, **vanilla JS, no build step**. Server
  serves it from disk on every GET (edit + reload browser; no server restart for
  frontend-only changes).
- `config.yaml`, `test_opm.py`, `docs/` (screenshots + `demo.html`).

## Invariants — do not break these
- **Never write to a card during copy.** Read path uses `blockdev --setro` +
  `mount -o ro,noload`. If `existing_mount(dev)` finds a desktop auto-mount under
  `/media`, reuse it (don't mount our own; don't `setro` a rw mount).
- **Slot identity = `/dev/disk/by-path`**, never `/dev/sdX` (unstable) or serial
  (multi-slot readers share one serial across LUNs).
- **A card is only eligible if its ext label matches `safety.require_label`
  (`opi_root`).** This is the guard against touching unrelated USB drives.
- **Deletion has three gates, in order:** `verify()` (rsync -c) →
  `missing_on_dest()` (presence/size, respects excludes) →
  `rsync -aHAXc --remove-source-files` (re-checksums, re-copies if needed before
  removing). Don't remove a gate. Deletion never removes dirs; `prune_empty_dirs`
  only deletes empty session dirs, never the leaf.
- **`copy.flatten`** must stay consistent across `do_copy`, `verify`,
  `missing_on_dest`, `delete_source` (all via `_rsync_src`) and the sampler
  (`dest_path_for`). If you change copy layout, change all of them.
- **Mounting needs root.** `run`/`eject` require euid 0; `detect`/`precheck` don't.
  Output is `chown`ed back to `SUDO_UID`.

## Run & test
- Tests: `python3 -m unittest test_opm -v` (synthetic dirs, no root, needs rsync).
- Compile: `python3 -m py_compile opm.py webui.py`.
- Frontend JS check (Node 22 is available): extract the `<script>` and
  `node --check`; you can `eval` it with stubbed `document`/`fetch` to unit-test
  pure render fns (`sessRows`, `renderProgress`, `renderPlan`) — see how prior
  work verified them.
- Live server: `sudo python3 webui.py` → http://127.0.0.1:8765. **The user often
  has their own `sudo webui.py` already bound to 8765** — you can't kill it
  (root) and your test server won't bind. Verify via component tests instead.
- Screenshots: `docs/demo.html` renders the real UI with mock data; capture with
  `firefox --headless --window-size=W,H --screenshot out.png file://…/demo.html`,
  crop with Pillow.

## Gotchas
- Don't `pkill -f webui.py` — the pattern matches your own shell. Kill by port
  (`ss`/`fuser`) or `/proc` scan excluding `os.getpid()`.
- The Orange Pi `/home` shows a file-manager padlock because it's root-owned
  (`drwxr-xr-x root root`), **not** encrypted.
- Cards get swapped during testing; `detect`/`precheck` reflect whatever is in
  the reader *now*. Don't assume a fixed card.
- PC system disk is small relative to full cards — real workflow needs an
  external destination.

## Style
Match the existing code: engine functions are small and pure where possible and
shell out to `lsblk`/`rsync`/`mount`; the UI is plain JS with template strings.
Keep changes consistent with these patterns; don't introduce build tooling or
heavy dependencies.
