from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

from .exports import sample_observations
from .observations import ObservationData
from .peaks import PeakCandidate
from .reconstruct import VolumeData


def _relative_to(path: str, base: Path) -> str:
    try:
        return Path(os.path.relpath(path, base)).as_posix()
    except ValueError:
        return path


def _validated_reciprocal_basis(crystal: dict | None) -> list[list[float]] | None:
    """Return a finite, invertible 3x3 reciprocal basis from panel metadata."""

    if not crystal:
        return None
    try:
        basis = np.asarray(crystal.get("reciprocal_basis"), dtype=np.float64)
    except (TypeError, ValueError):
        return None
    if basis.shape != (3, 3) or not np.all(np.isfinite(basis)):
        return None
    if abs(float(np.linalg.det(basis))) <= 1e-15:
        return None
    return basis.tolist()


def _panel_data(
    peaks: list[PeakCandidate],
    observations: ObservationData,
    volume: VolumeData,
    max_observations: int,
    reciprocal_pixel_per_angstrom: float | None = None,
    detector_dimensions_px: tuple[int, int] | None = None,
    indexed_observations: np.ndarray | None = None,
    indexed_slices: dict[str, dict] | None = None,
    crystal: dict | None = None,
    panel_dir: Path | None = None,
) -> dict:
    keep = sample_observations(observations, max_observations)
    indexed = bool(
        indexed_observations is not None and indexed_observations.shape[0] == observations.size
    )
    reciprocal_basis = _validated_reciprocal_basis(crystal) if indexed else None
    indexed_geometry = reciprocal_basis is not None
    crystal_payload = dict(crystal) if crystal else None
    if crystal_payload is not None:
        crystal_payload["reciprocal_basis"] = reciprocal_basis
    positions = observations.q
    peak_payload = [
        {
            "id": peak.id,
            "q": list(peak.q),
            "intensity": peak.intensity,
            "hit_count": peak.hit_count,
            "frame_min": peak.frame_min,
            "frame_max": peak.frame_max,
        }
        for peak in peaks
    ]
    observation_payload = [
        {
            "q": [float(v) for v in positions[i]],
            "intensity": float(observations.intensity[i]),
            "frame": int(observations.frame[i]),
        }
        for i in keep
    ]
    slices = {
        "xy": {"png": "../reconstruction/slice_xy.png", "label": "XY slice (q)"},
        "xz": {"png": "../reconstruction/slice_xz.png", "label": "XZ slice (q)"},
        "yz": {"png": "../reconstruction/slice_yz.png", "label": "YZ slice (q)"},
    }
    if indexed_slices:
        slices = {}
        for family, entry in indexed_slices.items():
            png = str(entry.get("png"))
            slices[family] = {
                "png": _relative_to(png, panel_dir) if panel_dir is not None else png,
                "label": str(entry.get("title", family)),
            }
    return {
        "peaks": peak_payload,
        "observations": observation_payload,
        "space": "q",
        "indexed": indexed,
        "indexed_geometry": indexed_geometry,
        "crystal": crystal_payload,
        "frame_range": [
            int(np.min(observations.frame)) if observations.size else 1,
            int(np.max(observations.frame)) if observations.size else 1,
        ],
        "volume": {
            "shape": list(volume.shape),
            "voxel_size": volume.voxel_size,
            "origin": list(volume.origin),
            "max_intensity": float(np.max(volume.i_mean)) if volume.i_mean.size else 0.0,
            "nonzero_voxels": int(np.count_nonzero(volume.hit_count)),
            "slices": slices,
        },
        "detector": {
            "reciprocal_pixel_per_angstrom": (
                float(reciprocal_pixel_per_angstrom)
                if reciprocal_pixel_per_angstrom is not None
                else None
            ),
            "dimensions_px": (
                [int(detector_dimensions_px[0]), int(detector_dimensions_px[1])]
                if detector_dimensions_px is not None
                else [516, 516]
            ),
            "reciprocal_span_a_inv": (
                [
                    float(detector_dimensions_px[0] * reciprocal_pixel_per_angstrom),
                    float(detector_dimensions_px[1] * reciprocal_pixel_per_angstrom),
                ]
                if detector_dimensions_px is not None and reciprocal_pixel_per_angstrom is not None
                else None
            ),
        },
    }


