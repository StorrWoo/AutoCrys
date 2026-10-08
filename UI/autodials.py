"""Independent AutoDials workflow panel; all Tk calls stay on the UI thread."""
import json
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
from AutoDials import service
from AutoDials.ui_support import local_path, native_path, parse_cell, probe_dials


class AutoDialsTab(ttk.Frame):
    def __init__(self, parent, owner):
        super().__init__(parent, padding=10)
        self.owner = owner
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.active_result = None
        self.hca = None
        self.datasets = []
        self.root_var = tk.StringVar(value=str(owner.data_root))
        self.python_var = tk.StringVar(value=service.default_python())
        self.cell_var = tk.StringVar()
        self.sg_var = tk.StringVar(value="P1")
        self.res_var = tk.StringVar(value="1.0")
        self.xds_var = tk.BooleanVar(value=False)
        self.method_var = tk.StringVar(value="unit-cell")
        self.cutoff_var = tk.StringVar(value="0.1")
        self.name_var = tk.StringVar(value="merged")
        self.status_var = tk.StringVar(value="Select datasets; saved settings are used for batch processing.")
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        controls = ttk.Frame(self)
        controls.grid(row=0, column=0, sticky="ns", padx=(0, 10))
        controls.rowconfigure(0, weight=1)
        canvas = tk.Canvas(controls, width=325, highlightthickness=0)
        canvas.grid(row=0, column=0, sticky="ns")
        scrollbar = ttk.Scrollbar(controls, orient="vertical", command=canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scrollbar.set)
        left = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=left, anchor="nw", width=325)
        left.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        right = ttk.Frame(self)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=3)
        right.rowconfigure(1, weight=2)
        right.rowconfigure(2, weight=3)
        right.rowconfigure(3, weight=2)
        self.actions = []
        def entry(label, var):
            ttk.Label(left, text=label, wraplength=320).pack(anchor="w", pady=(7, 0))
            ttk.Entry(left, textvariable=var, width=38).pack(fill="x")
        def button(label, command):
            b = ttk.Button(left, text=label, command=lambda: self.guard(command))
            b.pack(fill="x", pady=(4, 0))
            self.actions.append(b)
        entry("Working directory", self.root_var)
        button("Browse / Scan datasets", self.browse)
        button("Refresh", self.scan)
        button("Select all", lambda: self.tree.selection_set(self.tree.get_children()))
        entry("DIALS Python", self.python_var)
        button("Check DIALS runtime", self.check_runtime)
        entry("Target cell: a b c alpha beta gamma (optional)", self.cell_var)
        entry("Space group (number or symbol)", self.sg_var)
        entry("Resolution limit d_min (angstrom)", self.res_var)
        ttk.Checkbutton(left, text="Use each dataset's XDS geometry", variable=self.xds_var).pack(anchor="w", pady=6)
        button("Set Cell / SG / Res for selected", self.save)
        button("Process & Convert selected", self.process)
        ttk.Separator(left).pack(fill="x", pady=10)
        ttk.Label(left, text="HCA method").pack(anchor="w")
        ttk.Combobox(left, textvariable=self.method_var, values=("unit-cell", "cc1"), state="readonly").pack(fill="x")
        button("HCA Run selected", self.cluster)
        entry("Cutoff (or click dendrogram)", self.cutoff_var)
        button("Show clusters at cutoff", self.show_clusters)
        entry("Merged output name", self.name_var)
        button("Merge & Convert selected", lambda: self.merge(False))
        button("Merge clusters at cutoff", lambda: self.merge(True))
        footer = ttk.Frame(self)
        footer.grid(row=1, column=0, columnspan=2, sticky="ew", pady=5)
        for label, fn in (("Send result to AutoR3D", self.send_r3d), ("Send result to AutoSolve", self.send_solve)):
            b = ttk.Button(footer, text=label, command=lambda f=fn: self.guard(f))
            b.pack(side="left", padx=(0, 8))
            self.actions.append(b)
        self.stop_button = ttk.Button(footer, text="Stop", command=self.stop, state="disabled")
        self.stop_button.pack(side="right")
        style = ttk.Style(self)
        style.configure("AutoDials.Treeview", background="#141e2d", fieldbackground="#141e2d", foreground="#e3ecff", rowheight=24)
        style.map("AutoDials.Treeview", background=[("selected", "#315684")], foreground=[("selected", "#ffffff")])
        self.tree = ttk.Treeview(right, columns=("state", "sg", "cell", "res"), selectmode="extended", height=8, style="AutoDials.Treeview")
        for col, title, width in (("#0", "Dataset", 145), ("state", "Result", 100), ("sg", "SG", 65), ("cell", "Final cell / target", 290), ("res", "d_min", 65)):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, minwidth=50, stretch=col in ("#0", "cell"))
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(right, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self.selected_changed)
        run_box = ttk.Frame(right)
        run_box.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
        run_box.columnconfigure(0, weight=1)
        run_box.rowconfigure(1, weight=1)
        ttk.Label(run_box, text="Successful runs for the selected dataset (HCA/Merge uses the checked runs)").grid(row=0, column=0, sticky="w")
        self.runs = ttk.Treeview(run_box, columns=("complete", "cell", "sg", "dials", "when"), show="tree headings", height=4, selectmode="none", style="AutoDials.Treeview")
        for col, title, width in (("#0", "Select", 70), ("complete", "Status", 90), ("cell", "Final cell", 250), ("sg", "SG", 60), ("dials", "DIALS", 90), ("when", "When", 130)):
            self.runs.heading(col, text=title)
            self.runs.column(col, width=width, minwidth=50, stretch=col in ("#0", "cell"))
        self.runs.grid(row=1, column=0, columnspan=2, sticky="nsew")
        self.runs_run_ids = {}
        self.runs.bind("<Button-1>", self._toggle_run)
        self.plot_frame = ttk.Frame(right)
        self.plot_frame.grid(row=2, column=0, sticky="nsew", pady=8)
        self.text = ScrolledText(right, height=10, wrap="word", state="disabled", background="#141e2d", foreground="#e3ecff", insertbackground="white", font=(owner._ui_mono_family, 10))
        self.text.grid(row=3, column=0, sticky="nsew")
        ttk.Label(self, textvariable=self.status_var, wraplength=1000).grid(row=2, column=0, columnspan=2, sticky="ew", pady=5)
        for widget in (canvas, left, *left.winfo_children()):
            widget.bind("<Button-4>", lambda e: canvas.yview_scroll(-3, "units"))
            widget.bind("<Button-5>", lambda e: canvas.yview_scroll(3, "units"))
        self.bind("<Destroy>", self.destroyed, add=True)
        self.poll_id = self.after(100, self.poll)
        self.scan()

    def guard(self, fn):
        try:
            fn()
        except Exception as exc:
            self.log(str(exc))
            messagebox.showerror("AutoDials", str(exc), parent=self)

    def log(self, text):
        self.text.configure(state="normal")
        self.text.insert("end", str(text).rstrip() + "\n")
        self.text.see("end")
        # Bound the live display; full subprocess output is saved in run.log.
        if int(self.text.index("end-1c").split(".")[0]) > 1600:
            self.text.delete("1.0", "300.0")
        self.text.configure(state="disabled")
        self.owner._console_log("[AutoDials] " + str(text).rstrip())

    def browse(self):
        path = filedialog.askdirectory(parent=self, initialdir=self.root_var.get())
        if path:
            self.root_var.set(path)
            self.hca = None
            self.scan()

    def selected(self):
        return [self.datasets[int(i)] for i in self.tree.selection()]

    def require_selected(self, minimum=1):
        datasets = self.selected()
        if len(datasets) < minimum:
            raise ValueError(f"Select at least {minimum} dataset(s)")
        return datasets

    def scan(self):
        self.ignore_selection = True
        selected = set(self.selected())
        self.datasets = service.discover(local_path(self.root_var.get()))
        self.tree.delete(*self.tree.get_children())
        for i, path in enumerate(self.datasets):
            settings = service.load_settings(path)
            run = service.latest_run(path)
            result = service.read_result(run) if run else {}
            cell = " ".join(f"{v:.3f}" for v in result.get("cell", [])) or settings.get("cell", "auto")
            label = str(path.relative_to(local_path(self.root_var.get())))
            self.tree.insert("", "end", iid=str(i), text=path.name if label == "." else label, values=("success" if run else "ready", result.get("space_group", settings.get("space_group", "P1")), cell, settings.get("d_min", "1.0")))
        self.tree.selection_set([str(i) for i, p in enumerate(self.datasets) if p in selected])
        self.status_var.set(f"{len(self.datasets)} datasets. Ctrl / Shift selects multiple rows.")
        self.after_idle(lambda: setattr(self, "ignore_selection", False))

    def check_runtime(self):
        python = local_path(self.python_var.get())
        info = probe_dials(python)
        self.log("DIALS runtime: " + json.dumps(info, ensure_ascii=False))

    def settings(self):
        parse_cell(self.cell_var.get())
        if float(self.res_var.get()) <= 0:
            raise ValueError("Resolution must be positive")
        return dict(cell=self.cell_var.get().strip(), space_group=self.sg_var.get().strip() or "P1", d_min=self.res_var.get().strip(), use_xds=self.xds_var.get())

    def save(self):
        paths = self.require_selected()
        service.save_settings(paths, self.settings())
        self.scan()
        self.log(f"Settings saved for {len(paths)} datasets; final refined cell may differ from target.")

    def selected_changed(self, _event=None):
        if self.busy or getattr(self, "ignore_selection", False):
            return
        paths = self.selected()
        if not paths:
            self.active_result = None
            return
        settings = service.load_settings(paths[0])
        for var, key, default in ((self.cell_var, "cell", ""), (self.sg_var, "space_group", "P1"), (self.res_var, "d_min", "1.0"), (self.xds_var, "use_xds", False)):
            var.set(settings.get(key, default))
        self.refresh_runs(paths)
        run = service.latest_run(paths[0])
        self.active_result = service.read_result(run) if run else None
        if len(paths) == 1 and self.active_result:
            self.show_result(self.active_result)

    def refresh_runs(self, datasets):
        self.runs.delete(*self.runs.get_children())
        self.runs_run_ids = {}
        for dataset in datasets:
            for i, info in enumerate(service.run_choices(dataset)):
                when = ""
                try:
                    from datetime import datetime
                    when = datetime.fromtimestamp(info["run"].stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                except OSError:
                    pass
                cell = " ".join(f"{v:.3f}" for v in info["cell"]) if info.get("cell") else "?"
                status = "complete" if info["complete"] else "incomplete"
                dials = info.get("dials_version") or "legacy"
                # Auto-check the newest run of each dataset so the default stays
                # reproducible; older runs stay available but unchecked.
                node = self.runs.insert("", "end", text="\u2611" if i == 0 else "\u2610",
                                        values=(status, cell, info.get("space_group") or "?", dials, when))
                self.runs_run_ids[node] = info["id"]

    def _toggle_run(self, event):
        node = self.runs.identify_row(event.y)
        if not node:
            return
        self.runs.item(node, text="\u2610" if self.runs.item(node, "text") == "\u2611" else "\u2611")

    def checked_runs(self, minimum=2):
        checked = [run_id for node, run_id in self.runs_run_ids.items() if self.runs.item(node, "text") == "\u2611"]
        runs = {info["id"]: info["run"] for dataset in self.selected() for info in service.run_choices(dataset)}
        chosen = [runs[run_id] for run_id in checked if run_id in runs]
        if len(chosen) < minimum:
            raise ValueError(f"Check at least {minimum} successful runs in the run list")
        return chosen

    def launch(self, label, task):
        if self.busy:
            raise ValueError("An AutoDials task is already running")
        self.busy = True
        self.cancel.clear()
        for b in self.actions:
            b.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set(label)
        def worker():
            try:
                result = task()
                self.events.put(("complete", result))
            except Exception as exc:
                self.events.put(("failure", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def emit(self, event):
        self.events.put(("event", event))

    def process(self):
        paths = self.require_selected()
        python = str(local_path(self.python_var.get()))
        probe_dials(python)
        # Single-row processing uses the form. Batch uses each saved setting;
        # Set Cell / SG / Res applies one form to all selected rows explicitly.
        current = self.settings()
        if len(paths) == 1:
            service.save_settings(paths, current)
        settings = [(p, service.load_settings(p) or current) for p in paths]
        def task():
            errors = []
            results = []
            for i, (path, config) in enumerate(settings):
                if self.cancel.is_set():
                    break
                self.emit({"kind": "log", "text": f"Processing {i+1}/{len(paths)}: {path.name}"})
                try:
                    result = service.process_dataset(path, config, python, self.emit, self.cancel)
                    results.append(result)
                except Exception as exc:
                    errors.append(f"{path.name}: {exc}")
                    self.emit({"kind": "log", "text": errors[-1]})
            return dict(action="batch", results=results, errors=errors, cancelled=self.cancel.is_set())
        self.launch("Processing datasets...", task)

    def cluster(self):
        runs = self.checked_runs(2)
        root = local_path(self.root_var.get())
        folder, command = service.multi_job(root, runs, local_path(self.python_var.get()), "hca", {"method": self.method_var.get()})
        def task():
            result = service.execute(command, folder, self.emit, self.cancel)
            result["datasets"] = [str(r.parent.parent) for r in runs]
            return result
        self.launch("HCA running...", task)

    def clusters(self):
        if not self.hca:
            raise ValueError("Run HCA first")
        from scipy.cluster.hierarchy import fcluster
        method = self.method_var.get()
        if method not in self.hca["methods"]:
            raise ValueError("Run HCA for the selected method first")
        cutoff = float(self.cutoff_var.get())
        if not 0 <= cutoff < float("inf"):
            raise ValueError("Cutoff must be finite and nonnegative")
        groups = fcluster(self.hca["methods"][method]["linkage"], cutoff, criterion="distance")
        paths = list(map(Path, self.hca["datasets"]))
        return [[p for p, g in zip(paths, groups) if g == group] for group in sorted(set(groups))]

    def show_clusters(self):
        groups = self.clusters()
        self.log("\n".join(f"Cluster {i+1}: " + ", ".join(p.name for p in group) for i, group in enumerate(groups)))
        self.plot()

    def plot(self):
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        from scipy.cluster.hierarchy import dendrogram
        for w in self.plot_frame.winfo_children():
            w.destroy()
        method = self.method_var.get()
        if method not in self.hca["methods"]:
            method = next(iter(self.hca["methods"]))
        self.method_var.set(method)
        fig = Figure(figsize=(7, 3), dpi=100, layout="tight")
        ax = fig.add_subplot()
        dendrogram(self.hca["methods"][method]["linkage"], labels=self.hca["labels"], ax=ax, leaf_rotation=30, leaf_font_size=8)
        ax.set_ylabel("Distance")
        ax.set_title("HCA: " + method + " (click to set cutoff)", fontsize=10)
        try:
            ax.axhline(float(self.cutoff_var.get()), color="red", linestyle="--", linewidth=1)
        except ValueError:
            pass
        canvas = FigureCanvasTkAgg(fig, self.plot_frame)
        canvas.get_tk_widget().pack(fill="both", expand=True)
        def clicked(event):
            if event.inaxes is ax and event.ydata is not None:
                self.cutoff_var.set(f"{max(0, event.ydata):.5f}")
                self.guard(self.show_clusters)
        canvas.mpl_connect("button_press_event", clicked)
        canvas.draw()

    def merge(self, clusters):
        root, python, name = local_path(self.root_var.get()), local_path(self.python_var.get()), self.name_var.get().strip()
        if clusters:
            run_map = {info["run"].parent.parent.name: info["run"]
                       for dataset in self.selected() for info in service.run_choices(dataset)}
            groups = []
            for group in self.clusters():
                chosen = [run_map[p.name] for p in group if p.name in run_map]
                if len(chosen) >= 2:
                    groups.append(chosen)
        else:
            groups = [self.checked_runs(2)]
        if not groups:
            raise ValueError("No clusters with at least two checked runs")
        jobs = [service.multi_job(root, group, python, "merge", {"name": name + (f"_cluster_{i+1}" if clusters else "")}) for i, group in enumerate(groups)]
        def task():
            results = []
            for folder, command in jobs:
                if self.cancel.is_set():
                    break
                results.append(service.execute(command, folder, self.emit, self.cancel))
            return dict(action="batch", results=results, errors=[], cancelled=self.cancel.is_set())
        self.launch("Scaling / merging selected datasets...", task)

    def show_result(self, result):
        self.active_result = result
        self.log(json.dumps(result, indent=2, ensure_ascii=False))

    def send_r3d(self):
        result = self.active_result
        if not result:
            raise ValueError("Select a successful individual result")
        run = Path(result["run_directory"])
        if not (run / "work/integrated.expt").exists():
            raise ValueError("AutoR3D needs an individual acquisition, not a merged reflection file")
        if (run / "reference.json").is_file():
            self.owner.dials_to_r3d(run.parent.parent, run)
            return
        python = local_path(self.python_var.get())
        config = run / "reference_request.json"
        config.write_text(json.dumps({"action": "reference", "run_directory": native_path(run, python)}), encoding="utf-8")
        command = [str(python), "-u", native_path(Path(service.__file__).with_name("worker.py"), python), native_path(config, python)]
        def task():
            service.execute(command, run, self.emit, self.cancel)
            return {"action": "send_r3d", "run_directory": str(run)}
        self.launch("Exporting native DIALS geometry...", task)

    def send_solve(self):
        if not self.active_result or not Path(self.active_result.get("hkl", "")).is_file():
            raise ValueError("Select a successful result with a SHELX HKL file")
        status = self.active_result.get("intensity_status")
        if status != "scaled" and not messagebox.askyesno(
                "Unscaled intensities",
                "This result is not scaled/merged (intensity status: %s).\n"
                "Solving with unscaled single-dataset intensities is for expert use only.\n\n"
                "Send it to AutoSolve anyway?" % (status or "unknown"), parent=self):
            return
        self.owner.dials_to_solve(Path(self.active_result["hkl"]))

    def stop(self):
        self.cancel.set()
        self.status_var.set("Stopping DIALS...")

    def destroyed(self, event):
        if event.widget is self:
            self.cancel.set()
            self.after_cancel(self.poll_id)

    def poll(self):
        # Limit each UI tick so verbose DIALS output cannot freeze the window.
        for _ in range(120):
            try:
                kind, data = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "event":
                if data.get("kind") == "log":
                    self.log(data["text"])
                elif data.get("kind") == "stage":
                    self.status_var.set(str(data.get("stage", "Running")))
                continue
            self.busy = False
            for b in self.actions:
                b.configure(state="normal")
            self.stop_button.configure(state="disabled")
            self.scan()
            if kind == "failure":
                self.log(data)
                self.status_var.set("Stopped" if self.cancel.is_set() else "Failed — see run.log")
                continue
            if data.get("action") == "hca":
                self.hca = data
                self.plot()
                self.show_clusters()
            elif data.get("action") == "send_r3d":
                run = Path(data["run_directory"])
                self.owner.dials_to_r3d(run.parent.parent, run)
            elif data.get("action") == "batch":
                for result in data["results"]:
                    self.show_result(result)
                self.log(f"Finished: {len(data['results'])} succeeded; {len(data['errors'])} failed" + ("; cancelled" if data["cancelled"] else ""))
            self.status_var.set("Stopped" if self.cancel.is_set() else "Finished; select a result or send the displayed result to AutoSolve.")
        self.poll_id = self.after(100, self.poll)
