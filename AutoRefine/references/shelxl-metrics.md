# SHELXL metrics and decision rules

Use these values as review thresholds, not publication guarantees. Electron diffraction
(ED) refined with a kinematic model usually has substantially higher residuals than a
good X-ray refinement.

## Invocation and provenance

SHELXL reads `<name>.ins` and `<name>.hkl` in the current directory and writes
`<name>.res`, `<name>.lst`, `<name>.fcf`, and related artifacts:

```bash
$HOME/AutoCrys/AutoSolve/tools/shelxl <name> -t4
```

Use 2-4 `L.S.` cycles for an incomplete model. Preserve `SFAC`, `UNIT`, symmetry,
occupancy/free-variable encodings, and the atom model. Remove stale Q records, set
`PLAN 20` for diagnostic peaks, and run inside a new directory. Use full-matrix least
squares for the final stages.

## Metrics

### R1 and wR2

`R1(gt)` measures agreement of |Fo| and |Fc| for the observed subset, conventionally
`Fo > 4 sigma(Fo)`. `R1(all)` uses all reflections. `wR2` is a weighted residual on
squared amplitudes and is normally larger than R1.

For kinematic ED, Olex2 describes R1 around 0.12-0.15 as excellent. Use these broad
diagnostic bands for ED only:

- `R1(gt) <= 0.15`: strong fit candidate, still validate chemistry;
- `0.15 < R1(gt) <= 0.25`: usable but needs investigation;
- `0.25 < R1(gt) <= 0.40`: poor model/data fit;
- `R1(gt) > 0.40`: likely wrong/incomplete model, wrong symmetry/data, or severe ED
  modelling limitations.

Do not compare X-ray and ED R thresholds directly.

### GooF / S

SHELXL reports approximately:

```text
S = sqrt(sum[w(Fo^2-Fc^2)^2] / (number_of_data-number_of_parameters))
```

It should approach 1 only when both model and uncertainty/weight model are appropriate.
Use `0.8-1.2` as a healthy review band; `1.2-1.5` warns, `>1.5` is poor, and `>2` is
severe. A low value can mean overestimated uncertainties or excessive weighting. Because
WGHT changes S, never tune weights merely to manufacture S=1.

### Shift and shift/esd

The authoritative convergence trace is in `.lst` after each cycle:

```text
Mean shift/esd = ... Maximum = ... for <parameter> <atom>
Max. shift = ... A ... Max. dU = ...
```

Olex2 may append `REM Shift_max` and `REM Shift_mean` to `.res`.

- `max |shift/esd| <= 0.001`: strict final convergence candidate;
- `<= 0.01`: practically settled;
- `0.01-0.05`: continue a short stage if chemistry is sound;
- `0.05-0.20`: not converged; inspect the named parameter;
- `>0.20`: unstable or materially moving;
- `>1`: severe instability.

Coordinate shift in A and shift/esd are different quantities. Report both when present.
Small shift plus large R/GooF means a converged bad minimum: inspect the model rather
than adding cycles.

### Highest peak and deepest hole

The `.res`/`.lst` lines are:

```text
Highest difference peak ..., deepest hole ..., 1-sigma level ...
```

These are residual density extrema. Evaluate magnitude, map sigma, position, distance to
existing atoms, and chemical plausibility together:

- `max(abs(peak), abs(hole)) / map_sigma < 3`: usually low significance;
- `3-5`: inspect;
- `>5`: strong unexplained residual;
- a deep hole centred on an atom can indicate an overly heavy type, too-high occupancy,
  or bad ADP;
- a positive peak at a plausible framework site can indicate a missing atom, but do not
  promote a Q peak by height alone.

Absolute e/A^3 cutoffs depend on element types, resolution, temperature, scaling, and ED
approximation. Prefer the sigma ratio and spatial context.

### Displacement parameters (ADP/U)

For isotropic atoms, inspect `Uiso`; for anisotropic atoms inspect `Ueq`, eigenvalues,
ellipsoid shape, and SHELXL NPD warnings.

Hard failures:

- negative Uiso/Ueq;
- non-positive-definite (NPD) anisotropic tensor;
- extreme coordinate/ADP correlation or runaway parameter.

Review heuristics for a room-temperature inorganic framework:

- `Ueq < 0.003 A^2`: suspiciously small unless low temperature or constrained;
- `0.003-0.08 A^2`: broad plausible range;
- `0.08-0.10 A^2`: elevated; compare with equivalent sites and temperature;
- `0.10-0.15 A^2`: warning;
- `>0.15 A^2`: severe warning;
- `>2.5x` or `<0.4x` the median of chemically equivalent sites: relative outlier.

These are not universal physical constants. Large ADP can mean disorder, partial
occupancy, wrong type, absorption/dynamical-scattering error, or a missing split model.
An atom assigned too heavy often inflates its ADP; an atom assigned too light can refine
to an unusually small ADP. Change identity only after checking geometry and density.

## Stop/go policy

- `halt_invalid`: missing/mismatched HKL, SHELXL failure, negative/NPD ADP, impossible
  contacts, or corrupt instructions.
- `continue_cycles`: meaningful shift remains, metrics improve, and no hard chemistry
  alert exists.
- `inspect_model`: settled shift but poor R/GooF, significant residuals, bad geometry,
  or warnings. Propose a specific model/data hypothesis.
- `final_candidate`: strict convergence, stable weights, acceptable fit for the data
  type, low unexplained residuals, chemically consistent model, and no hard alerts.

## Primary documentation

- SHELXL refinement paper: https://journals.iucr.org/c/issues/2015/01/00/fa3356/
- Olex2 refinement workflow: https://www.olexsys.org/olex2/docs/tasks/tasks/structure-refinement/
- Olex2 ED refinement: https://www.olexsys.org/olex2/docs/tasks/refining-ed-structures/
- Olex2 command line: https://www.olexsys.org/olex2/docs/getting-started/getting-around-olex2/command-line-options/
