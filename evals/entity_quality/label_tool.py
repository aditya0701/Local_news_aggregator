"""Local GUI for grading evals/entity_quality/candidates.jsonl by hand.

Run with:  python evals/entity_quality/label_tool.py

Unlike the triage tool (one item per screen), this shows one ARTICLE per
screen with ALL of its extracted entities and GAP queries listed together,
each with its own grade dropdown -- grading a whole article's Stage 1 output
in one pass instead of switching screens per entity/query (there are 397
rows across only 54 articles, so per-row screens would mean a lot of
re-reading the same article context over and over).

Nothing is pre-selected -- every dropdown starts blank. Grades are written
to evals/entity_quality/golden_set.jsonl immediately on Save & Next, so
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

ENTITY_GRADES = ["correct", "wrong_type", "hallucinated_not_in_article", "not_an_entity", "ambiguity_mislabeled"]
QUERY_GRADES = ["good", "hallucinated_competitor", "dangling_reference", "not_fact_seeking", "irrelevant"]


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class EntityQualityLabelTool(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("TechDrishti Entity/Query Quality Labeler")
        self.geometry("950x760")

        rows = load_jsonl(CANDIDATES_PATH)
        if not rows:
            messagebox.showerror("No candidates", f"{CANDIDATES_PATH} is empty or missing.")
            self.destroy()
            return

        by_article: dict = defaultdict(list)
        for r in rows:
            by_article[r["article_id"]].append(r)
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
            header, text="", font=("Segoe UI", 13, "bold"), wraplength=900, justify="left"
        )
        self.title_label.pack(anchor="w", pady=(4, 4))
        ttk.Button(header, text="Open source URL in browser", command=self._open_url).pack(anchor="w")

        ttk.Label(header, text="Article context:").pack(anchor="w", pady=(8, 0))
        ctx_frame = ttk.Frame(header)
        ctx_frame.pack(fill="x")
        self.context_text = tk.Text(ctx_frame, height=5, wrap="word", state="disabled")
        ctx_scroll = ttk.Scrollbar(ctx_frame, command=self.context_text.yview)
        self.context_text.configure(yscrollcommand=ctx_scroll.set)
        self.context_text.pack(side="left", fill="both", expand=True)
        ctx_scroll.pack(side="right", fill="y")

        # ---- scrollable region for entity/query rows ----
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
        self.title_label.configure(text=first["article_title"])

        self.context_text.configure(state="normal")
        self.context_text.delete("1.0", "end")
        self.context_text.insert("1.0", first.get("article_context") or "(no context captured)")
        self.context_text.configure(state="disabled")

        for widget in self.rows_frame.winfo_children():
            widget.destroy()
        self.grade_vars = {}

        entity_rows = [r for r in rows if r["kind"] == "entity"]
        query_rows = [r for r in rows if r["kind"] == "query"]

        if entity_rows:
            ttk.Label(self.rows_frame, text="Entities", font=("Segoe UI", 11, "bold")).pack(
                anchor="w", pady=(6, 2)
            )
        for r in entity_rows:
            self._add_row(
                r,
                label=self._entity_label(r),
                grades=ENTITY_GRADES,
            )

        if query_rows:
            ttk.Label(self.rows_frame, text="GAP search queries", font=("Segoe UI", 11, "bold")).pack(
                anchor="w", pady=(12, 2)
            )
        for r in query_rows:
            self._add_row(
                r,
                label=r["query_text"],
                grades=QUERY_GRADES,
            )

        self._update_progress()
        self.status_label.configure(text="")

    @staticmethod
    def _entity_label(r: dict) -> str:
        text = f"{r['entity_name']} ({r['entity_type']})"
        if r.get("entity_ambiguous"):
            text += f"  [ambiguous: {r.get('entity_resolved_sense') or '?'}]"
        return text

    def _add_row(self, r: dict, label: str, grades: list[str]):
        row_frame = ttk.Frame(self.rows_frame)
        row_frame.pack(fill="x", pady=2, anchor="w")
        ttk.Label(row_frame, text=label, wraplength=650, justify="left").pack(
            side="left", padx=(0, 10)
        )
        var = tk.StringVar(value=self.grades.get(r["id"], ""))
        self.grade_vars[r["id"]] = var
        combo = ttk.Combobox(row_frame, textvariable=var, state="readonly", width=28, values=grades)
        combo.pack(side="right")

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
            text=f"Articles fully graded: {graded_articles}/{len(self.articles)}  |  Rows graded: {graded_rows}/{total_rows}"
        )

    # ------------------------------------------------------------------
    def _save_and_next(self):
        _, rows = self._current()
        ungraded = [r for r in rows if not self.grade_vars[r["id"]].get()]
        if ungraded:
            self.status_label.configure(
                text=f"{len(ungraded)} row(s) still ungraded on this article -- grade all or use 'Next (leave ungraded)'."
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
    app = EntityQualityLabelTool()
    app.mainloop()
