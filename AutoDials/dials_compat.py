"""Process-local workaround for scitbx's Windows LLP64 memory-limit overflow.

Does not edit the DIALS installation or the reported system memory. Only clamps
the cubicle-neighbor allocation ceiling to the C unsigned-long API's range.
"""
import ctypes
import os


def apply():
    if os.name != "nt" or ctypes.sizeof(ctypes.c_ulong) != 4:
        return
    import scitbx.array_family.flex  # initialize extension dependencies
    import scitbx.stl.map
    import scitbx_cubicle_neighbors_ext as ext
    original = ext.cubicles_max_memory_allocation_set
    if getattr(original, "_cred2_compat", False):
        return
    def bounded(number_of_bytes):
        return original(number_of_bytes=min(int(number_of_bytes), 2**32 - 1))
    bounded._cred2_compat = True
    ext.cubicles_max_memory_allocation_set = bounded


apply()  # Also executed by Windows multiprocessing spawn (__mp_main__).

if __name__ == "__main__":
    import importlib
    import sys
    apply()
    command = sys.argv.pop(1)
    module = "dials_import" if command == "import" else command
    importlib.import_module("dials.command_line." + module).run()
