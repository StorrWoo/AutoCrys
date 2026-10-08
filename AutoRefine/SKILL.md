---
name: autorefine
description: Script-first, source-preserving SHELXL refinement and model diagnosis for AutoCrys. Use when an Agent must inspect or refine .res/.ins plus .hkl data, interpret R1/wR2/GooF/shift/difference peaks/ADPs, or validate and improve zeolite and SiO2 framework atom assignments, coordination, bond lengths, angles, and bond-valence sums.
---

# AutoRefine

## Role

Use scientific judgment to choose the next refinement action. Delegate file discovery,
SHELXL execution, metric parsing, and SiO2 geometry checks to:

```bash
python3 AutoRefine/scripts/autorefine.py
```

Never overwrite source `.res`, `.ins`, or `.hkl` files. Every SHELXL run must use a
new isolated output directory. Do not relabel, add, delete, split, restrain, omit a
reflection, or change occupancy automatically from a single heuristic.

## Start Here

Locate and inspect a dataset alias or explicit model:

```bash
python3 AutoRefine/scripts/autorefine.py locate sample3_1_a --json
python3 AutoRefine/scripts/autorefine.py inspect --target sample3_1_a --profile zeolite-sio2 --json
python3 AutoRefine/scripts/autorefine.py inspect --res /path/model.res --hkl /path/model.hkl --profile zeolite-sio2 --json
```

Run one short, isolated refinement stage:

```bash
python3 AutoRefine/scripts/autorefine.py refine \
  --target sample3_1_a --profile zeolite-sio2 --cycles 3 --peaks 20 --json
```

Continue from the returned isolated `.res`, not from the original:

```bash
python3 AutoRefine/scripts/autorefine.py refine \
  --res /path/previous_run/model.res --hkl /path/previous_run/model.hkl \
  --profile zeolite-sio2 --cycles 3 --peaks 20 --json
```

Use the AutoCrys WSL Python/runtime. SHELXL defaults to `AutoSolve/tools/shelxl`. Pass
`--shelxl-bin` only when intentionally testing another binary.

## Required Workflow

1. Resolve the exact `.res` and matching `.hkl`; report any alias resolution.
2. Run `inspect` before refinement. Record formula, space group, scattering factors,
   refinement metrics, warnings, atoms, ADPs, coordination, geometry, and provenance.
3. Classify the stage independently on three axes:
   - numerical convergence: shift/esd approaches zero;
   - fit: R1, wR2, GooF, difference-map extrema, warning text;
   - chemistry: coordination, Si-O distances, angles, bond-valence sums, ADPs.
4. Run only 2-4 cycles while the model is incomplete. Request more cycles only when
   shifts remain meaningful and the model has no hard chemical failure.
5. After every run, compare metrics and inspect the highest peak/deepest hole and the
   atoms named by maximum shift or abnormal ADP.
6. Stop cycling when shift is small but R/GoF/geometry remains poor. More cycles cannot
   repair a wrong model; propose one model change and its evidence instead.
7. Make model-changing edits explicitly, one hypothesis at a time, in an isolated
   copy. Retain a before/after metric record and revert hypotheses that do not improve
   both fit and chemistry.
8. Finish with a full-matrix stage, stable weights, no unexplained hard alerts, and a
   human final report. Never claim completion from R values alone.

## Zeolite / SiO2 Policy

Read [references/zeolite-sio2.md](references/zeolite-sio2.md) whenever the model is a
zeolite, silica, silicate framework, or contains ambiguous Si/O peaks.

Treat the coordination shortcut as a candidate classifier:

- 3-4 neighbours in the provisional Si-O contact shell suggest a T/Si site.
- 1-2 neighbours suggest an O site.
- A complete ideal silica framework should have SiO4 tetrahedra and two-connected
  bridging oxygen, but interrupted frameworks, disorder, missing sites, and special
  positions can break this pattern.

Require agreement among coordination, bond distances, angles, bond-valence sum,
occupancy/special-position multiplicity, ADP behaviour, difference density, and formula
before changing an element. Never use peak height alone for Si/O assignment.

## Reading SHELXL Results

Read [references/shelxl-metrics.md](references/shelxl-metrics.md) before judging
convergence or model quality. Key locations are:

- `.lst`: per-cycle `Mean shift/esd` and `Maximum`, the responsible parameter,
  warnings, NPD count, disagreeable reflections, and final peak/hole positions.
- `.res`: final `REM R1`, `wR2`, `GooF`, `Highest difference peak`, `deepest hole`,
  and Olex2's `Shift_max` summary when present.
- atom records in `.res`: occupancy and isotropic/anisotropic displacement parameters.

Interpret `shift/esd` as convergence only. A value near zero means the least-squares
minimum stopped moving; it does not prove the atom model, space group, scattering
factors, weights, or diffraction approximation is correct.

## Model-Change Guardrails

The Agent may execute without further confirmation:

- locate and inspect files;
- run short refinement stages in new directories;
- parse and compare outputs;
- propose ranked atom assignments and next actions.

Require explicit user approval or a prior instruction that clearly authorizes automated
model editing before:

- changing atom identity or coordinates;
- adding/removing Q peaks as atoms;
- changing occupancy, free variables, PART, EXTI, WGHT, restraints, or space group;
- omitting reflections;
- accepting a result as the new canonical structure.

For electron diffraction, preserve supplied electron-scattering `SFAC` records. Do not
replace them with X-ray scattering factors. Consider `EXTI`, dynamical-scattering
limitations, completeness, and merged-data quality before blaming every high R value on
the structural model.

## Required Report

Report at least:

```text
Requested target and resolved RES/HKL:
Input SHA-256 and isolated output directory:
SHELXL binary/version and exact command:
R1(gt/all), wR2, GooF:
Mean/max shift-esd and responsible parameter:
Highest peak, deepest hole, map sigma, peak/sigma:
NPD and SHELXL warnings:
ADP outliers:
Si/O coordination, Si-O distance, angle, and BVS outliers:
Classification: continue_cycles | inspect_model | halt_invalid | final_candidate
Evidence-ranked next action:
What was not changed automatically:
```
