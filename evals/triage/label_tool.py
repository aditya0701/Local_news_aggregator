"""Small local GUI for labeling evals/triage/candidates.jsonl by hand.

Run with:  python evals/triage/label_tool.py

Shows one candidate at a time (title, content snippet, source, a button to
open the original URL, and the historical stage1_decision_at_the_time for
reference only) with Previous/Next navigation. You pick true_label
(publish/skip) and, for skips, a skip_reason from a dropdown -- nothing is
pre-selected, every choice is yours to make from a blank state.

Labels are written to evals/triage/golden_set.jsonl immediately on save, so
closing the tool mid-session never loses progress -- re-opening it reloads
whatever's already labeled and lets you keep going or revisit earlier calls.

This is a labeling AID only. It never chooses, suggests, or pre-fills a
label -- see finalize_project.md's division-of-labor rule.
"""

import json
import tkinter as tk
import webbrowser
from collections import Counter
from pathlib import Path
from tkinter import messagebox, ttk

CANDIDATES_PATH = Path(__file__).parent / "candidates.jsonl"
GOLDEN_PATH = Path(__file__).parent / "golden_set.jsonl"

SKIP_REASONS = ["job_posting", "listicle", "show_hn", "off_topic", "too_thin"]


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class LabelTool(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("TechDrishti Triage Labeler")
        self.geometry("900x720")

        self.candidates = load_jsonl(CANDIDATES_PATH)
        if not self.candidates:
            messagebox.showerror("No candidates", f"{CANDIDATES_PATH} is empty or missing.")
            self.destroy()
            return

        self.labels: dict[str, dict] = {r["id"]: r for r in load_jsonl(GOLDEN_PATH)}

        self.filter_source = tk.StringVar(value="all")
        self.filter_unlabeled_only = tk.BooleanVar(value=False)
        self.visible_indices: list[int] = []
        self.pos = 0

        self._build_ui()
        self._apply_filter()

    # ------------------------------------------------------------------
    def _build_ui(self):
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")

        ttk.Label(top, text="Source:").pack(side="left")
        source_combo = ttk.Combobox(
            top, textvariable=self.filter_source, state="readonly", width=14,
            values=["all", "rss", "github", "hn_jobs", "manual_listicle"],
        )
        source_combo.pack(side="left", padx=(4, 16))
        source_combo.bind("<<ComboboxSelected>>", lambda e: self._apply_filter())

        ttk.Checkbutton(
            top, text="Unlabeled only", variable=self.filter_unlabeled_only,
            command=self._apply_filter,
        ).pack(side="left")

        self.progress_label = ttk.Label(top, text="", anchor="e")
        self.progress_label.pack(side="right")

        body = ttk.Frame(self, padding=8)
        body.pack(fill="both", expand=True)

        self.pos_label = ttk.Label(body, text="", font=("Segoe UI", 9))
        self.pos_label.pack(anchor="w")

        self.title_label = ttk.Label(
            body, text="", font=("Segoe UI", 13, "bold"), wraplength=850, justify="left"
        )
        self.title_label.pack(anchor="w", pady=(4, 8))

        meta_frame = ttk.Frame(body)
        meta_frame.pack(fill="x")
        self.source_label = ttk.Label(meta_frame, text="", foreground="#555")
        self.source_label.pack(side="left")
        self.history_label = ttk.Label(meta_frame, text="", foreground="#555")
        self.history_label.pack(side="left", padx=(16, 0))

        ttk.Button(body, text="Open source URL in browser", command=self._open_url).pack(
            anchor="w", pady=(6, 6)
        )

        ttk.Label(body, text="Content snippet:").pack(anchor="w")
        snippet_frame = ttk.Frame(body)
        snippet_frame.pack(fill="both", expand=True)
        self.snippet_text = tk.Text(snippet_frame, height=10, wrap="word", state="disabled")
        snippet_scroll = ttk.Scrollbar(snippet_frame, command=self.snippet_text.yview)
        self.snippet_text.configure(yscrollcommand=snippet_scroll.set)
        self.snippet_text.pack(side="left", fill="both", expand=True)
        snippet_scroll.pack(side="right", fill="y")

        label_frame = ttk.LabelFrame(body, text="Your label", padding=10)
        label_frame.pack(fill="x", pady=(12, 0))

        self.true_label = tk.StringVar(value="")
        ttk.Radiobutton(
            label_frame, text="Publish", variable=self.true_label, value="publish",
            command=self._on_label_change,
        ).grid(row=0, column=0, sticky="w", padx=(0, 20))
        ttk.Radiobutton(
            label_frame, text="Skip", variable=self.true_label, value="skip",
            command=self._on_label_change,
        ).grid(row=0, column=1, sticky="w")

        ttk.Label(label_frame, text="Skip reason:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.skip_reason = tk.StringVar(value="")
        self.skip_reason_combo = ttk.Combobox(
            label_frame, textvariable=self.skip_reason, state="disabled", width=20,
            values=SKIP_REASONS,
        )
        self.skip_reason_combo.grid(row=1, column=1, sticky="w", pady=(8, 0))

        nav_frame = ttk.Frame(body)
        nav_frame.pack(fill="x", pady=(16, 0))
        ttk.Button(nav_frame, text="< Previous", command=self._go_previous).pack(side="left")
        ttk.Button(nav_frame, text="Save & Next >", command=self._save_and_next).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(nav_frame, text="Next (leave unlabeled)", command=self._go_next_unsaved).pack(
            side="left", padx=(8, 0)
        )

        self.status_label = ttk.Label(body, text="", foreground="#a00")
        self.status_label.pack(anchor="w", pady=(8, 0))

    # ------------------------------------------------------------------
    def _apply_filter(self):
        src = self.filter_source.get()
        unlabeled_only = self.filter_unlabeled_only.get()
        self.visible_indices = [
            i
            for i, rec in enumerate(self.candidates)
            if (src == "all" or rec["source"] == src)
            and (not unlabeled_only or rec["id"] not in self.labels)
        ]
        if not self.visible_indices:
            self.visible_indices = list(range(len(self.candidates)))
        self.pos = 0
        self._render()

    def _current(self) -> dict:
        return self.candidates[self.visible_indices[self.pos]]

    def _render(self):
        rec = self._current()
        self.pos_label.configure(
            text=f"Item {self.pos + 1} of {len(self.visible_indices)} (filtered) — id {rec['id']}"
        )
        self.title_label.configure(text=rec.get("title", ""))
        self.source_label.configure(text=f"source: {rec.get('source')}")

        hist = rec.get("stage1_decision_at_the_time") or {}
        decision = hist.get("decision")
        if decision is None:
            hist_text = "historical model decision: none (item never went through Stage 1)"
        else:
            hist_text = f"historical model decision: {decision} — {hist.get('reason') or '(no reason captured)'}"
        self.history_label.configure(text=hist_text)

        self.snippet_text.configure(state="normal")
        self.snippet_text.delete("1.0", "end")
        self.snippet_text.insert("1.0", rec.get("content_snippet") or "(no content snippet)")
        self.snippet_text.configure(state="disabled")

        existing = self.labels.get(rec["id"])
        if existing:
            self.true_label.set(existing.get("true_label", ""))
            self.skip_reason.set(existing.get("skip_reason") or "")
        else:
            self.true_label.set("")
            self.skip_reason.set("")
        self._on_label_change()
        self._update_progress()
        self.status_label.configure(text="")

    def _on_label_change(self):
        if self.true_label.get() == "skip":
            self.skip_reason_combo.configure(state="readonly")
        else:
            self.skip_reason_combo.configure(state="disabled")
            self.skip_reason.set("")

    def _open_url(self):
        url = self._current().get("url")
        if url:
            webbrowser.open(url)

    def _update_progress(self):
        counts: Counter = Counter()
        for rec in self.labels.values():
            if rec.get("true_label") == "publish":
                counts["publish"] += 1
            else:
                counts[rec.get("skip_reason") or "skip"] += 1
        parts = ", ".join(f"{k}={v}" for k, v in counts.items())
        self.progress_label.configure(text=f"Labeled: {len(self.labels)}  ({parts})")

    # ------------------------------------------------------------------
    def _save_and_next(self):
        label = self.true_label.get()
        if label not in ("publish", "skip"):
            self.status_label.configure(text="Pick Publish or Skip before saving.")
            return
        reason = self.skip_reason.get() or None
        if label == "skip" and not reason:
            self.status_label.configure(text="Pick a skip reason before saving.")
            return
        if label == "publish":
            reason = None

        rec = dict(self._current())
        rec["true_label"] = label
        rec["skip_reason"] = reason
        self.labels[rec["id"]] = rec
        self._write_golden_set()
        self._go_next_unsaved()

    def _write_golden_set(self):
        with GOLDEN_PATH.open("w", encoding="utf-8") as f:
            for rec in self.labels.values():
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _go_next_unsaved(self):
        if self.pos < len(self.visible_indices) - 1:
            self.pos += 1
            self._render()
        else:
            self.status_label.configure(text="This is the last item in the current filter.")

    def _go_previous(self):
        if self.pos > 0:
            self.pos -= 1
            self._render()


if __name__ == "__main__":
    app = LabelTool()
    app.mainloop()
