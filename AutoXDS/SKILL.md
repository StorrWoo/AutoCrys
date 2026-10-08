---
name: autoXDS
description: Script-first XDS processing for AutoCrys datasets. Use this skill to decide dataset selection, declared cell/SG intent, optional pre-merge clustering, merge intent, and user confirmations, then delegate deterministic XDS.INP edits, archiving, XDS/xdsconv/xscale execution, parsing, summary writing, clustering, and merge preparation to AutoXDS/scripts/auto_xds.py.
---

# AutoXDS Skill

## Role

AutoXDS is the data-processing module for `diff/p/XDS.INP` datasets.

The skill is responsible for judgment:

- decide the search root and selected datasets
- decide whether to run optional pre-merge clustering
- ask before merging mismatched space groups
- decide whether declared cell/SG is scientifically intended
- decide whether to process all datasets or only named datasets
- stop when inputs are missing or ambiguous
- explain failures and suggest next steps

The script is responsible for mechanics:

```text
AutoXDS/scripts/auto_xds.py
```

Do not manually redo scriptable work unless the script is broken and the fix is smaller than updating the script.

Previous long-form operating manuals are backed up as:

```text
AutoXDS/SKILL.md.bak_scriptfirst_*
AutoXDS/SKILL_backup.md
AutoXDS/SKILL_history_*.md
```

Use the generic AutoXDS script for all datasets. Local experiment-specific helpers are not distributed.

---

# Lessons Learned

## 1. NAME_TEMPLATE path

The standard XDS.INP template uses:

```text
NAME_TEMPLATE_OF_DATA_FRAMES= ../frame_????.tif   tiff
```

From `diff/p/`, `../` resolves to `diff/`, which is where the frame `.tif` files normally live. Do not override `--name-template` unless you have verified that frames are elsewhere.

## 2. WSL processor setting

Some WSL runs fail with:

```text
!!! ERROR !!! CANNOT OPEN OR READ FILE bin1_01.tmp
```

If this appears, set `MAXIMUM_NUMBER_OF_PROCESSORS=1` in `XDS.INP` before running.

## 3. XSCALE.INP order

XSCALE is order-sensitive. `INPUT_FILE=` lines must come before `UNIT_CELL_CONSTANTS` and `SPACE_GROUP_NUMBER`, or XSCALE can fail with:

```text
!!! ERROR !!! MISPLACED PARAMETER
```

`set_repeated_keyword_block()` in `scripts/autocrys_common.py` preserves the original keyword-block position.

## 4. Preserve observations through XSCALE and XDSCONV

When scaling several datasets and converting the scaled `.ahkl` to SHELX
`.hkl`, both `XSCALE.INP` and `XDSCONV.INP` must include:

```text
MERGE= FALSE
```

This retains individual observations so SHELX/Olex2 can calculate a meaningful
Rint. Once observations have been pre-merged into unique reflections, XDSCONV
cannot reconstruct the lost redundancy and downstream Rint becomes 0.

## 5. REIDX

The current XDS build may reject `REIDX` as obsolete. If reindexing is needed, write a transformed HKL file directly with a script instead of relying on `REIDX`.

---

# Scope

Use this skill for:

- finding datasets with `diff/p/XDS.INP`
- safe fresh XDS reruns
- `NAME_TEMPLATE_OF_DATA_FRAMES` normalization
- declared `SPACE_GROUP_NUMBER` / `UNIT_CELL_CONSTANTS`
- recovery from the defined `INSUFFICIENT PERCENTAGE (< 50%)` case
- CORRECT.LP-based resolution cutoff pass
- xdsconv export to `temp.hkl`
- writing `summary.txt`
- optional HCA clustering before merge
- selected-dataset merging with XSCALE and xdsconv

Do not use this skill for:

- SHELXT/SHELXL structure solution or refinement
- Olex2 GUI operations
- manual crystallographic interpretation beyond the explicit workflow
- new recovery strategies not encoded in the skill/script

