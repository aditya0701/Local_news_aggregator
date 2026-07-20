"""Local GUI for grading evals/faithfulness/candidates.jsonl by hand.

Run with:  python evals/faithfulness/label_tool.py

One ARTICLE per screen (same pattern as evals/entity_quality/label_tool.py,
for the same reason -- 1107 claim rows across only 40 articles, so per-row
screens would mean re-reading the same source text over and over). Claims
are grouped under their field (concept_box / lede / deep dive / strategic
analysis / conclusion), each with its own grade dropdown. The real English
source text is shown in a scrollable panel for reference while grading --
that's the actual thing each claim needs to be checked against.

Claims are displayed in ENGLISH (claim_text_en, machine-translated by
build_candidates.py) so grading doesn't require reading Hindi -- the
original Hindi (claim_text_hi) is shown smaller underneath each one, in
case the translation looks off and you want to cross-check. The grade you
pick is still recorded against claim_text_hi, the actual published claim --
claim_text_en is a display aid only, never written back into golden_set.jsonl
as anything other than a copy of what candidates.jsonl already had.

Nothing is pre-selected -- every dropdown starts blank. Grades are written
to evals/faithfulness/golden_set.jsonl immediately on Save & Next, so
closing the tool mid-session never loses progress.
"""

import json
import tkinter as tk
import webbrowser
from collections import defaultdict
from pathlib import Path
from tkinter import messagebox, ttk

CANDIDATES_PATH = Path(__file__).parent / "candidates.jsonl"
GOLDEN_PATH = Path(__file__).parent / "golden_set.jsonl"

CLAIM_GRADES = ["supported", "unsupported", "hedged_opinion"]

