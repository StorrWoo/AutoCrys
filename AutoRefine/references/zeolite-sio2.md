# Zeolite and SiO2 refinement policy

## Framework model

An ideal fully connected silica zeolite consists of corner-sharing SiO4 tetrahedra:

- framework Si (T site) has four O neighbours;
- framework O normally bridges two T sites;
- each T therefore connects through oxygen to four neighbouring T sites;
- real interrupted frameworks, terminal silanols, defects, disorder, and special
  positions may deviate from the ideal topology.

This is why `coordination > 2 => Si; coordination <= 2 => O` is useful only as a first
candidate rule. Use a provisional 1.35-1.90 A contact shell, including crystallographic
symmetry and periodic neighbours. Then require all evidence below before relabelling.

## Geometry targets

For ordinary tetrahedral Si(IV)-O:

- central target: about `1.61 A`;
- normal review band: `1.56-1.68 A`;
- early-model warning: `<1.50 A` or `>1.75 A`;
- severe contact/model warning: `<1.40 A` or `>1.90 A`.

Do not force every bond to exactly 1.61 A. Bridging environment, temperature, framework
strain, composition, uncertainty, and refinement quality create a distribution.

Angle checks:

- O-Si-O tetrahedral target is `109.47 degrees`;
- `100-120 degrees` is a broad review band for a preliminary zeolite model;
- Si-O-Si is flexible and commonly much wider; use `130-180 degrees` as a broad
  topology review band, not a restraint target.

## Bond-valence check

Use the bond-valence equation only as corroboration:

```text
s_ij = exp[(R0 - R_ij) / B]
BVS_i = sum(s_ij)
```

For Si4+-O2-, use the modern oxygen-pair fit `R0=1.624 A`, `B=0.389 A` in the diagnostic
script. Expect a Si BVS near 4 and a bridging O BVS near 2. Flag Si outside `3.4-4.6` or
O outside `1.6-2.4` for review; treat larger deviations as strong model evidence, not an
automatic edit.

## Evidence required for Si/O reassignment

Rank a candidate as Si only when most of the following agree:

1. 3-4 neighbours in the provisional Si-O shell, ideally four.
2. Distances and O-Si-O angles form a plausible tetrahedron.
3. BVS is compatible with Si4+.
4. Its neighbours are mostly two-connected bridging-O candidates.
5. Occupancy and special-position multiplicity preserve a plausible Si:O framework
   composition (near 1:2 for pure silica).
6. The assigned ADP becomes comparable to other Si sites.
7. R1/wR2/GooF and local difference density improve after a trial refinement.

Rank a candidate as O when it has one or two plausible T neighbours, sensible BVS,
bridging geometry, and O-like ADP/density behaviour.

Never relabel solely from peak height. In ED, scattering contrast is not the same as
X-ray atomic-number intuition, and dynamical scattering/incomplete data distort density.

## Refinement sequence for a preliminary framework

1. Confirm cell, space group, HKL identity, ED scattering factors, formula intent, and
   special-position occupancies.
2. Run 2-4 isotropic cycles; inspect shift/esd, R values, GooF, Q peaks, and ADPs.
3. Build the symmetry-aware contact graph. Resolve gross topology errors first:
   impossible short contacts, isolated sites, Si CN far from four, O CN above two.
4. Trial one atom-type correction at a time in a new branch. Keep it only if fit,
   geometry, BVS, and ADPs improve together.
5. Locate genuinely missing framework sites from positive density plus topology. Do not
   turn all Q peaks into atoms.
6. Refine occupancy/disorder only with map and chemical evidence. Use restraints as
   justified observations, never to conceal a wrong topology.
7. Introduce anisotropic refinement only when data/parameter ratio and stability allow;
   reject NPD or severely distorted ellipsoids and step back if needed.
8. Review EXTI for ED, then weights late in the workflow. Do not use WGHT to hide model
   error. Do not mass-omit disagreeable reflections.
9. Run final full-matrix cycles and re-check every metric and framework invariant.

## Sample-specific expectation: sample3_1_a

The current repository resolves `sample3_1_a` to
`Data/sample3_1/diff/p/cluster_1_a.res`. It is a Pnma Si/O model. The existing output is
numerically settled but has poor fit, so its first classification should be
`inspect_model`, not `continue_cycles` or `final_candidate`.

## Primary references

- IZA framework definition and T connectivity: https://www.iza-structure.org/databases/DatabaseHelp_Structures.php
- IZA framework description: https://www.iza-structure.org/IZA-SC/verified_syntheses/Introduction_Verified_Syntheses_of_Zeolites-3rd-Edition-2016.pdf
- Gagne and Hawthorne, bond-valence parameters: https://journals.iucr.org/b/issues/2015/05/00/yb5007/
- Olex2 ED caveats: https://www.olexsys.org/olex2/docs/tasks/refining-ed-structures/