def write_panel(
    out_dir: str | Path,
    peaks: list[PeakCandidate],
    observations: ObservationData,
    volume: VolumeData,
    max_observations: int = 20000,
    reciprocal_pixel_per_angstrom: float | None = None,
    detector_dimensions_px: tuple[int, int] | None = None,
    indexed_observations: np.ndarray | None = None,
    indexed_slices: dict[str, dict] | None = None,
    crystal: dict | None = None,
) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    data = _panel_data(
        peaks,
        observations,
        volume,
        max_observations,
        reciprocal_pixel_per_angstrom=reciprocal_pixel_per_angstrom,
        detector_dimensions_px=detector_dimensions_px,
        indexed_observations=indexed_observations,
        indexed_slices=indexed_slices,
        crystal=crystal,
        panel_dir=out_dir,
    )
    data_json = json.dumps(data, separators=(",", ":"))
    slice_cards = "".join(
        f'<div class="slice"><img src="{entry.get("png", "")}" alt="{family} slice">'
        f'<p>{entry.get("label", family)}</p></div>'
        for family, entry in data["volume"]["slices"].items()
    )
    crystal = data.get("crystal") or {}
    cell = crystal.get("cell")
    crystal_text = (
        " ".join(f"{float(value):.2f}" for value in cell)
        + (
            f" | SG {crystal.get('space_group_number')}"
            if crystal.get("space_group_number")
            else ""
        )
        if cell
        else "n/a"
    )
    fallback_text = (
        "Drag orbits; Shift+drag rolls in-plane; Alt+drag rotates vertically; "
        "Ctrl+drag rotates horizontally. Right drag pans; wheel zooms. "
        "A/B/C align a*/b*/c*, I isometric, F fit."
        if data["indexed_geometry"]
        else "Drag orbits; Shift/Alt/Ctrl constrain rotation. Right drag pans; wheel zooms."
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AutoR3D Panel</title>
  <style>
    :root {{
      color-scheme: dark;
      font-family: SimSun, "Songti SC", "宋体", serif;
      background: #101214;
      color: #eef1f3;
    }}
    body {{ margin: 0; min-height: 100vh; background: #101214; }}
    .app {{ display: grid; grid-template-columns: 320px 1fr; min-height: 100vh; }}
    aside {{ border-right: 1px solid #2d3338; padding: 18px; background: #171a1d; }}
    main {{ position: relative; min-height: 100vh; }}
    h1 {{ font-size: 20px; margin: 0 0 14px; letter-spacing: 0; }}
    h2 {{ font-size: 13px; margin: 20px 0 10px; color: #b8c0c7; text-transform: uppercase; letter-spacing: 0; }}
    .tabs {{ display: grid; grid-template-columns: 1fr; gap: 8px; margin-bottom: 18px; }}
    button {{
      border: 1px solid #3a4148;
      background: #22272b;
      color: #eef1f3;
      border-radius: 6px;
      padding: 9px 10px;
      text-align: left;
      cursor: pointer;
      font-size: 14px;
    }}
    button.active {{ background: #2f5d62; border-color: #58a6ad; }}
    label {{ display: grid; gap: 6px; margin: 12px 0; color: #c8d0d6; font-size: 13px; }}
    select {{
      width: 100%;
      border: 1px solid #3a4148;
      background: #22272b;
      color: #eef1f3;
      border-radius: 6px;
      padding: 8px 9px;
      font-size: 14px;
    }}
    input[type="range"] {{ width: 100%; }}
    .stat {{ display: flex; justify-content: space-between; gap: 12px; border-top: 1px solid #2d3338; padding: 8px 0; font-size: 13px; }}
    .stat span:first-child {{ color: #aab3ba; }}
    #scene {{ width: 100%; height: 100vh; display: block; }}
    .volume {{ display: none; padding: 22px; overflow: auto; height: calc(100vh - 44px); }}
    .volume.active {{ display: block; }}
    .slice-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }}
    .slice img {{ width: 100%; height: auto; border: 1px solid #2d3338; background: #050607; }}
    .slice p {{ margin: 8px 0 0; color: #c8d0d6; }}
    .fallback {{ position: absolute; left: 18px; bottom: 18px; color: #aab3ba; font-size: 12px; }}
    .view-toolbar {{
      position: absolute;
      top: 14px;
      right: 14px;
      display: grid;
      gap: 8px;
      padding: 10px;
      border: 1px solid #343c43;
      border-radius: 8px;
      background: rgba(18, 22, 25, 0.88);
      backdrop-filter: blur(5px);
      z-index: 3;
    }}
    .view-buttons {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .view-buttons button {{ padding: 6px 9px; text-align: center; min-width: 42px; }}
    .view-buttons button.active {{ background: #2f5d62; border-color: #58a6ad; }}
    .view-toggle {{ display: flex; grid-template-columns: auto 1fr; align-items: center; gap: 7px; margin: 0; }}
    .axis-legend {{ display: flex; gap: 12px; font-size: 12px; }}
    .axis-a {{ color: #ee5253; }}
    .axis-b {{ color: #49c969; }}
    .axis-c {{ color: #448bff; }}
    .scale-bar {{
      position: absolute;
      right: 18px;
      bottom: 18px;
      min-width: 190px;
      color: #dbe4ea;
      font-size: 12px;
      text-align: right;
      pointer-events: none;
      text-shadow: 0 1px 2px #000;
    }}
    .scale-line {{
      height: 10px;
      margin-left: auto;
      margin-bottom: 5px;
      border-left: 2px solid #dbe4ea;
      border-right: 2px solid #dbe4ea;
      border-bottom: 2px solid #dbe4ea;
      background: rgba(219, 228, 234, 0.08);
    }}
    .scale-meta {{ color: #aab3ba; margin-top: 2px; }}
    @media (max-width: 860px) {{
      .app {{ grid-template-columns: 1fr; }}
      aside {{ border-right: none; border-bottom: 1px solid #2d3338; }}
      #scene {{ height: 68vh; }}
    }}
    @media (max-width: 520px) {{
      .fallback {{
        left: 18px;
        right: 18px;
        bottom: 72px;
        line-height: 1.35;
      }}
      .scale-bar {{
        right: 18px;
        bottom: 16px;
        max-width: calc(100vw - 36px);
      }}
      .scale-meta {{
        max-width: 260px;
        margin-left: auto;
      }}
    }}
  </style>
</head>
<body>
  <div class="app">
    <aside>
      <h1>AutoR3D</h1>
      <div class="tabs">
        <button id="tab-peaks" class="active">Peak Candidates</button>
        <button id="tab-volume">Volume</button>
        <button id="tab-observations">Raw Observations</button>
      </div>
      <h2>Filters</h2>
      <label>Intensity threshold <input id="threshold" type="range" min="0" max="100" value="0"></label>
      <label>Display mode <select id="displayMode"><option value="spot" selected>Spot</option><option value="gaussian">Gaussian</option></select></label>
      <label>Point size <input id="pointSize" type="range" min="1" max="10" value="4"></label>
      <label>Brightness <input id="brightness" type="range" min="0" max="3" step="0.05" value="3"></label>
      <label>Contrast <input id="contrast" type="range" min="0.1" max="3" step="0.05" value="3"></label>
      <label>Gamma <input id="gamma" type="range" min="0.2" max="3" step="0.05" value="1"></label>
      <label>Frame min <input id="frameMin" type="range" min="1" max="421" value="1"></label>
      <label>Frame max <input id="frameMax" type="range" min="1" max="421" value="421"></label>
      <h2>Stats</h2>
      <div class="stat"><span>Shown points</span><span id="shown">0</span></div>
      <div class="stat"><span>Peaks</span><span id="peakCount">0</span></div>
      <div class="stat"><span>Observations</span><span id="obsCount">0</span></div>
      <div class="stat"><span>Volume shape</span><span id="volShape"></span></div>
      <div class="stat"><span>Voxel size</span><span id="voxel"></span></div>
      <div class="stat"><span>Space</span><span id="space"></span></div>
      <div class="stat"><span>Crystal cell</span><span id="cell"></span></div>
    </aside>
    <main>
      <canvas id="scene"></canvas>
      <div id="viewToolbar" class="view-toolbar">
        <div class="view-buttons">
          <button type="button" data-view="iso" class="active">Iso · I</button>
          <button type="button" data-view="a">a* · A</button>
          <button type="button" data-view="b">b* · B</button>
          <button type="button" data-view="c">c* · C</button>
          <button type="button" id="fitView">Fit · F</button>
        </div>
        <label class="view-toggle"><input id="showCell" type="checkbox" checked> Cell/grid · G</label>
        <div class="axis-legend"><span class="axis-a">a* red</span><span class="axis-b">b* green</span><span class="axis-c">c* blue</span></div>
      </div>
      <section id="volume" class="volume">
        <div class="slice-grid">
          {slice_cards}
        </div>
      </section>
      <div class="fallback">{fallback_text}</div>
      <div id="scaleBar" class="scale-bar">
        <div id="scaleBarLine" class="scale-line"></div>
        <div id="scaleBarLabel"></div>
        <div id="scaleBarMeta" class="scale-meta"></div>
      </div>
    </main>
  </div>
  <script>window.AUTOR3D_DATA = {data_json};</script>
  <script type="importmap">
    {{
      "imports": {{
        "three": "https://unpkg.com/three@0.164.1/build/three.module.js",
        "three/addons/": "https://unpkg.com/three@0.164.1/examples/jsm/"
      }}
    }}
  </script>
  <script type="module">
    import * as THREE from "three";
    import {{ OrbitControls }} from "three/addons/controls/OrbitControls.js";

    const data = window.AUTOR3D_DATA;
    const canvas = document.getElementById("scene");
    const volumeSection = document.getElementById("volume");
    const renderer = new THREE.WebGLRenderer({{ canvas, antialias: true }});
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x101214);
    const reciprocalBasis = data.indexed_geometry ? data.crystal?.reciprocal_basis : null;
    const hklToQ = (hkl) => reciprocalBasis
      ? [
          reciprocalBasis[0][0] * hkl[0] + reciprocalBasis[0][1] * hkl[1] + reciprocalBasis[0][2] * hkl[2],
          reciprocalBasis[1][0] * hkl[0] + reciprocalBasis[1][1] * hkl[1] + reciprocalBasis[1][2] * hkl[2],
          reciprocalBasis[2][0] * hkl[0] + reciprocalBasis[2][1] * hkl[1] + reciprocalBasis[2][2] * hkl[2]
        ]
      : hkl;
    const reciprocalAxes = [
      hklToQ([1, 0, 0]),
      hklToQ([0, 1, 0]),
      hklToQ([0, 0, 1])
    ].map((values) => new THREE.Vector3(...values));
    const allQ = [...data.peaks, ...data.observations].flatMap((row) => row.q ?? []);
    const qExtent = Math.max(1, ...allQ.map((value) => Math.abs(value)));
    const viewSize = qExtent * 2.25;
    const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.001, 1000);
    const cameraDistance = Math.max(4, viewSize * 2.5);
    camera.position.set(cameraDistance, cameraDistance, cameraDistance);
    camera.lookAt(0, 0, 0);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.rotateSpeed = 0.65;
    controls.screenSpacePanning = true;
    controls.zoomToCursor = true;
    controls.minZoom = 0.08;
    controls.maxZoom = 40;
    controls.target.set(0, 0, 0);
    const guideGroup = new THREE.Group();
    const axisLength = Math.max(qExtent * 0.32, 1e-3);
    const axisPositions = [];
    const axisColors = [];
    const reciprocalColors = [
      new THREE.Color(0xee5253),
      new THREE.Color(0x49c969),
      new THREE.Color(0x448bff)
    ];
    reciprocalAxes.forEach((axisVector, axis) => {{
      const end = axisVector.clone().normalize().multiplyScalar(axisLength);
      axisPositions.push(0, 0, 0, end.x, end.y, end.z);
      const color = reciprocalColors[axis];
      axisColors.push(color.r, color.g, color.b, color.r, color.g, color.b);
    }});
    const axisGeometry = new THREE.BufferGeometry();
    axisGeometry.setAttribute("position", new THREE.Float32BufferAttribute(axisPositions, 3));
    axisGeometry.setAttribute("color", new THREE.Float32BufferAttribute(axisColors, 3));
    const axes = new THREE.LineSegments(
      axisGeometry,
      new THREE.LineBasicMaterial({{ vertexColors: true }})
    );
    guideGroup.add(axes);
    const grid = new THREE.GridHelper(2, 2, 0x47515a, 0x252a2f);
    grid.rotation.x = Math.PI / 2;
    grid.visible = !data.indexed_geometry;
    guideGroup.add(grid);

    const cellPositions = [];
    const cellColors = [];
    for (let axis = 0; axis < 3; axis += 1) {{
      const other = [0, 1, 2].filter((value) => value !== axis);
      for (const first of [-1, 0, 1]) {{
        for (const second of [-1, 0, 1]) {{
          const start = [0, 0, 0];
          const end = [0, 0, 0];
          start[axis] = -1;
          end[axis] = 1;
          start[other[0]] = end[other[0]] = first;
          start[other[1]] = end[other[1]] = second;
          cellPositions.push(...hklToQ(start), ...hklToQ(end));
          const color = reciprocalColors[axis];
          cellColors.push(color.r, color.g, color.b, color.r, color.g, color.b);
        }}
      }}
    }}
    const cellGeometry = new THREE.BufferGeometry();
    cellGeometry.setAttribute("position", new THREE.Float32BufferAttribute(cellPositions, 3));
    cellGeometry.setAttribute("color", new THREE.Float32BufferAttribute(cellColors, 3));
    const cellLines = new THREE.LineSegments(
      cellGeometry,
      new THREE.LineBasicMaterial({{ vertexColors: true, transparent: true, opacity: 0.92 }})
    );
    cellLines.visible = data.indexed_geometry;
    guideGroup.add(cellLines);
    scene.add(guideGroup);
    let points = null;
    let pointMaterial = null;
    let mode = "peaks";

    const els = {{
      threshold: document.getElementById("threshold"),
      displayMode: document.getElementById("displayMode"),
      pointSize: document.getElementById("pointSize"),
      brightness: document.getElementById("brightness"),
      contrast: document.getElementById("contrast"),
      gamma: document.getElementById("gamma"),
      frameMin: document.getElementById("frameMin"),
      frameMax: document.getElementById("frameMax"),
      shown: document.getElementById("shown"),
      peakCount: document.getElementById("peakCount"),
      obsCount: document.getElementById("obsCount"),
      volShape: document.getElementById("volShape"),
      voxel: document.getElementById("voxel"),
      space: document.getElementById("space"),
      cell: document.getElementById("cell"),
      viewToolbar: document.getElementById("viewToolbar"),
      showCell: document.getElementById("showCell"),
      fitView: document.getElementById("fitView"),
      scaleBar: document.getElementById("scaleBar"),
      scaleBarLine: document.getElementById("scaleBarLine"),
      scaleBarLabel: document.getElementById("scaleBarLabel"),
      scaleBarMeta: document.getElementById("scaleBarMeta")
    }};
    els.frameMin.min = data.frame_range[0];
    els.frameMin.max = data.frame_range[1];
    els.frameMin.value = data.frame_range[0];
    els.frameMax.min = data.frame_range[0];
    els.frameMax.max = data.frame_range[1];
    els.frameMax.value = data.frame_range[1];
    els.peakCount.textContent = data.peaks.length;
    els.obsCount.textContent = data.observations.length;
    els.volShape.textContent = data.volume.shape.join(" x ");
    els.voxel.textContent = data.volume.voxel_size.toFixed(4);
    const space = "q";
    const units = "A^-1";
    els.space.textContent = data.indexed_geometry ? "q (A^-1; indexed)" : "q (A^-1)";
    els.cell.textContent = {crystal_text!r};

    function setTab(next) {{
      mode = next;
      document.querySelectorAll(".tabs button").forEach((button) => button.classList.remove("active"));
      document.getElementById(`tab-${{next}}`).classList.add("active");
      const isVolume = next === "volume";
      canvas.style.display = isVolume ? "none" : "block";
      volumeSection.classList.toggle("active", isVolume);
      els.scaleBar.style.display = isVolume ? "none" : "block";
      els.viewToolbar.style.display = isVolume ? "none" : "grid";
      if (!isVolume) rebuildPoints();
    }}
    document.getElementById("tab-peaks").onclick = () => setTab("peaks");
    document.getElementById("tab-volume").onclick = () => setTab("volume");
    document.getElementById("tab-observations").onclick = () => setTab("observations");

    function markActiveView(name) {{
      document.querySelectorAll("[data-view]").forEach((button) => {{
        button.classList.toggle("active", button.dataset.view === name);
      }});
    }}
    function setView(name) {{
      const axisIndex = {{ a: 0, b: 1, c: 2 }};
      const direction = name === "iso"
        ? reciprocalAxes.reduce(
            (sum, axis) => sum.add(axis.clone().normalize()),
            new THREE.Vector3()
          ).normalize()
        : reciprocalAxes[axisIndex[name]].clone().normalize();
      camera.position.copy(direction.multiplyScalar(cameraDistance));
      const upAxisIndex = name === "c" ? 1 : 2;
      let up = reciprocalAxes[upAxisIndex].clone().normalize();
      up.addScaledVector(direction, -up.dot(direction)).normalize();
      if (up.lengthSq() < 1e-12) {{
        const fallback = [
          new THREE.Vector3(0, 0, 1),
          new THREE.Vector3(0, 1, 0),
          new THREE.Vector3(1, 0, 0)
        ].reduce((best, candidate) =>
          Math.abs(candidate.dot(direction)) < Math.abs(best.dot(direction)) ? candidate : best
        ).clone();
        up = fallback.addScaledVector(direction, -fallback.dot(direction)).normalize();
      }}
      camera.up.copy(up);
      controls.target.set(0, 0, 0);
      camera.lookAt(controls.target);
      controls.update();
      markActiveView(name);
    }}
    function fitView() {{
      camera.zoom = 1;
      controls.target.set(0, 0, 0);
      camera.updateProjectionMatrix();
      controls.update();
    }}
    document.querySelectorAll("[data-view]").forEach((button) => {{
      button.addEventListener("click", () => setView(button.dataset.view));
    }});
    els.fitView.addEventListener("click", fitView);
    els.showCell.addEventListener("change", () => {{
      grid.visible = els.showCell.checked && !data.indexed_geometry;
      cellLines.visible = els.showCell.checked && data.indexed_geometry;
    }});
    renderer.domElement.addEventListener("dblclick", () => {{
      setView("iso");
      fitView();
    }});
    let modifierDrag = null;
    renderer.domElement.addEventListener("pointerdown", (event) => {{
      const dragMode = event.shiftKey ? "roll" : event.altKey ? "vertical" : event.ctrlKey ? "horizontal" : null;
      if (!dragMode || event.button !== 0) return;
      modifierDrag = {{ mode: dragMode, x: event.clientX, y: event.clientY }};
      controls.enabled = false;
      renderer.domElement.setPointerCapture?.(event.pointerId);
      event.preventDefault();
      event.stopImmediatePropagation();
    }}, true);
    window.addEventListener("pointermove", (event) => {{
      if (!modifierDrag) return;
      const dx = event.clientX - modifierDrag.x;
      const dy = event.clientY - modifierDrag.y;
      const rotationSpeed = 0.006;
      modifierDrag.x = event.clientX;
      modifierDrag.y = event.clientY;
      const offset = camera.position.clone().sub(controls.target);
      if (modifierDrag.mode === "roll") {{
        const viewAxis = controls.target.clone().sub(camera.position).normalize();
        camera.up.applyAxisAngle(viewAxis, -dx * rotationSpeed).normalize();
      }} else if (modifierDrag.mode === "vertical") {{
        const viewDirection = controls.target.clone().sub(camera.position).normalize();
        const rightAxis = viewDirection.clone().cross(camera.up).normalize();
        offset.applyAxisAngle(rightAxis, -dy * rotationSpeed);
        camera.up.applyAxisAngle(rightAxis, -dy * rotationSpeed).normalize();
        camera.position.copy(controls.target).add(offset);
      }} else {{
        const verticalAxis = camera.up.clone().normalize();
        offset.applyAxisAngle(verticalAxis, -dx * rotationSpeed);
        camera.up.applyAxisAngle(verticalAxis, -dx * rotationSpeed).normalize();
        camera.position.copy(controls.target).add(offset);
      }}
      camera.lookAt(controls.target);
      markActiveView("");
      event.preventDefault();
    }}, true);
    const endModifierDrag = () => {{
      if (!modifierDrag) return;
      modifierDrag = null;
      controls.enabled = true;
      controls.update();
    }};
    window.addEventListener("pointerup", endModifierDrag, true);
    window.addEventListener("pointercancel", endModifierDrag, true);
    document.addEventListener("keydown", (event) => {{
      const tag = event.target?.tagName?.toLowerCase();
      if (["input", "select", "textarea"].includes(tag)) return;
      const key = event.key.toLowerCase();
      if (["a", "b", "c"].includes(key)) setView(key);
      if (key === "i") setView("iso");
      if (key === "f") fitView();
      if (key === "r") {{ setView("iso"); fitView(); }}
      if (key === "g") {{
        els.showCell.checked = !els.showCell.checked;
        els.showCell.dispatchEvent(new Event("change"));
      }}
    }});

    function colorFor(value, minValue, maxValue) {{
      const t = Math.max(0, Math.min(1, (value - minValue) / Math.max(maxValue - minValue, 1e-6)));
      return [0.19 + 0.81 * t, 0.52 + 0.46 * Math.sqrt(t), 1.0 - 0.55 * t];
    }}
    function visualUniforms() {{
      return {{
        uPointSize: {{ value: Number(els.pointSize.value) }},
        uDisplayMode: {{ value: els.displayMode.value === "gaussian" ? 1.0 : 0.0 }},
        uBrightness: {{ value: Number(els.brightness.value) }},
        uContrast: {{ value: Number(els.contrast.value) }},
        uGamma: {{ value: Number(els.gamma.value) }}
      }};
    }}
    function updateVisualUniforms() {{
      if (!pointMaterial) return;
      pointMaterial.uniforms.uPointSize.value = Number(els.pointSize.value);
      pointMaterial.uniforms.uDisplayMode.value = els.displayMode.value === "gaussian" ? 1.0 : 0.0;
      pointMaterial.uniforms.uBrightness.value = Number(els.brightness.value);
      pointMaterial.uniforms.uContrast.value = Number(els.contrast.value);
      pointMaterial.uniforms.uGamma.value = Number(els.gamma.value);
    }}
    function makePointMaterial() {{
      return new THREE.ShaderMaterial({{
        transparent: true,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
        uniforms: visualUniforms(),
        vertexShader: `
          attribute vec3 color;
          attribute float intensityNorm;
          varying vec3 vColor;
          varying float vIntensity;
          uniform float uPointSize;
          void main() {{
            vColor = color;
            vIntensity = intensityNorm;
            vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
            gl_Position = projectionMatrix * mvPosition;
            gl_PointSize = uPointSize;
          }}
        `,
        fragmentShader: `
          varying vec3 vColor;
          varying float vIntensity;
          uniform float uDisplayMode;
          uniform float uBrightness;
          uniform float uContrast;
          uniform float uGamma;
          void main() {{
            vec2 centered = gl_PointCoord - vec2(0.5);
            float radius = length(centered) * 2.0;
            if (radius > 1.0) discard;
            float shape = mix(1.0, exp(-radius * radius * 4.5), step(0.5, uDisplayMode));
            float corrected = pow(clamp(vIntensity, 0.0, 1.0), 1.0 / max(uGamma, 0.001));
            corrected = (corrected - 0.5) * uContrast + 0.5;
            corrected = clamp(corrected * uBrightness, 0.0, 3.0);
            vec3 color = vColor * corrected;
            gl_FragColor = vec4(color, clamp(shape * corrected, 0.0, 1.0));
          }}
        `
      }});
    }}
    function niceScaleLength(target) {{
      if (!Number.isFinite(target) || target <= 0) return 1;
      const exponent = Math.floor(Math.log10(target));
      const base = Math.pow(10, exponent);
      const candidates = [1, 2, 5, 10].map((value) => value * base);
      let chosen = candidates[0];
      for (const candidate of candidates) {{
        if (candidate <= target) chosen = candidate;
      }}
      return chosen;
    }}
    function formatScaleValue(value) {{
      if (value >= 1) return value.toFixed(value >= 10 ? 0 : 1);
      if (value >= 0.1) return value.toFixed(2).replace(/0+$/, "").replace(/\\.$/, "");
      return value.toExponential(1);
    }}
    function updateScaleBar(width, height) {{
      const visibleWidth = (camera.right - camera.left) / Math.max(camera.zoom, 1e-6);
      const targetLength = visibleWidth * 0.18;
      const scaleLength = niceScaleLength(targetLength);
      const pixelWidth = Math.max(36, Math.min(width * 0.32, (scaleLength / visibleWidth) * width));
      els.scaleBarLine.style.width = `${{pixelWidth.toFixed(1)}}px`;
      els.scaleBarLabel.textContent = `${{formatScaleValue(scaleLength)}} ${{units}}`;
      const detector = data.detector ?? {{}};
      const dimensions = detector.dimensions_px ?? [516, 516];
      const pixelSize = detector.reciprocal_pixel_per_angstrom;
      const pixelText = Number.isFinite(pixelSize) ? pixelSize.toPrecision(6) : "unknown";
      els.scaleBarMeta.textContent =
        data.indexed_geometry
          ? `indexed | cell ${{els.cell.textContent}}`
          : `Pixelsize ${{pixelText}} A^-1/px, ${{dimensions[0]}}x${{dimensions[1]}} px`;
    }}
    function currentRows() {{
      const rows = mode === "peaks" ? data.peaks : data.observations;
      const intensities = rows.map((row) => row.intensity);
      const maxI = Math.max(...intensities, 1);
      const minI = Math.min(...intensities, 0);
      const threshold = Number(els.threshold.value) / 100 * maxI;
      const frameMin = Number(els.frameMin.value);
      const frameMax = Number(els.frameMax.value);
      return rows.filter((row) => {{
        if (row.intensity < threshold) return false;
        const lo = row.frame ?? row.frame_min ?? data.frame_range[0];
        const hi = row.frame ?? row.frame_max ?? data.frame_range[1];
        return hi >= frameMin && lo <= frameMax;
      }}).map((row) => [row, minI, maxI]);
    }}
    function rebuildPoints() {{
      if (points) {{
        scene.remove(points);
        points.geometry.dispose();
        points.material.dispose();
        pointMaterial = null;
      }}
      const rows = currentRows();
      const positions = new Float32Array(rows.length * 3);
      const colors = new Float32Array(rows.length * 3);
      const intensityNorm = new Float32Array(rows.length);
      rows.forEach(([row, minI, maxI], i) => {{
        positions.set(row.q, i * 3);
        colors.set(colorFor(row.intensity, minI, maxI), i * 3);
        const normalized = Math.max(0, Math.min(1, (row.intensity - minI) / Math.max(maxI - minI, 1e-6)));
        intensityNorm[i] = 0.18 + 0.82 * normalized;
      }});
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
      geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
      geometry.setAttribute("intensityNorm", new THREE.BufferAttribute(intensityNorm, 1));
      pointMaterial = makePointMaterial();
      points = new THREE.Points(geometry, pointMaterial);
      scene.add(points);
      els.shown.textContent = rows.length;
    }}
    ["threshold", "pointSize", "frameMin", "frameMax"].forEach((id) => {{
      els[id].addEventListener("input", rebuildPoints);
    }});
    ["displayMode", "brightness", "contrast", "gamma"].forEach((id) => {{
      els[id].addEventListener("input", updateVisualUniforms);
      els[id].addEventListener("change", updateVisualUniforms);
    }});
    function resize() {{
      const width = canvas.clientWidth || window.innerWidth;
      const height = canvas.clientHeight || window.innerHeight;
      const aspect = width / Math.max(height, 1);
      camera.left = -viewSize * aspect / 2;
      camera.right = viewSize * aspect / 2;
      camera.top = viewSize / 2;
      camera.bottom = -viewSize / 2;
      camera.updateProjectionMatrix();
      renderer.setSize(width, height, false);
      updateScaleBar(width, height);
    }}
    window.addEventListener("resize", resize);
    function animate() {{
      resize();
      controls.update();
      renderer.render(scene, camera);
      requestAnimationFrame(animate);
    }}
    setView("iso");
    rebuildPoints();
    animate();
  </script>
</body>
</html>
"""
    index_path = out_dir / "index.html"
    index_path.write_text(html, encoding="utf-8")
    return index_path