FIELD_LABELS = {
    "concept_box": "Concept box",
    "introduction_lede": "Introduction / lede",
    "deep_dive_and_context": "Deep dive & context",
    "strategic_analysis": "Strategic analysis",
    "conclusion_and_significance": "Conclusion & significance",
}


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class FaithfulnessLabelTool(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("TechDrishti Faithfulness Labeler")
        self.geometry("1000x800")

        rows = load_jsonl(CANDIDATES_PATH)
        if not rows:
            messagebox.showerror("No candidates", f"{CANDIDATES_PATH} is empty or missing.")
            self.destroy()
            return

        by_article: dict = defaultdict(list)
        for r in rows:
            by_article[r["article_id"]].append(r)
        for article_rows in by_article.values():
            article_rows.sort(key=lambda r: r["claim_index"])
        self.articles = list(by_article.items())  # [(article_id, [rows...]), ...]

        existing = load_jsonl(GOLDEN_PATH)
        self.grades: dict[str, str] = {r["id"]: r["grade"] for r in existing if "grade" in r}

        self.pos = 0
        self.grade_vars: dict[str, tk.StringVar] = {}

        self._build_ui()
        self._render()

    # ------------------------------------------------------------------
    def _build_ui(self):
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        self.pos_label = ttk.Label(top, text="")
        self.pos_label.pack(side="left")
        self.progress_label = ttk.Label(top, text="", anchor="e")
        self.progress_label.pack(side="right")

        header = ttk.Frame(self, padding=(8, 0))
        header.pack(fill="x")
        self.title_label = ttk.Label(
            header, text="", font=("Segoe UI", 13, "bold"), wraplength=950, justify="left"
        )
        self.title_label.pack(anchor="w", pady=(4, 4))
        ttk.Button(header, text="Open source URL in browser", command=self._open_url).pack(anchor="w")

        ttk.Label(header, text="Real English source text (check claims against this):").pack(
            anchor="w", pady=(8, 0)
        )
        src_frame = ttk.Frame(header)
        src_frame.pack(fill="x")
        self.source_text = tk.Text(src_frame, height=8, wrap="word", state="disabled")
        src_scroll = ttk.Scrollbar(src_frame, command=self.source_text.yview)
        self.source_text.configure(yscrollcommand=src_scroll.set)
        self.source_text.pack(side="left", fill="both", expand=True)
        src_scroll.pack(side="right", fill="y")

        # ---- scrollable region for claim rows ----
        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        canvas = tk.Canvas(mid, highlightthickness=0)
        scrollbar = ttk.Scrollbar(mid, orient="vertical", command=canvas.yview)
        self.rows_frame = ttk.Frame(canvas)
        self.rows_frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=self.rows_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        nav = ttk.Frame(self, padding=8)
        nav.pack(fill="x")
        ttk.Button(nav, text="< Previous article", command=self._go_previous).pack(side="left")
        ttk.Button(nav, text="Save & Next article >", command=self._save_and_next).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(nav, text="Next (leave ungraded)", command=self._go_next_unsaved).pack(
            side="left", padx=(8, 0)
        )
        self.status_label = ttk.Label(nav, text="", foreground="#a00")
        self.status_label.pack(side="left", padx=(16, 0))

    # ------------------------------------------------------------------
    def _current(self):
        return self.articles[self.pos]

    def _render(self):
        article_id, rows = self._current()
        first = rows[0]
        self.pos_label.configure(text=f"Article {self.pos + 1} of {len(self.articles)} — id {article_id}")
        self.title_label.configure(text=first.get("article_title_en") or first["article_title"])

        self.source_text.configure(state="normal")
        self.source_text.delete("1.0", "end")
        self.source_text.insert("1.0", first.get("source_text") or "(no source text captured)")
        self.source_text.configure(state="disabled")

        for widget in self.rows_frame.winfo_children():
            widget.destroy()
        self.grade_vars = {}

        by_field: dict = defaultdict(list)
        for r in rows:
            by_field[r["field"]].append(r)

        for field in FIELD_LABELS:
            field_rows = by_field.get(field, [])
            if not field_rows:
                continue
            ttk.Label(
                self.rows_frame, text=FIELD_LABELS[field], font=("Segoe UI", 11, "bold")
            ).pack(anchor="w", pady=(10, 2))
            for r in field_rows:
                self._add_row(r)

        self._update_progress()
        self.status_label.configure(text="")

    def _add_row(self, r: dict):
        row_frame = ttk.Frame(self.rows_frame)
        row_frame.pack(fill="x", pady=(4, 6), anchor="w")

        text_frame = ttk.Frame(row_frame)
        text_frame.pack(side="left", fill="x", expand=True, padx=(0, 10))
        ttk.Label(
            text_frame, text=r.get("claim_text_en") or r["claim_text_hi"],
            wraplength=700, justify="left",
        ).pack(anchor="w")
        if r.get("claim_text_en"):
            ttk.Label(
                text_frame, text=r["claim_text_hi"], wraplength=700, justify="left",
                foreground="#888", font=("Segoe UI", 8),
            ).pack(anchor="w")

        var = tk.StringVar(value=self.grades.get(r["id"], ""))
        self.grade_vars[r["id"]] = var
        combo = ttk.Combobox(row_frame, textvariable=var, state="readonly", width=20, values=CLAIM_GRADES)
        combo.pack(side="right", anchor="n")

    def _open_url(self):
        _, rows = self._current()
        url = rows[0].get("article_url")
        if url:
            webbrowser.open(url)

    def _update_progress(self):
        graded_articles = sum(
            1 for _, rows in self.articles if all(r["id"] in self.grades for r in rows)
        )
        total_rows = sum(len(rows) for _, rows in self.articles)
        graded_rows = len(self.grades)
        self.progress_label.configure(
            text=f"Articles fully graded: {graded_articles}/{len(self.articles)}  |  Claims graded: {graded_rows}/{total_rows}"
        )

    # ------------------------------------------------------------------
    def _save_and_next(self):
        _, rows = self._current()
        ungraded = [r for r in rows if not self.grade_vars[r["id"]].get()]
        if ungraded:
            self.status_label.configure(
                text=f"{len(ungraded)} claim(s) still ungraded on this article -- grade all or use 'Next (leave ungraded)'."
            )
            return
        for r in rows:
            self.grades[r["id"]] = self.grade_vars[r["id"]].get()
        self._write_golden_set()
        self._go_next_unsaved()

    def _write_golden_set(self):
        by_id = {}
        for _, rows in self.articles:
            for r in rows:
                if r["id"] in self.grades:
                    row = dict(r)
                    row["grade"] = self.grades[r["id"]]
                    by_id[r["id"]] = row
        with GOLDEN_PATH.open("w", encoding="utf-8") as f:
            for row in by_id.values():
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _go_next_unsaved(self):
        if self.pos < len(self.articles) - 1:
            self.pos += 1
            self._render()
        else:
            self.status_label.configure(text="This is the last article.")

    def _go_previous(self):
        if self.pos > 0:
            self.pos -= 1
            self._render()


if __name__ == "__main__":
    app = FaithfulnessLabelTool()
    app.mainloop()
