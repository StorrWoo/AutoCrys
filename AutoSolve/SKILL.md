---
name: AutoSolve
description: Script-first SHELXT structure solution for AutoCrys after AutoXDS. Use this skill to decide the target dataset/HKL, confirm composition and space-group intent, then delegate deterministic INS/HKL preparation, SHELXT execution, result classification, and optional Olex2 opening to AutoSolve/scripts/auto_shelxt.py.
---

# AutoSolve Skill

## Role

AutoSolve is the post-AutoXDS structure-solution module.

The skill is responsible for judgment:

- decide whether the user means a single dataset, merged HKL, or explicit HKL path
- ask for composition when it is missing
- decide whether to use user-declared SG, XDS-derived SG, no forced SG, or explicit P-1 fallback
- stop when scientific inputs are missing or contradictory
- explain results and next actions

The script is responsible for mechanics:

```text
AutoSolve/scripts/auto_shelxt.py
```

Do not manually redo scriptable work unless the script is broken and the fix is too small to justify a script update.

The previous long-form operating manual is backed up as:

```text
AutoSolve/SKILL.md.bak_scriptfirst_*
```

---

# ⚠️ Pre-requisite: Keep merged-dataset observations unmerged

Before running AutoSolve on **merged** data, verify the XDSCONV step was run
with `MERGE=FALSE`, and that the upstream XSCALE `.ahkl` header also says
`MERGE=FALSE`. This keeps the individual scaled observations from all selected
datasets, allowing SHELX/Olex2 to calculate Rint from their redundancy.

Signs of accidental pre-merging:
- XSCALE `.ahkl` header says `MERGE=TRUE`
- final HKL record count is close to the unique-reflection count
- Olex2/SHELX reports Rint as 0 or nearly 0

Proper combined output retains **individual observations**, not only unique
reflections. Do not run AutoSolve on an already pre-merged HKL when Rint and
redundancy must remain meaningful.

---

---

# Scope

Use this skill for:

- preparing SHELXT `.ins` and matching `.hkl`
- solving one AutoXDS dataset from `diff/p/temp.hkl`
- solving an explicit merged or standalone `.hkl`
- generating `CELL`, fixed `ZERR`, symbolic `SFAC`, `UNIT`, `HKLF`
- validating composition against `AutoSolve/lib/SFAC_UCLA_2022.txt`
- mapping SG number/symbol through `AutoSolve/lib/shelxt_space_groups.json`
- running `shelxt <basename> -s"<SG>"`
- classifying `.res` / `.lxt` / `.lst`
- opening successful `.res` in Olex2 when requested or appropriate

Do not use this skill for:

- XDS, XSCALE, or xdsconv processing
- SHELXL refinement
- Olex2 GUI solving
- atom-type correction, disorder modelling, CIF preparation, or refinement strategy

---

# Required Script Contract

Before doing mechanical work, prefer:

```bash
python3 AutoSolve/scripts/auto_shelxt.py --help
```

Main commands:

```bash
python3 AutoSolve/scripts/auto_shelxt.py prepare --dataset experiment_003 --composition "Au4 C20 H16 N2" --sg 4
python3 AutoSolve/scripts/auto_shelxt.py solve --dataset experiment_003 --composition "Au4 C20 H16 N2" --sg 4 --open-olex2
python3 AutoSolve/scripts/auto_shelxt.py classify --workdir Data/experiment_003/diff/p --basename experiment_003
```

For explicit or merged HKL:

```bash
python3 AutoSolve/scripts/auto_shelxt.py solve --hkl Data/experiment_3/diff/p/3_1_2.hkl --basename 3_1_2 --composition "Au4 C20 H16 N2" --sg 14
```

For dry-run planning:

```bash
python3 AutoSolve/scripts/auto_shelxt.py solve --dataset experiment_003 --composition "Au4 C20 H16 N2" --sg 4 --dry-run
```

---

# Inputs The Agent Must Decide

## Target

- Single dataset: use `--dataset <name>` and script will use `<dataset>/diff/p/temp.hkl`.
- Merged or explicit HKL: use `--hkl <path>` and usually `--basename <hkl-stem>`.
- If multiple dataset folders share a name, stop and ask/clarify.
- Do not guess another HKL when the intended source is missing.

## Composition

Composition is required.

Preferred input is unit-cell contents:

```text
Au4 C20 H16 N2
```

Use:

