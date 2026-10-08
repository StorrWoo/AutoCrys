"""Exercise the actual Tk callbacks and WSL->Windows process bridge on a dataset."""
import argparse
import json
from pathlib import Path
import sys
import time
import tkinter as tk

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from AutoDials.mini_ui import DialsMiniUI


def capture_tk(root, filename):
    # WSLg's root-window capture is unavailable; read only this test window.
    import ctypes as c
    from PIL import Image
    class XImage(c.Structure):
        _fields_ = [("width", c.c_int), ("height", c.c_int), ("xoffset", c.c_int),
                    ("format", c.c_int), ("data", c.c_void_p), ("byte_order", c.c_int),
                    ("bitmap_unit", c.c_int), ("bitmap_bit_order", c.c_int),
                    ("bitmap_pad", c.c_int), ("depth", c.c_int),
                    ("bytes_per_line", c.c_int), ("bits_per_pixel", c.c_int)]
    lib = c.CDLL("libX11.so.6")
    lib.XOpenDisplay.argtypes = [c.c_char_p]
    lib.XOpenDisplay.restype = c.c_void_p
    lib.XGetImage.argtypes = [c.c_void_p, c.c_ulong, c.c_int, c.c_int, c.c_uint, c.c_uint, c.c_ulong, c.c_int]
    lib.XGetImage.restype = c.POINTER(XImage)
    lib.XDestroyImage.argtypes = [c.POINTER(XImage)]
    lib.XCloseDisplay.argtypes = [c.c_void_p]
    display = lib.XOpenDisplay(None)
    root.update()
    pointer = lib.XGetImage(display, root.winfo_id(), 0, 0, root.winfo_width(), root.winfo_height(), c.c_ulong(-1).value, 2)
    if not pointer:
        raise RuntimeError("Could not capture Tk test window")
    try:
        frame = pointer.contents
        raw = c.string_at(frame.data, frame.bytes_per_line * frame.height)
        image = Image.frombytes("RGB", (frame.width, frame.height), raw, "raw", "BGRX" if frame.bits_per_pixel == 32 else "BGR", frame.bytes_per_line)
        image.save(filename)
    finally:
        lib.XDestroyImage(pointer)
        lib.XCloseDisplay(display)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--xds", action="store_true")
    parser.add_argument("--cell", default="")
    parser.add_argument("--sg", default="P1")
    parser.add_argument("--screenshot")
    parser.add_argument("--result", required=True)
    args = parser.parse_args()
    root = tk.Tk()
    ui = DialsMiniUI(root, args.dataset)
    root.update()
    ui.cell.set(args.cell)
    ui.sg.set(args.sg)
    ui.use_xds.set(args.xds)
    ui._toggle_geometry()
    assert str(ui.run_button["state"]) == "normal"
    assert str(ui.geometry_entry["state"]) == ("normal" if args.xds else "disabled")
    if args.screenshot:
        capture_tk(root, args.screenshot)
    root.withdraw()
    if not args.run:
        Path(args.result).write_text(json.dumps({"status": "ui_smoke_passed", "size": root.geometry()}))
        ui.close()
        return
    ui.start()
    if not ui.running:
        raise RuntimeError("UI did not start the worker")
    assert str(ui.run_button["state"]) == "disabled"
    deadline = time.monotonic() + 900
    def poll():
        if ui.running and time.monotonic() < deadline:
            root.after(200, poll)
        elif ui.running:
            ui.cancel()
            root.after(200, poll)
        else:
            payload = {"run_directory": str(ui.run_directory), "status_text": ui.status.get(), "result": ui.result}
            Path(args.result).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            ui.close()
    root.after(200, poll)
    root.mainloop()
    result = json.loads(Path(args.result).read_text(encoding="utf-8"))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("result") or result["result"].get("status") != "success":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