---

# Required Script Contract

Start with help or dry-run when unsure:

```bash
python3 AutoXDS/scripts/auto_xds.py --help
python3 AutoXDS/scripts/auto_xds.py list --root Data --json
```

Process one dataset:

```bash
python3 AutoXDS/scripts/auto_xds.py process --root Data --dataset experiment_003
```

Process every dataset under a root:

```bash
python3 AutoXDS/scripts/auto_xds.py process --root Data/example_batch --all --replace-summary
```

Plan without changing files:

```bash
python3 AutoXDS/scripts/auto_xds.py process --root Data/example_batch --all --dry-run --json
```

Declare cell and SG before processing:

```bash
python3 AutoXDS/scripts/auto_xds.py process --root Data/example_batch --dataset experiment_11 --declared-sg 15 --declared-cell "16 28 21 90 96 90" --resolution "20 0.8"
```

Only edit declared cell/SG, without running XDS:

```bash
python3 AutoXDS/scripts/auto_xds.py set-cell-sg --root Data/example_batch --dataset experiment_11 --declared-sg 15 --declared-cell "16 28 21 90 96 90"
```

Run pre-merge clustering only:

```bash
python3 AutoXDS/scripts/auto_xds.py cluster --root Data --dataset sample1_2 --dataset sample2_1 --dataset sample2_2 --cluster-method both --json
```

Save dendrogram SVGs:

```bash
python3 AutoXDS/scripts/auto_xds.py cluster --root Data --dataset sample1_2 --dataset sample2_1 --dataset sample2_2 --cluster-method both --dendrogram cluster.svg --json
```

Use XSCALE.LP CC(I)/CC1 correlations, matching the reference cluster script style:

```bash
python3 AutoXDS/scripts/auto_xds.py cluster --root Data/example_batch --dataset experiment_10 --dataset experiment_11 --dataset experiment_12 --cluster-method cc1 --cc-source xscale-lp --xscale-lp Data/example_batch/xds_info/XSCALE.LP --dendrogram example_cc1.svg --json
```

Prepare merge inputs:

```bash
python3 AutoXDS/scripts/auto_xds.py merge --root Data --dataset experiment_3 --dataset experiment_1 --dataset experiment_2
```

Prepare merge inputs with clustering first:

```bash
python3 AutoXDS/scripts/auto_xds.py merge --root Data --dataset experiment_3 --dataset experiment_1 --dataset experiment_2 --cluster both --dendrogram merge_cluster.svg --dry-run --json
```

Prepare and run merge:

```bash
python3 AutoXDS/scripts/auto_xds.py merge --root Data --dataset experiment_3 --dataset experiment_1 --dataset experiment_2 --run
```

---

# Inputs The Agent Must Decide

## Search Root

Use the user's explicit path when provided.

If no path is provided, use the current project root or `Data/`, depending on context.

The script detects valid datasets by finding:

```text
<dataset>/diff/p/XDS.INP
```

## Dataset Set

- Use `--dataset NAME` for named datasets; repeat it for multiple datasets.
- Use `--all` only when the user clearly wants all valid datasets under the root.
- Do not process archived folders under `autoXDS_old_outputs_*`.

## Declared Cell / SG

Use declared cell/SG only when the user explicitly provides them or the task clearly says to rerun with a declared setting.

Use:

```bash
--declared-sg <number>
--declared-cell "a b c alpha beta gamma"
```

Do not invent unit cells or space groups.

When no cell/SG is supplied from the UI, processing uses:

```bash
--clear-declared-cell-sg --preserve-original-cell-sg-on-first-run
```

This preserves the imported `XDS.INP` cell/SG for the dataset's first AutoXDS run.
Later runs clear stale declared constraints, so deleting a previously entered cell returns to unspecified mode.

## Resolution

Let the script parse `CORRECT.LP` and decide the standard cutoff unless the user explicitly provides a resolution.

