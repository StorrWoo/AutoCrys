"""Read reflection-specific metadata without requiring DIALS in AutoCrys."""
import json
import math
from pathlib import Path


def reflection_metadata(hkl):
    hkl = Path(hkl)
    summary = hkl.parent / "summary.json"
    if not summary.is_file():
        return None
    data = json.loads(summary.read_text(encoding="utf-8"))
    if data.get("status") != "success" or data.get("experiment") != hkl.stem:
        return None
    cell = data.get("cell")
    if not cell or len(cell) != 6 or not all(math.isfinite(float(v)) for v in cell):
        raise ValueError(f"Invalid result cell in {summary}")
    sg = data.get("space_group_number")
    if sg is None:
        # DIALS' own SHELX writer encodes its SG number in TITL.
        ins = hkl.with_suffix(".ins")
        for line in ins.read_text(encoding="utf-8").splitlines():
            if line.startswith("TITL "):
                words = line.split()
                if len(words) > 1 and words[1].isdigit():
                    sg = int(words[1])
    if sg is None or not 1 <= int(sg) <= 230:
        raise ValueError(f"Missing result space-group number in {summary}")
    result = {"cell": list(map(float, cell)), "space_group_number": int(sg), "source": str(summary)}
    ins = hkl.with_suffix(".ins")
    if ins.is_file():
        symm = []
        for line in ins.read_text(encoding="utf-8").splitlines():
            words = line.split()
            if not words:
                continue
            if words[0] == "LATT":
                result["latt"] = int(words[1])
            elif words[0] == "SYMM":
                symm.append(line[5:].strip())
            elif words[0] == "CELL":
                result["wavelength"] = float(words[1])
        result["symm"] = symm
    return result
