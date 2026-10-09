# AutoCrys v1.0

**Author: Shitao Wu · Affiliation: ShanghaiTech**

**Viewing and downloading are permitted. Use requires prior written permission.** See [NOTICE.md](NOTICE.md). Write access is reserved for the owner and authorized collaborators.

Download [v1.0](https://github.com/StorrWoo/AutoCrys/releases/tag/v1.0) and follow [GETTING_STARTED.md](GETTING_STARTED.md) to install. Release notes: [RELEASE_NOTES.md](RELEASE_NOTES.md).

## Git distribution scope

This Git edition contains source code and generic configuration examples only.
`Data/`, `Demo/`, runtime logs, personal settings, machine deployment records and
third-party executables are not distributed. Create `Data/` locally or select
another local data directory. Download SHELXT/SHELXL, XDS and optional DIALS/Olex2
from their official providers yourself; see [GETTING_STARTED.md](GETTING_STARTED.md#8-配置晶体学外部程序).
No open-source license is granted; see [NOTICE.md](NOTICE.md) for the permission notice. See [RELEASE_AUDIT.md](RELEASE_AUDIT.md)
for the scope and limitations of this edition's acceptance checks.

For a clean Windows + WSL/Linux deployment, start with
[GETTING_STARTED.md](GETTING_STARTED.md). If an AI/coding Agent will perform
the installation, give it [DEPLOYMENT_AGENT.md](DEPLOYMENT_AGENT.md) and ask it
to follow every gate in order.

For an existing deployment, run the read-only updater audit first and then use
the detailed operating manual:

```bash
python3 scripts/update_self_check.py
```

- [Existing-system update and self-check](GETTING_STARTED.md#2-已部署系统先自检再原位更新)
- [Detailed user manual](USER_MANUAL.md)

HOW TO USE:

conda run -n AutoCrys autor3d-ui

AutoCrys is an automated crystallographic workflow workspace for processing 3D ED / electron diffraction datasets and preparing initial structure solutions.

The project is divided into five workflow modules plus the integrated UI:

1. **AutoXDS** — data processing, XDS execution, resolution cutoff optimization, xdsconv export, summary generation, and dataset merging.
2. **AutoDials** — an alternative DIALS backend: cRED2 TIFF → miniCBF conversion, DIALS import/find_spots/index/refine/integrate, MTZ/SHELX export, multi-dataset HCA/cosym/scale/merge, and native-geometry handoff to AutoR3D.
3. **AutoR3D** — cRED2 / 3DED peak search, XDS- or DIALS-derived rotation axis and crystal orientation, reciprocal-space reconstruction, hkl-indexed 2D slices (hk0/h0l/0kl), and 3D point-cloud QC.
4. **AutoSolve** — SHELXT input preparation, structure solution, result classification, and opening successful solutions in Olex2.
5. **AutoRefine** — source-preserving SHELXL refinement and model diagnosis.
6. **UI** — the integrated desktop interface, console, and AI assistant.

This README is intended for agents operating inside the AutoCrys workspace.

---

# Project structure

The expected AutoCrys directory layout is:

```text
AutoCrys/
├── README.md
├── main.py
├── requirements.txt
├── pyproject.toml
├── config/                  # AI/runtime config, Conda environment, shared helpers
├── UI/                      # Tk UI and AI assistant
├── AutoXDS/                 # XDS scripts, templates, skill, and tests
├── AutoDials/               # DIALS backend: cRED2 conversion, pipeline, HCA/merge, bridges
├── AutoR3D/                 # Reconstruction engine, skill, and tests
├── AutoSolve/               # SHELXT/SHELXL scripts, libraries, tools, templates
├── AutoRefine/              # Refinement skill, rules, script, and tests
└── scripts/                 # Deployment checks
```

---

# High-level workflow

The full AutoCrys workflow is:

```text
Raw / converted diffraction data
↓
AutoXDS
    locate experiment_XXX/diff/p
    run xds
    recover defined <50% failure mode
    optimize resolution cutoff from CORRECT.LP
    run xdsconv
    generate temp.hkl
    write summary.txt
    optionally merge datasets with xscale
↓
AutoDials (optional alternative to AutoXDS / merge)
    convert cRED2 TIFF to miniCBF with lossless verification
    dials.import / find_spots / index / refine / integrate
    export MTZ and SHELX HKL/INS
    HCA and cosym/scale/merge across datasets
↓
AutoR3D
    read cRED2 geometry and diff/frame_*.tif
    use XDS axis/orientation or native DIALS geometry when available
    reconstruct reciprocal space, QC, 3D panel and hkl slices
↓
AutoSolve
    select temp.hkl or merged .hkl
    copy template.ins
    generate basename.ins
    copy HKL to basename.hkl
    write CELL / ZERR / SFAC / UNIT / HKLF
    force SHELXT space group using -s
    run shelxt
    classify result
    open successful .res in Olex2
↓
AutoRefine / SHELXL
    inspect the model and matching reflections
    refine in a new isolated output directory
    diagnose metrics, ADPs and optional SiO2 geometry
```

---

# Integrated UI

The unified desktop UI is launched from:

```bash
python main.py
```

The window title is **AutoCrys**. It hosts AutoXDS, AutoDials, AutoR3D, and AutoSolve tabs, with a persistent right-side console for command output and reconstruction logs.

## AutoDials backend

The AutoDials tab runs DIALS as an external runtime so the AutoCrys process never
needs to import cctbx:

- Point **DIALS Python** at a DIALS interpreter. The default lookup checks
  `DIALS_PYTHON`, then `C:\dials\python.exe`, then Linux/conda locations such as
  `~/miniconda3/envs/dials/bin/python` and `/mnt/c/dials/python.exe`.
- **Check DIALS runtime** reports the DIALS/dxtbx versions and confirms required
  packages (`numpy`, `Pillow`, `scipy`) before any work starts.
- **Process & Convert selected** writes each run to
  `<dataset>/AutoDials/run_<timestamp>_<id>/`, converting cRED2 TIFFs to generic
  miniCBF with a per-frame lossless round-trip check, then running
  import/find_spots/index/refine/integrate and exporting `.mtz`, `.hkl`, `.ins`.
- The run list under each dataset shows completeness, final cell, space group,
  DIALS version, and time. HCA and Merge use the **checked** runs, so specific
  historical results can be chosen instead of always taking the newest one.
- Results are handed to AutoR3D as native DIALS geometry (`autocrys.dials.geometry.v1`)
  and to AutoSolve as SHELX HKL/INS with cell and space group metadata.
- Single-dataset output is unscaled profile intensity; AutoSolve warns before
  accepting it. Run HCA/Merge to obtain scaled data.

AutoDials writes only inside its own run directories and never modifies source
TIFFs or XDS outputs.

The AutoSolve page includes a lightweight **Structure** subtab. It reads the
selected or newly solved SHELX `.res`/`.ins`, converts fractional coordinates
through the `CELL` matrix, colours atoms by element, infers bonds from covalent
radii, and supports drag rotation, right-drag panning, wheel zoom, atom hover
details, a unit-cell toggle, exact `a`/`b`/`c` views, and incremental `Grow`
through `SYMM`/`LATT` symmetry and neighbouring cell translations. Each click
grows the configured number of shells (two by default); `Reset` removes all
grown copies and restores the original file model without changing the camera.
The AutoSolve **Open Olex2** button opens exactly the
`.res` currently displayed in the Structure subtab. When Olex2 is a native
Windows executable, AutoCrys converts the WSL path to a Windows UNC path before
launching it. The viewer is controlled in
`config/ai_assistant.json` (the file also holds general UI settings):

```json
"structure_viewer": {
  "enabled": true,
  "show_unit_cell": true,
  "bond_tolerance_angstrom": 0.45,
  "grow_steps_per_click": 2
}
```

The viewer is deliberately an inspection tool: it starts from the coordinates
explicitly stored in the SHELX file and can display symmetry/translation mates
with `Grow`, but it does not edit the crystallographic model or replace a full
crystallographic modeller.

## Evidence-driven AI Assistant

The installed launchers provide config-driven, forced, and diagnostic variants:

```bash
autocrys       # Compact Console; AI follows assistant.enabled in config.json
autocrys-ai    # Compact Console; AI is forced on
autocrys-base  # Compact Console; AI is forced off
autocrys-main  # Full unfiltered Console; AI follows config.json
```

`autocrys` and `python main.py` follow `assistant.enabled` in
`config/ai_assistant.json`; `python main.py ai`
and `autocrys-ai` always enable AI. When enabled, the main notebook adds an
`Assistant` tab for chat, suggestions, diagnostics, confirmations, and the
`Send`/`AI建议` controls. The right side remains a process-only Console.

The compact Console keeps operation starts, results, warnings, and errors while
hiding command paths and routine subprocess chatter. Use `autocrys-main` when the
complete command/output stream is needed for debugging.

The assistant records every completed operation together with its tab, selected
datasets, command, operation-specific logs, structured result, rule diagnoses,
and current UI state. `AI建议` reviews that last operation first, then inspects
the relevant artifacts before asking an LLM to explain anything:

Agent-executed AutoXDS, AutoSolve, and AutoR3D tasks return a small structured
completion block. AutoCrys removes that block from chat, switches to the matching
workflow tab, and renders the result in its normal result area.

- AutoXDS: `summary.txt`, `CORRECT.LP`, `XDS.INP`, and reflection outputs
- AutoR3D: `qc_report.json`, `frame_geometry.json`, peak candidates, and the 3D panel
- AutoSolve: `.res` plus `.lxt`/`.lst`, including atom-model classification

HCA suggestions use the actual method, pairwise distance/CC/common-reflection
statistics, selected cutoff, and current groups. XDS, merge, AutoR3D, SHELXT,
and SHELXL use their corresponding result and artifact evidence. Suggestions no
longer use 300 characters as a hard cut: 300 is the normal response target,
ordinary replies have a 1200-character safety ceiling, explicit detailed
requests allow up to 4000 characters, and safety truncation prefers a complete
sentence boundary. In `auto`
mode the backend order is: a detected Ollama model, the named cloud profile selected
by `llm.cloud.active_profile`, then deterministic offline analysis. Cloud profiles
read `api_key` directly from the machine-local `config/ai_assistant.json`;
`api_key_env` remains an optional fallback. An explicit `local`, `cloud`, or
`offline` mode overrides auto detection.

Natural-language skills cover AutoXDS, AutoR3D, HCA/merge, and AutoSolve. Context
may fill parameters but never counts as execution intent. Commands with missing
required values are refused, and executable actions are shown for confirmation.
Ordinary chat starts with a compact AutoCrys/crystallography prompt. When exact
workflow knowledge is needed, the model requests one or more whitelisted SKILL
modules; the app loads them from its in-memory cache and retries the question once.
This app-managed skill path applies to direct cloud/local LLM modes. In `agent`
mode, OpenClaw owns skill discovery and tool execution; AutoCrys sends only UI
context, evidence rules, response preferences, and the structured completion
contract used to return executed results to the matching workflow tab.

Run the assistant regression tests with real repository data:

```bash
python -m unittest discover -s UI/tests -p "test_*.py" -v
RUN_OLLAMA_TESTS=1 python -m unittest discover -s UI/tests -p "test_*.py" -v
python UI/tests/ai_assistant_diagnostics.py
```

---

# Module responsibilities

## AutoXDS

Located at:

```text
AutoCrys/AutoXDS/SKILL.md
```

AutoXDS handles:

- locating valid datasets
- entering `experiment_XXX/diff/p`
- fixing `NAME_TEMPLATE_OF_DATA_FRAMES`
- running `xds`
- detecting `XDS_ASCII.HKL`
- recovering from the defined `INSUFFICIENT PERCENTAGE (< 50%)` failure mode
- optimizing resolution cutoff from `CORRECT.LP`
- running `xdsconv`
- generating `temp.hkl`
- writing `summary.txt`
- merging selected datasets using `xscale`
- converting merged `.ahkl` to SHELX `.hkl`
- preserving individual observations with `MERGE=FALSE` in both XSCALE and
  XDSCONV, so SHELX/Olex2 receives redundancy and can calculate a meaningful
  `Rint` instead of a pre-merged zero

AutoXDS must not perform structure solution or refinement.

---

## AutoDials

Located at:

```text
AutoCrys/AutoDials/
```

AutoDials is an alternative, independent processing backend that runs DIALS in a
separate interpreter. It handles:

- locating cRED2 datasets (`*cRED2*parameters*.txt` plus `diff/frame_*.tif`)
- lossless TIFF → miniCBF conversion with per-frame SHA-256 and pixel verification
- building or borrowing geometry (`XPARM.XDS` / `GXPARM.XDS` geometry only)
- `dials.import` → `find_spots` → `index` → `refine` → `integrate`
- MTZ and SHELX HKLF4/INS export with cell and symmetry cross-checks
- multi-dataset HCA (Niggli G6 unit-cell, or correlation-coefficient on common ASU
  intensities after `dials.cosym`)
- `dials.scale` / `dials.merge` and scaled SHELX export
- native DIALS geometry export for AutoR3D and metadata for AutoSolve

AutoDials must not perform structure solution or refinement. Single-dataset runs
produce unscaled intensities; scaled data requires HCA/merge.

---

## AutoSolve

Located at:

```text
AutoCrys/AutoSolve/SKILL.md
```

AutoSolve handles:

- selecting single-dataset `temp.hkl` or merged `.hkl`
- assigning basename:
  - single dataset: `experiment_XXX`
  - merged dataset: merge order, e.g. `3_1_2`
- copying `AutoSolve/templates/template.res`
- generating `<basename>.ins`
- copying HKL to `<basename>.hkl`
- reading cell parameters from:
  1. `summary.txt`
  2. `CORRECT.LP`
  3. `XDS.INP`
  4. user manual input
- writing fixed wavelength:
  ```text
  0.02508
  ```
- writing fixed ZERR:
  ```text
  ZERR 1 0.001 0.001 0.001 0.010 0.010 0.010
  ```
- parsing user-provided unit-cell contents into `SFAC` and `UNIT`
- validating elements using `AutoSolve/lib/SFAC_UCLA_2022.txt`
- mapping space group with `AutoSolve/lib/shelxt_space_groups.json`
- running:
  ```bash
  shelxt <basename> -s"<SG>"
  ```
- checking `.res` and `.lxt`
- using `.lst` only as fallback log
- opening successful `.res` with Olex2

AutoSolve must not run XDS, XSCALE, xdsconv, SHELXL, or GUI-based Olex2 solving.

---

# Data folder convention

Datasets normally live under:

```text
AutoCrys/Data/
```

A standard dataset looks like:

```text
experiment_XXX/
├── Continuous 3D ED (cRED2) parameters.txt
├── diff/
│   ├── frame_0001.tif
│   ├── frame_0002.tif
│   └── p/
│       ├── XDS.INP
│       ├── CORRECT.LP
│       ├── XDS_ASCII.HKL
│       ├── temp.hkl
│       └── ...
```

AutoXDS defaults to `../frame_????.tif tiff` from `diff/p`, and AutoR3D
defaults to `diff/frame_*.tif`. Legacy datasets that keep frames in `raw/`
must explicitly use `--frames raw`.

The active XDS and SHELXT working directory is normally:

```text
experiment_XXX/diff/p
```

All command-line operations for a dataset should be run from this folder unless explicitly stated otherwise.

---

# Shared libraries

## Space-group library

Located at:

```text
AutoCrys/AutoSolve/lib/shelxt_space_groups.json
```

Purpose:

```text
XDS SPACE_GROUP_NUMBER or user-provided space-group symbol
→ SHELXT -s compatible space-group string
```

Expected JSON structure:

```json
{
  "by_number": {
    "1": "P1",
    "2": "P-1",
    "4": "P2(1)",
    "14": "P2(1)_c"
  },
  "aliases": {
    "P21": "P2(1)",
    "P21/c": "P2(1)_c",
    "P2(1)/c": "P2(1)_c"
  }
}
```

Rules:

- If the user provides a space-group symbol, check `aliases`.
- If the user provides a space-group number, check `by_number`.
- If the user does not provide a space group, AutoSolve should use the XDS-derived `SPACE_GROUP_NUMBER`.
- Do not run SHELXT without `-s` by default.
- Do not default to `P-1` unless the user explicitly requests it.

---

## SFAC library

Located at:

```text
AutoCrys/AutoSolve/lib/SFAC_UCLA_2022.txt
```

Purpose:

- validate element or ion symbols in user-provided composition
- support future full scattering-factor coefficient generation

Version 1 rule:

```text
Use SFAC_UCLA_2022.txt only for validation.
Do not write full coefficient SFAC lines.
Write symbolic SFAC lines instead.
```

Example:

User input:

```text
Au4 C20 H16 N2
```

Generated SHELX lines:

```text
SFAC Au C H N
UNIT 4 20 16 2
```

---

# Templates

Templates are owned by the module that consumes them:

```text
AutoCrys/AutoXDS/templates/
AutoCrys/AutoSolve/templates/
```

Expected files:

```text
XDSCONV.INP
XSCALE.INP
template.ins
```

Rules:

- Never edit template originals directly.
- Always copy a template into the active working directory before editing.
- If the destination file already exists, back it up before overwriting.

---

# Required commands

The following commands should be available from the relevant working environment.

## XDS tools

Used by AutoXDS:

```bash
xds
xdsconv
xscale
```

## SHELXT

Used by AutoSolve:

```bash
shelxt <basename>
shelxt <basename> -s"<SG>"
```

The preferred AutoSolve command is:

```bash
shelxt <basename> -s"<SG>"
```

where `<SG>` is taken from user input or XDS-derived space-group mapping.

## Olex2

Used only to open successful `.res` files:

```bash
"/mnt/c/Program Files/Olex2-1.5/olex2.exe" "<path-to-res>"
```

Olex2 must not be used for automated GUI clicking.

---

# File safety rules

These rules apply to all AutoCrys modules.

## Never edit originals directly

Do not directly edit files in:

```text
AutoCrys/AutoXDS/templates/
AutoCrys/AutoSolve/templates/
AutoCrys/AutoSolve/lib/
```

Copy required files into the active working directory first.

## Always back up before overwriting

Before overwriting existing working files, create backups.

Recommended naming:

```text
<filename>.bak_<purpose>_autoXDS
<filename>.bak_autoSolve
```

If a backup already exists, append:

```text
_001
_002
_003
```

Never overwrite existing backups.

## Do not delete original outputs

Do not delete original:

```text
XDS_ASCII.HKL
temp.hkl
merged .hkl files
CORRECT.LP
XDS.INP
raw frames
```

For fresh reruns, archive old outputs into a timestamped folder if needed.

Example:

```text
autoXDS_old_outputs_YYYYMMDD_HHMMSS/
```

---

# AutoXDS result summary

AutoXDS writes:

```text
summary.txt
```

Preferred columns:

```text
Dataset	FirstRun	Recovery	Resolution	Xdsconv	Final	Cell	SG	ISa	Rfactor	Completeness	Output	Notes
```

AutoSolve uses this file to read:

- dataset name
- cell
- space group
- output HKL path
- processing status

If a value is unavailable, write:

```text
NA
```

Do not invent missing values.

---

# AutoSolve composition rule

AutoSolve requires composition.

Preferred input is unit-cell contents:

```text
Au4 C20 H16 N2
```

This directly becomes:

```text
SFAC Au C H N
UNIT 4 20 16 2
```

If the user provides molecular formula instead of unit-cell contents, the user must also provide `Z`.

Example:

```text
formula = Au2 C10 H8 N
Z = 2
```

AutoSolve converts it to:

```text
Au4 C20 H16 N2
```

For batch AutoSolve runs, if the user provides one composition, reuse it for all selected datasets unless the user provides dataset-specific compositions.

Do not guess composition.

---

# AutoSolve result classification

AutoSolve must classify results into exactly one of these statuses:

## solved_success

Use when:

```text
<basename>.res exists and is non-empty
<basename>.lxt exists and is non-empty, or <basename>.lst exists and is non-empty as fallback
<basename>.res contains at least one atom line before HKLF
```

If `solved_success`, open the `.res` file in Olex2.

## shelxt_no_solution

Use when:

```text
.res and log exist
but .res contains no clear atom model before HKLF
```

Do not open Olex2 automatically.

## shelxt_failed

Use when:

```text
.res is missing or empty
or both .lxt and .lst are missing or empty
```

Do not open Olex2 automatically.

## input_preparation_failed

Use when required input preparation fails.

Examples:

```text
missing_hkl
missing_cell
missing_composition
invalid_element_or_sfac_symbol
missing_template_ins
missing_shelxt_space_groups_json
missing_sfac_library
space_group_mapping_failed
missing_or_unmapped_xds_space_group
invalid_unit_cell_format
invalid_composition_format
```

Do not run SHELXT if input preparation fails.

---

# Agent operating rules

## General

- Read the relevant module skill before acting.
- For XDS processing, follow `AutoXDS/SKILL.md`.
- For SHELXT solving, follow `AutoSolve/SKILL.md`.
- Do not mix responsibilities across modules.

## Do not guess

Never guess:

- composition
- cell parameters
- space group if no valid mapping exists
- missing file paths
- success status without checking output files

## Prefer explicit user intent

If the user says “single dataset”, use `temp.hkl`.

If the user says “merged data” or gives a merged filename, use the merged `.hkl`.

If the user provides a space group, prefer it over XDS-derived SG.

If the user provides composition once for a batch, reuse it.

---

# Common command examples

## Solve a single dataset

Working folder:

```text
AutoCrys/Data/experiment_XXX/diff/p
```

Files:

```text
experiment_XXX.ins
experiment_XXX.hkl
```

Command:

```bash
shelxt experiment_XXX -s"P2(1)"
```

Expected outputs:

```text
experiment_XXX.res
experiment_XXX.lxt
```

---

## Solve merged data

Working folder:

```text
AutoCrys/Data/experiment_XXX/diff/p
```

Files:

```text
3_1_2.ins
3_1_2.hkl
```

Command:

```bash
shelxt 3_1_2 -s"P2(1)_c"
```

Expected outputs:

```text
3_1_2.res
3_1_2.lxt
```

---

# Milestones

## Milestone 1: AutoXDS

Completed scope:

- XDS run
- defined `<50%` recovery
- resolution cutoff optimization
- xdsconv export
- summary generation
- selected-dataset merging

## Milestone 2: AutoSolve

Completed design scope:

- SHELXT input generation
- `CELL`, `ZERR`, `SFAC`, `UNIT`, `HKLF`
- XDS-derived space-group forcing through `-s`
- SHELXT execution
- `.res` / `.lxt` result classification
- Olex2 opening for successful solutions

## Milestone 3: AutoDials

Implemented scope:

- Fifth workflow module integrated into the main UI
- cRED2 TIFF → miniCBF conversion with lossless verification
- full DIALS single-dataset pipeline and MTZ/SHELX export
- HCA, cosym alignment, scaling and merging across explicitly chosen runs
- native DIALS geometry handoff to AutoR3D and metadata handoff to AutoSolve
- Windows DIALS and Linux/conda DIALS runtime support with pre-run probing
- run completeness gating and per-run DIALS version/platform recording

## Milestone 4: AutoRefine

Implemented scope:

- source-preserving, isolated SHELXL refinement
- model, metric and ADP inspection
- generic and zeolite-SiO2 diagnostic profiles
- dataset/model alias resolution with ambiguity rejection
- JSON output for UI/agent evidence

Manual atom-type correction, restraints, disorder modelling and final CIF
preparation remain expert work in a full crystallographic modeller such as
Olex2.