**Default resolution policy (重要):** the default merge/process resolution is **1.0 Å**, and 1.0 Å is the *critical point*.

- Keep 1.0 Å as the default high-resolution limit.
- When lowering the resolution (cutting at a coarser shell, e.g. to improve R), **do not go past (coarser than) 1.0 Å** unless the user explicitly declares a different limit for that job.
- Do not silently coarsen the cutoff to 1.2/1.5 Å "to make R look better". If it looks necessary, stop and ask.

Use:

```bash
--resolution "20 1.0"
```

or:

```bash
--resolution 0.8
```

## Pre-Merge Clustering

Clustering is optional and should happen before merge when the user asks to compare datasets, choose a merge group, or inspect similarity.

In the AutoXDS UI, `HCA Run` is an inspection step only:

- the blank clustering field means `both`
- `UnitCell`, `CC`, and `Both` map to `unit-cell`, `cc1`, and `both`
- HCA displays the dendrogram and lets the user click a cutoff height
- clicking the dendrogram updates the cluster preview only
- HCA must not run XSCALE or XDSCONV by itself

Available methods:

- `unit-cell`: distance from unit-cell parameters:
  `sqrt(da^2 + db^2 + dc^2 + d_sin_alpha^2 + d_sin_beta^2 + d_sin_gamma^2)`
- `cc1`: intensity-based distance:
  `sqrt(1 - CC1_used^2)`, where `CC1_used=max(0, CC1)`. This matches the reference clustering behavior of clipping negative correlations to 0 before distance calculation.
- `both`: run both methods

Use:

```bash
--cluster-method unit-cell
--cluster-method cc1
--cluster-method both
```

or during merge:

```bash
--cluster unit-cell
--cluster cc1
--cluster both
```

Optional HCA parameters:

```bash
--cluster-linkage average
--cluster-linkage single
--cluster-linkage complete
--cluster-threshold <distance>
--max-reflections <N>
--dendrogram <svg-path>
```

There is no arbitrary minimum-common-reflection cutoff. Small datasets are
accepted; direct `raw` Pearson correlation only requires the mathematical
minimum of two common, non-constant intensities. The legacy
`--min-common-reflections` option is accepted for compatibility but ignored.

CC1 source options:

```bash
--cc-source raw
--cc-source xscale-lp --xscale-lp <XSCALE.LP>
```

`raw` computes Pearson correlation directly from common reflection intensities in each selected `XDS_ASCII.HKL`.

`xscale-lp` parses `CORRELATIONS BETWEEN INPUT DATA SETS AFTER CORRECTIONS` from an existing `XSCALE.LP`, matching the reference implementation more closely.

Do not let clustering silently decide the merge set unless the user has explicitly asked for automatic selection.

## Merging

**Default: do NOT pre-merge (不要预合并).** Feed the individual, per-dataset `XDS_ASCII.HKL` files directly into XSCALE as separate `INPUT_FILE=` entries, and explicitly use `MERGE=FALSE` in both XSCALE and XDSCONV. Never feed an already-merged file (e.g. a previous merged `.ahkl` / `.hkl`) into a merge, and never average observations before SHELX/Olex2 — that collapses the redundancy and makes Rint ~ 0, which destroys the quality metrics. Only pre-merge if the user explicitly asks for it.

The script already follows this: `auto_xds.py merge` copies each selected dataset's own `XDS_ASCII.HKL` into the best dataset workdir (as `<id>.HKL`) and lists them all as separate `INPUT_FILE` lines. Keep it that way.

Before merge, the script reads `summary.txt`, optionally runs clustering, sorts by ascending R-factor, copies selected `XDS_ASCII.HKL` files into the best dataset workdir, prepares `XSCALE.INP`, and optionally runs `xscale` plus `xdsconv`.

**Pass the right summary and root for nested data (踩坑):**

