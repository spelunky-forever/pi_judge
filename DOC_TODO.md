# Documentation TODO (maintainer notes)

Doc/spec frictions found during QA that a student **cannot discover without
decrypting the judge**. These are fair-game instructor content (the *contract*,
not the answer) and should land in `README.md`. Tracked here so they aren't lost.

## README fixes

- **Wrong edit path (R-1).** README says edit `implementation/pi.cpp` — that file
  does not exist. The real files are:
  - `implementation/pi1/src/pi1.cpp`
  - `implementation/pi2/src/pi2.cpp`
  - `implementation/pi3/src/pi3.cpp`
- **Three separate tasks (R-2).** README implies one TODO; there are three, with
  different requirements. Document each level's contract (below).

## Per-level I/O contract to document

Common framing (already in `main.cpp`, outside the TODO — don't edit):
- Input line: `[tag]N M`  →  output: `[tag]<digits>[END]`
- **Positions are 1-indexed and inclusive**: emit digits at positions `N..M`.
  *(This is the off-by-one everyone hits — `data[N-1 .. M-1]` in 0-indexed terms.)*

| Level | Data file | Decode | Notes |
|---|---|---|---|
| pi1 | `pi_data.txt` (ASCII) | `digit(p) = pi_data_txt_start[p-1]` | span ≤ 5000; per-digit `Serial.write` is fine |
| pi2 | `pi_data.bin` (BCD, 2 digits/byte) | `b = pi_data_bin_start[(p-1)>>1]`; high nibble `b>>4` if `(p-1)` even, else low nibble `b&0x0F` | spans up to 279k; no time pressure |
| pi3 | `pi_data.bin` (BCD) | same as pi2 | **see below** |

### pi3 specifics (R-5, R-6, R-16)
- Spans up to **279,000 digits**. You **must** batch output: write decoded chars
  into the provided `out_buf[2048]` and flush with `Serial.write(...)` when near
  full. Naive per-digit `Serial.print` → time-out; filling `out_buf` without
  flushing → **buffer overflow → ESP32 crash** (surfaces as a cryptic Runtime
  Error with no hint it's your buffer).
- pi3 is **time-scored**: `score = (pass/total) * 40 * timeRatio`, where
  `timeRatio = 11.5 / (level_time − 5)` once `level_time − 5 > 11.5`, else `1.0`.
  In practice the level must finish in roughly **≤ 16.5 s** for the full 40.
  Document this target — it's currently invisible, so a correct-but-slow solution
  silently loses points. (Confirmed 100/100 *is* reachable: batched pi3 ≈ 11 s.)

## Lower-priority / deferred (judge code, not docs)

- **Crash vs build-fail masking (partial R-13).** A crash banner (ending 5) can
  still be overwritten by a *later* level's **build-fail** or **board-not-ready**.
  Only timeout/partial are guarded now (commit `e211474`). To fully fix, rank all
  endings by severity instead of last-write-wins, or guard the `ending = 2/1`
  sites too.
- **Missing-data totals.** A missing `.in` shows the level as `0/0` (we can't count
  a file that isn't there), so STAGE TOTAL excludes it. Acceptable, but note it.
- **`--show-diff` truncation (R-7).** Diffs are cut to 55 chars; for long spans the
  divergence point is hidden. Could center the window on the first mismatch.

## By design / not changing
R-11 (40 MHz + 460800 baud may glitch), R-21 (no enforcement that only the TODO
was edited), R-22 (multi-board path untested), data encryption (intentional for a
graded judge).
