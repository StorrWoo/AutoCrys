# External programs (user installation only)

This repository and its Python distributions contain no SHELXT, SHELXL,
SHELXS, SHELXC/D/E, AnoDe, CIFTAB, ShredCIF, PDB2INS, XDS or other external
program binaries or download archives. Users obtain and install them independently.

- SHELX: https://shelx.uni-goettingen.de/ (registration, terms and downloads).
- XDS: https://xds.mr.mpg.de/html_doc/downloading.html
- DIALS (optional): https://dials.github.io/installation.html
- Olex2 (optional): https://www.olexsys.org/olex2/

For a source checkout, place your independently obtained Linux SHELXL executable
at `AutoSolve/tools/shelxl` and grant executable permission. SHELXT is called as
`shelxt` on Linux PATH. For wheel installations, use `--shelxl-bin /path/to/shelxl`
with AutoSolve/AutoRefine CLI, or use a source checkout for the integrated UI.
Local binaries are ignored by Git and excluded from source/wheel distributions.
See the deployment guide for detailed setup. AutoCrys does not download them.