- `merge`/`process` with `--dataset NAME` resolve names via `find_dataset`, which only looks at `<root>/<NAME>` and `<root>/Data/<NAME>` — it does **not** recurse. For data nested like `Data/YY/CuBrBDT/20260723/experiment_*`, point `--root` at the date folder (`Data/YY/CuBrBDT/20260723`), or it will report `dataset not found`.
- Always pass the matching `--summary <file>` explicitly when you ran a declared run into a custom summary; otherwise merge reads the default `summary.txt` and can stop on a stale SG mismatch.

In the AutoXDS UI, `Merge & Conv` uses the active dendrogram method and the last clicked cutoff. It processes only clusters with at least two datasets. Single-dataset clusters are reported as skipped and are not merged.

If selected datasets have different SG values, stop and ask before using:

```bash
--allow-sg-mismatch
```

Do not guess a merge set.

---

# What The Script Does

`auto_xds.py list`:

- finds valid dataset folders
- prints dataset names and working directories

`auto_xds.py process`:

- optionally archives old XDS outputs into `autoXDS_old_outputs_YYYYMMDD_HHMMSS/`
- backs up `XDS.INP`
- resets standard JOB lines
- normalizes `NAME_TEMPLATE_OF_DATA_FRAMES`
- applies declared cell/SG/resolution when provided
- runs `xds`
- handles the defined `<50%` recovery case
- applies standard CORRECT.LP resolution cutoff pass unless skipped
- prepares `XDSCONV.INP`
- runs `xdsconv`
- parses cell, SG, ISa, R-factor, completeness
- writes or appends `summary.txt`

`auto_xds.py cluster`:

- reads selected rows from `summary.txt`
- computes pairwise unit-cell distance and/or CC1 distance
- can compute CC1 either from raw common HKL intensities or from existing `XSCALE.LP` correlations
- performs hierarchical clustering with average/single/complete linkage
- optionally reports flat clusters at a user-provided distance threshold
- optionally writes dendrogram SVG output
- does not write files

`auto_xds.py set-cell-sg`:

- backs up `XDS.INP`
- edits only declared cell/SG/resolution
- does not run XDS

`auto_xds.py merge`:

- reads `summary.txt`
- optionally runs clustering and includes the result in output
- checks SG consistency
- sorts selected datasets by R-factor
- averages unit cells
- copies `XDS_ASCII.HKL` as merge inputs
- writes `XSCALE.INP`
- with `--run`, runs `xscale`, prepares `XDSCONV.INP`, and runs `xdsconv`

---

# Summary Contract

The canonical summary columns are:

```text
Dataset	FirstRun	Recovery	Resolution	Xdsconv	Final	Cell	SG	ISa	Rfactor	Completeness	Output	Notes
```

Unavailable values must be `NA`.

Do not invent missing statistics.

---

# File Safety

- Never edit template originals in `TEMPLATES/`.
- Never edit library files in `AutoSolve/lib/`.
- Do not delete raw frames or source HKL files.
- Archive old XDS outputs only before fresh reruns.
- Keep `XDS.INP`; back it up before edits.
- Do not add unapproved recovery strategies.

---

# Reporting

For processing, report:

```text
Dataset:
FirstRun:
Recovery:
Resolution:
Xdsconv:
Final:
Cell:
SG:
ISa:
Rfactor:
Completeness:
Output:
summary.txt:
Notes:
```

For clustering, report:

```text
Datasets:
Method:
Pairwise distances:
CC1 and common reflection counts, for cc1 method:
HCA linkage:
Merge steps:
Clusters at threshold, if threshold was supplied:
Dendrogram SVG:
Notes:
```

For merge, report:

```text
Merged datasets:
Pre-merge clustering:
Merge order:
Best dataset:
Average cell:
SG:
Working folder:
XSCALE input:
AHKL:
Final HKL:
Status:
```

---

# When To Improve The Script

If you notice a repeated manual edit, parser, backup, archive, summary operation, or clustering calculation, update `AutoXDS/scripts/auto_xds.py` or `scripts/autocrys_common.py` instead of growing this SKILL again.