```bash
--composition "Au4 C20 H16 N2"
```

If the user gives a molecular formula instead, require `Z`:

```bash
--formula "Au2 C10 H8 N" --z 2
```

For batch use, one provided composition may be reused across selected datasets unless the user gives dataset-specific compositions.

Never guess composition from atom labels, filenames, or chemical intuition.

## Cell

Let the script resolve the cell in this priority:

1. `--cell`
2. `summary.txt`
3. `CORRECT.LP`
4. `XDS.INP`

If no cell is available, stop and ask for:

```text
a b c alpha beta gamma
```

## Space Group

Default behavior is to force SHELXT with `-s`.

Priority:

1. user-declared `--sg`
2. `summary.txt` SG
3. `CORRECT.LP`
4. `XDS.INP`

Use `--no-force-sg` only if the user explicitly requests no forced SG.

Use P-1 fallback only if the user explicitly requests it:

```bash
--sg 2
```

Do not run SHELXT without `-s` by default.

---

# What The Script Does

`auto_shelxt.py prepare`:

- locates the working folder and HKL source
- validates required libraries
- prefers `TEMPLATES/template.ins`; falls back to current repo's `TEMPLATES/template.res`
- backs up existing target `.ins` / `.hkl`
- copies HKL to `<basename>.hkl` when needed
- writes a minimal v1 INS containing:
  - `CELL 0.02508 ...`
  - `ZERR 1 0.001 0.001 0.001 0.010 0.010 0.010`
  - symbolic `SFAC`
  - `UNIT`
  - `HKLF 4`
- intentionally omits `LATT` and `SYMM`
- prints the exact SHELXT command

`auto_shelxt.py solve`:

- runs `prepare`
- backs up existing `.res`, `.lxt`, `.lst`
- runs SHELXT
- classifies the result
- opens Olex2 only with `--open-olex2` and only on `solved_success`

`auto_shelxt.py classify`:

- returns exactly one main status:
  - `solved_success`
  - `shelxt_no_solution`
  - `shelxt_failed`
  - `input_preparation_failed`

---

# Result Handling

Report at least:

```text
Dataset / basename:
Working folder:
HKL:
INS:
Cell and source:
Composition:
SFAC / UNIT:
Space group source:
SHELXT command:
Result:
RES:
Log:
Olex2:
Notes:
```

If `input_preparation_failed`, report the reason and do not run SHELXT.

If `shelxt_failed` or `shelxt_no_solution`, do not open Olex2 automatically.

If `solved_success`, opening Olex2 is allowed, but still report the `.res` path.

---

# File Safety

- Never edit files under `AutoSolve/templates/` or `AutoSolve/lib/` directly.
- Use script backups before overwriting generated files.
- Do not delete source HKL files.
- Do not overwrite `.ins`, `.hkl`, `.res`, `.lxt`, or `.lst` without backup.
- Do not hand-add `LATT`/`SYMM`. The script injects them automatically from
  `AutoSolve/lib/shelx_symops.json` when the target space group is known (required so
  SHELXT `-s` works on freshly xdsconv-converted ED HKL; without LATT/SYMM
  SHELXT bails with "No satisfactory space group found" and writes no `.res`).
- Do not add extra SHELXT options unless the user asks.

---

# When To Improve The Script

If you catch yourself doing the same file edit, parsing step, backup pattern, or result check manually more than once, update `AutoSolve/scripts/auto_shelxt.py` or `scripts/autocrys_common.py` instead of making the SKILL longer.

---

# Known SHELXT Quirk (freshly converted ED HKL + forced -s)

On HKL freshly exported by xdsconv, SHELXT often fails when the space group is
forced with `-s`: it runs dual space, then exits with "No satisfactory space
group found" and writes no `.res`. Root cause observed 2026-09-07: the
generated `.ins` omitted `LATT`/`SYMM`, so SHELXT lacked Laue-group context.

Fix (in script): when the target SG number is known, `render_ins` injects
`LATT` + `SYMM` lines derived from `AutoSolve/lib/shelx_symops.json` (gemmi reference
settings, generated once - no gemmi dependency at runtime). Verified:
`-sCc` / `-sC2_c` / `-sPnma` then solve in a single attempt.

Fallback (still in script): if a forced run fails anyway, `run_shelxt`
automatically re-runs once without `-s` ("priming") and then retries the
forced run; the final `.res` is picked up via the `_a` suffix logic in
`classify()`.
