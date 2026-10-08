"""Standalone AutoDials UI for single-dataset processing and multi-run merging."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from .ui_support import create_job, create_merge_job, decode_event, default_python, discover_merge_runs, find_xparm, local_path, validate_inputs
except ImportError:
    from ui_support import create_job, create_merge_job, decode_event, default_python, discover_merge_runs, find_xparm, local_path, validate_inputs


def default_data_root():
    candidate = Path(__file__).resolve().parent.parent / "Data"
    return candidate if candidate.is_dir() else Path.cwd()


class DialsMiniUI:
    def __init__(self, root, dataset=""):
        self.root = root
        self.root.title("AutoDials")
        self.root.geometry("980x820")
        self.root.minsize(780, 680)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TLabel", font=("sans-serif", 11))
        style.configure("TButton", padding=(12, 6), font=("sans-serif", 11))
        style.configure("TEntry", padding=5, font=("sans-serif", 11))
        style.configure("TCheckbutton", font=("sans-serif", 11))
        self.dataset = tk.StringVar(value=dataset)
        self.python = tk.StringVar(value=default_python())
        self.cell = tk.StringVar()
        self.sg = tk.StringVar(value="P1")
        self.dmin = tk.StringVar(value="1.0")
        self.use_xds = tk.BooleanVar(value=False)
        self.xparm = tk.StringVar()
        self.merge_root = tk.StringVar(value=str(Path(dataset).parent) if dataset else str(default_data_root()))
        self.merge_name = tk.StringVar(value="merged")
        self.merge_runs = {}
        self.status = tk.StringVar(value="选择一个包含 cRED2 参数文件和 diff 图像的实验文件夹")
        self.events = queue.Queue()
        self.running = False
        self.run_directory = None
        self.result = None
        self.close_when_done = False
        self._after = None
        self._controls = []
        self._build()
        if dataset:
            self._detect_geometry()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._after = root.after(100, self._poll)

    def _build(self):
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(10, weight=1)
        ttk.Label(outer, text="3DED → AutoDials", font=("sans-serif", 17, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))
        self._entry(outer, 1, "实验文件夹", self.dataset, self._browse_dataset)
        self._entry(outer, 2, "DIALS Python", self.python, self._browse_python)
        self._entry(outer, 3, "目标晶胞", self.cell)
        ttk.Label(outer, text="a b c α β γ；留空自动索引，填写后仍允许精修", foreground="#596575").grid(row=4, column=1, columnspan=2, sticky="w", pady=(0, 6))
        options = ttk.Frame(outer)
        options.grid(row=5, column=0, columnspan=3, sticky="ew", pady=4)
        ttk.Label(options, text="空间群").pack(side="left")
        sg_entry = ttk.Entry(options, textvariable=self.sg, width=14)
        sg_entry.pack(side="left", padx=(12, 8))
        ttk.Label(options, text="名称 / 1–230；默认 P1", foreground="#596575").pack(side="left")
        ttk.Label(options, text="分辨率下限 Å").pack(side="left", padx=(22, 8))
        resolution = ttk.Entry(options, textvariable=self.dmin, width=6)
        resolution.pack(side="left")
        self._controls.extend([sg_entry, resolution])
        geometry = ttk.Frame(outer)
        geometry.grid(row=6, column=0, columnspan=3, sticky="ew", pady=6)
        geometry.columnconfigure(1, weight=1)
        toggle = ttk.Checkbutton(geometry, text="使用 XDS 几何", variable=self.use_xds, command=self._toggle_geometry)
        toggle.grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.geometry_entry = ttk.Entry(geometry, textvariable=self.xparm)
        self.geometry_entry.grid(row=0, column=1, sticky="ew")
        self.geometry_browse = ttk.Button(geometry, text="选择…", command=self._browse_geometry)
        self.geometry_browse.grid(row=0, column=2, padx=(8, 0))
        self._controls.extend([toggle, self.geometry_entry, self.geometry_browse])
        self._toggle_geometry()
        actions = ttk.Frame(outer)
        actions.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(8, 8))
        self.run_button = ttk.Button(actions, text="运行 DIALS", command=self.start)
        self.run_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="停止", command=self.cancel, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        self.open_button = ttk.Button(actions, text="打开结果目录", command=self.open_results, state="disabled")
        self.open_button.pack(side="right")
        self.progress = ttk.Progressbar(actions, maximum=8)
        self.progress.pack(side="left", fill="x", expand=True, padx=12)
        merge = ttk.LabelFrame(outer, text="多数据集合并（combine_experiments → cosym → scale → merge）", padding=8)
        merge.grid(row=8, column=0, columnspan=3, sticky="nsew", pady=(4, 8))
        merge.columnconfigure(1, weight=1)
        merge.rowconfigure(2, weight=1)
        ttk.Label(merge, text="数据根目录").grid(row=0, column=0, sticky="w", padx=(0, 8))
        merge_root_entry = ttk.Entry(merge, textvariable=self.merge_root)
        merge_root_entry.grid(row=0, column=1, sticky="ew")
        merge_browse = ttk.Button(merge, text="选择…", command=self._browse_merge_root)
        merge_browse.grid(row=0, column=2, padx=(8, 0))
        ttk.Label(merge, text="结果名").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(6, 0))
        merge_name_entry = ttk.Entry(merge, textvariable=self.merge_name, width=20)
        merge_name_entry.grid(row=1, column=1, sticky="w", pady=(6, 0))
        merge_scan = ttk.Button(merge, text="扫描成功 run", command=self.scan_merge_runs)
        merge_scan.grid(row=1, column=2, padx=(8, 0), pady=(6, 0))
        self.merge_tree = ttk.Treeview(merge, columns=("dataset", "run", "sg", "cell", "dials"), show="headings", selectmode="extended", height=5)
        for column, title, width in (("dataset", "数据集", 180), ("run", "Run", 210), ("sg", "空间群", 90), ("cell", "最终晶胞", 290), ("dials", "DIALS", 70)):
            self.merge_tree.heading(column, text=title)
            self.merge_tree.column(column, width=width, minwidth=55, stretch=column in ("dataset", "cell"))
        self.merge_tree.grid(row=2, column=0, columnspan=3, sticky="nsew", pady=(8, 0))
        merge_scroll = ttk.Scrollbar(merge, orient="vertical", command=self.merge_tree.yview)
        merge_scroll.grid(row=2, column=3, sticky="ns", pady=(8, 0))
        self.merge_tree.configure(yscrollcommand=merge_scroll.set)
        self.merge_button = ttk.Button(merge, text="合并所选 run", command=self.start_merge)
        self.merge_button.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self._controls.extend([merge_root_entry, merge_browse, merge_name_entry, merge_scan, self.merge_button])
        ttk.Label(outer, textvariable=self.status, wraplength=900).grid(row=9, column=0, columnspan=3, sticky="w", pady=(0, 8))
        logs = ttk.Frame(outer)
        logs.grid(row=10, column=0, columnspan=3, sticky="nsew")
        self.log = tk.Text(logs, height=10, wrap="word", state="disabled", bg="#111827", fg="#dce7f5", insertbackground="white", font=("monospace", 10), relief="flat", padx=10, pady=10)
        scrollbar = ttk.Scrollbar(logs, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        ttk.Label(outer, text="单数据集输出在 实验文件夹/AutoDials/run_*；合并输出在 数据根目录/AutoDials/merge_*", foreground="#596575", wraplength=900).grid(row=11, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self._append("就绪。单数据集为未标度强度；合并结果为 scaled 数据。\n")

    def _entry(self, parent, row, label, variable, browse=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, columnspan=1 if browse else 2, sticky="ew", pady=4)
        self._controls.append(entry)
        if browse:
            button = ttk.Button(parent, text="选择…", command=browse)
            button.grid(row=row, column=2, padx=(8, 0), pady=4)
            self._controls.append(button)
        return entry

    def _browse_dataset(self):
        selected = filedialog.askdirectory(parent=self.root, title="选择实验文件夹", initialdir=self.dataset.get() or str(default_data_root()))
        if selected:
            self.dataset.set(selected)
            self._detect_geometry()

    def _detect_geometry(self):
        try:
            found = find_xparm(local_path(self.dataset.get()))
        except (ValueError, OSError):
            found = None
        self.xparm.set(str(found) if found else "")

    def _browse_python(self):
        selected = filedialog.askopenfilename(parent=self.root, title="选择 DIALS 安装中的 Python", initialdir=str(Path(self.python.get()).parent))
        if selected:
            self.python.set(selected)

    def _browse_geometry(self):
        selected = filedialog.askopenfilename(parent=self.root, title="选择 XPARM.XDS / GXPARM.XDS", initialdir=self.dataset.get() or ".", filetypes=[("XDS geometry", "*.XDS"), ("All files", "*")])
        if selected:
            self.xparm.set(selected)

    def _browse_merge_root(self):
        selected = filedialog.askdirectory(parent=self.root, title="选择包含多个数据集的根目录", initialdir=self.merge_root.get() or ".")
        if selected:
            self.merge_root.set(selected)
            self.scan_merge_runs()

    def scan_merge_runs(self):
        try:
            entries = discover_merge_runs(self.merge_root.get())
        except Exception as error:
            messagebox.showerror("扫描 run", str(error), parent=self.root)
            return
        self.merge_tree.delete(*self.merge_tree.get_children())
        self.merge_runs = {}
        for item in entries:
            cell = " ".join(f"{float(value):.3f}" for value in item.get("cell") or [])
            node = self.merge_tree.insert("", "end", values=(item["dataset"].name, item["run_id"], item.get("space_group") or "?", cell or "?", item.get("dials_version") or "legacy"))
            self.merge_runs[node] = item["run"]
        self.status.set(f"找到 {len(entries)} 个成功 run；Ctrl/Shift 选择至少两个后合并")

    def start_merge(self):
        if self.running:
            return
        selected = [self.merge_runs[node] for node in self.merge_tree.selection() if node in self.merge_runs]
        try:
            run, command = create_merge_job(self.merge_root.get(), selected, self.python.get(), self.merge_name.get())
        except Exception as error:
            messagebox.showerror("合并参数", str(error), parent=self.root)
            return
        self._launch(command, run, "正在启动 DIALS 合并…")

    def _toggle_geometry(self):
        enabled = self.use_xds.get() and not self.running
        for widget in (self.geometry_entry, self.geometry_browse):
            widget.configure(state="normal" if enabled else "disabled")

    def _append(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        if int(self.log.index("end-1c").split(".")[0]) > 7000:
            self.log.delete("1.0", "1500.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def start(self):
        if self.running:
            return
        try:
            inputs = validate_inputs(self.dataset.get(), self.python.get(), self.cell.get(), self.sg.get(), self.dmin.get(), self.use_xds.get(), self.xparm.get())
            run, command = create_job(inputs)
        except Exception as error:
            messagebox.showerror("参数检查", str(error), parent=self.root)
            return
        self._launch(command, run, "正在启动 DIALS…")

    def _launch(self, command, run, status):
        self.run_directory = run
        self.result = None
        self.running = True
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self._append(f"结果目录：{run}\n")
        self.status.set(status)
        self.progress["value"] = 0
        self.run_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.open_button.configure(state="normal")
        for widget in self._controls:
            widget.configure(state="disabled")
        threading.Thread(target=self._worker, args=(command, run), daemon=True).start()

    def _worker(self, command, run):
        try:
            kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="replace", text=True, bufsize=1, **kwargs)
            with (run / "run.log").open("w", encoding="utf-8") as log:
                for line in process.stdout:
                    log.write(line)
                    log.flush()
                    event = decode_event(line)
                    self.events.put(("event", event) if event else ("log", line))
            process.stdout.close()
            self.events.put(("exit", process.wait()))
        except Exception as error:
            self.events.put(("log", f"启动失败：{error}\n"))
            self.events.put(("exit", -1))

    def _poll(self):
        chunks = []
        for _ in range(250):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                chunks.append(value)
            elif kind == "event":
                if value["kind"] == "stage":
                    self.status.set(f"{value['current']}/{value['total']}  {value['stage']}")
                    self.progress["value"] = max(0, value["current"] - 1)
                    chunks.append(f"\n—— {value['stage']} ——\n")
                elif value["kind"] == "result":
                    self.result = value
                elif value["kind"] == "error":
                    self.result = value
                    chunks.append(f"\n{value['error']}\n")
            elif kind == "exit":
                if chunks:
                    self._append("".join(chunks))
                    chunks = []
                self._finished(value)
                if self.close_when_done:
                    self.root.destroy()
                    return
        if chunks:
            self._append("".join(chunks))
        self._after = self.root.after(100, self._poll)

    def _finished(self, returncode):
        self.running = False
        self.run_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        for widget in self._controls:
            widget.configure(state="normal")
        self._toggle_geometry()
        if returncode == 0 and self.result and self.result.get("status") == "success":
            r = self.result
            self.progress["value"] = 8
            self.status.set("完成 · MTZ、HKL 和 INS 已输出")
            cell = "  ".join(f"{v:.4f}" for v in r["cell"])
            if r.get("action") == "merge":
                self._append(f"\n========== 合并结果 ==========\n实验：{r['experiment']}\n空间群：{r['space_group']}\n晶胞：{cell}\n输入 run：{', '.join(r.get('run_ids', []))}\nMTZ：{r['mtz_reflections']} 条\nHKL：{r['hkl_reflections']} 条（scaled unmerged HKLF4）\n\n{r['mtz']}\n{r['hkl']}\n{r['ins']}\n")
            else:
                self._append(f"\n========== 处理结果 ==========\n实验：{r['experiment']}\n空间群：{r['space_group']}\n晶胞：{cell}\n图像：{r['frames']} 帧；排除全零帧：{r['excluded_zero_frames']}\n索引：{r['indexed_spots']} / {r['strong_spots']}（{r['indexed_percent']:.2f}%）\n积分反射：{r['integrated_reflections']}\nMTZ：{r['mtz_reflections']} 条\nHKL：{r['hkl_reflections']} 条（SHELX HKLF 4）\n\n{self.run_directory / (r['experiment'] + '.mtz')}\n{self.run_directory / (r['experiment'] + '.hkl')}\n{self.run_directory / (r['experiment'] + '.ins')}\nINS 的晶胞和 LATT/SYMM 来自最终精修结果；元素组成仍需按样品填写。\n\n强度尚未标度；HKL 使用轮廓积分，MTZ 默认同时筛选求和与轮廓积分，条数可能不同。\n")
        elif self.result and self.result.get("status") == "cancelled":
            self.status.set("已停止 · 中间结果和日志已保留")
            self._append("\n任务已停止。\n")
        else:
            self.status.set("失败 · 请查看下方日志")
            self._append(f"\n任务失败（退出码 {returncode}）。完整日志：{self.run_directory / 'run.log'}\n")

    def cancel(self):
        if self.running and self.run_directory:
            (self.run_directory / "cancel.request").write_text("cancel\n", encoding="utf-8")
            self.stop_button.configure(state="disabled")
            self.status.set("正在停止，请稍候…")

    def open_results(self):
        if not self.run_directory:
            return
        try:
            if os.name == "nt":
                os.startfile(self.run_directory)
            elif Path("/mnt/c/Windows/explorer.exe").exists():
                target = subprocess.run(["wslpath", "-w", str(self.run_directory)], capture_output=True, text=True, check=True).stdout.strip()
                subprocess.Popen(["/mnt/c/Windows/explorer.exe", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.Popen(["xdg-open", str(self.run_directory)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as error:
            messagebox.showerror("打开目录", str(error), parent=self.root)

    def close(self):
        if self.running:
            self.close_when_done = True
            self.cancel()
        else:
            if self._after:
                self.root.after_cancel(self._after)
            self.root.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", nargs="?", default="")
    args = parser.parse_args(argv)
    root = tk.Tk()
    DialsMiniUI(root, args.dataset)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
