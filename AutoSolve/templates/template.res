TITL template structure refinement from XDS/SHELX HKL

REM ============================================================
REM Basic SHELXL .res/.ins template
REM Replace CELL, ZERR, LATT, SYMM, SFAC, UNIT and atom list.
REM Reflection file should normally have the same basename.
REM Example:
REM   template.res / template.hkl
REM or for command-line SHELXL:
REM   copy template.res template.ins
REM   shelxl template
REM ============================================================

CELL 0.02508  10.0000  10.0000  10.0000  90.000  90.000  90.000
ZERR 1        0.0010   0.0010   0.0010   0.010   0.010   0.010

REM ------------------------------------------------------------
REM LATT and SYMM must match the space group.
REM Examples:
REM P1:
REM   LATT -1
REM
REM P-1:
REM   LATT 1
REM
REM For other space groups, add proper SYMM lines.
REM ------------------------------------------------------------

LATT -1

REM Example SYMM lines should be inserted here if needed:
REM SYMM -X, Y+1/2, -Z+1/2

REM ------------------------------------------------------------
REM Define atom types and formula-unit contents.
REM SFAC order determines atom type numbers in atom lines.
REM Example below: C H N O Zn
REM UNIT numbers are total atom counts in the unit cell.
REM ------------------------------------------------------------

SFAC C H N O
UNIT 1 1 1 1

REM ------------------------------------------------------------
REM Refinement instructions
REM ------------------------------------------------------------

L.S. 10
PLAN 20
BOND $H
CONF
ACTA

REM Optional, often generated/recommended by SHELXL after refinement:
REM WGHT 0.1000 0.0000

FVAR 1.00000

REM ------------------------------------------------------------
REM Atom list starts here.
REM Format:
REM atom_name sfac_number x y z sof Uiso
REM
REM Example:
REM C1    1    0.100000    0.200000    0.300000    11.00000    0.05000
REM O1    4    0.250000    0.350000    0.450000    11.00000    0.05000
REM
REM sof = 11.00000 means occupancy fixed at 1.0.
REM ------------------------------------------------------------

REM Put atoms here

HKLF 4

END