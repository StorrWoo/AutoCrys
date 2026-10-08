from __future__ import annotations

import argparse
from pathlib import Path
import shutil


DEFAULT_SKILL_DIR = Path.home() / ".codex" / "skills" / "autor3d"
LAUNCHER = """from __future__ import annotations

from pathlib import Path
import sys


SCRIPT_DIR = Path(__file__).resolve().parent
PACKAGE_DIR = SCRIPT_DIR / "autor3d_pkg"
if not PACKAGE_DIR.exists():
    raise SystemExit(
        "AutoR3D skill package copy is missing. Run the project script "
        "'python scripts/sync_skill.py' from the AutoR3D repository."
    )

sys.path.insert(0, str(PACKAGE_DIR))

from autor3d.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
"""


def copy_package(project_root: Path, skill_dir: Path) -> None:
    src_pkg = project_root
    dst_pkg = skill_dir / "scripts" / "autor3d_pkg" / "autor3d"
    if not src_pkg.exists():
        raise FileNotFoundError(f"Source package not found: {src_pkg}")
    dst_pkg.mkdir(parents=True, exist_ok=True)
    for path in src_pkg.glob("*.py"):
        target = dst_pkg / path.name
        shutil.copy2(path, target)


def copy_skill_doc(project_root: Path, skill_dir: Path) -> None:
    skill_md = project_root / "SKILL.md"
    if not skill_md.exists():
        raise FileNotFoundError(f"Project skill document not found: {skill_md}")
    skill_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(skill_md, skill_dir / "SKILL.md")


def write_launcher(skill_dir: Path) -> None:
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "autor3d.py").write_text(LAUNCHER, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync project AutoR3D source into the Codex skill.")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--skill-dir", type=Path, default=DEFAULT_SKILL_DIR)
    args = parser.parse_args()
    project_root = args.project_root.resolve()
    skill_dir = args.skill_dir.resolve()
    copy_skill_doc(project_root, skill_dir)
    write_launcher(skill_dir)
    copy_package(project_root, skill_dir)
    print(f"Synced AutoR3D skill to {skill_dir}")
    print(f"Package copy: {skill_dir / 'scripts' / 'autor3d_pkg' / 'autor3d'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
