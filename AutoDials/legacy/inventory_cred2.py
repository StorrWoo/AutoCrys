"""Read-only survey of cRED2 metadata, TIFF counts and first-image dimensions."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from prepare_cred2 import read_parameters
from PIL import Image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    rows = []
    for path in sorted(args.root.resolve().rglob("*cRED2*parameters*.txt")):
        row = {"dataset": str(path.parent.relative_to(args.root.resolve()))}
        try:
            raw, number = read_parameters(path)
            frames = sorted(path.parent.joinpath("diff").glob("frame_*.tif"))
            ids = sorted(int(p.stem.split("_")[-1]) for p in frames)
            n = int(number("Number of frames"))
            row.update(frames_found=len(frames), frames_expected=n,
                       sequence_complete=ids == list(range(1, n + 1)),
                       wavelength_A=number("Wavelength"),
                       pixel_mm=number("Physical pixelsize"),
                       calibrated_distance_mm=number("Physical pixelsize") / (number("Wavelength") * number("Pixelsize")),
                       step_deg=number("Oscillation angle"),
                       stretch_amplitude=raw.get("Stretch amplitude"))
            if frames:
                with Image.open(frames[0]) as image:
                    row.update(image_size=image.size, image_mode=image.mode)
        except Exception as error:
            row["error"] = str(error)
        rows.append(row)
    result = {"datasets": len(rows), "complete_sequences": sum(r.get("sequence_complete", False) for r in rows), "records": rows}
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "records"}))


if __name__ == "__main__":
    main()
