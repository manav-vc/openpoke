"""Render source-backed PNG/SVG charts; requires matplotlib (no API calls)."""
from pathlib import Path
import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, MaxNLocator

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs/charts"
DATA = json.loads((OUT / "measurements.json").read_text(encoding="utf-8"))
BEFORE, AFTER, INK, MUTED = "#B45335", "#087F8C", "#172B3A", "#526575"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11, "text.color": INK,
                     "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED,
                     "svg.fonttype": "none", "axes.spines.top": False, "axes.spines.right": False,
                     "axes.spines.left": False, "axes.spines.bottom": False})


def canvas(title):
    fig, ax = plt.subplots(figsize=(12, 6.5), facecolor="#FAFCFD")
    ax.set_facecolor("#FAFCFD")
    fig.subplots_adjust(left=.12, right=.94, top=.76, bottom=.15)
    fig.text(.12, .92, title, fontsize=23, weight="bold")
    ax.grid(axis="y", color="#DDE5EA", linewidth=.8)
    ax.set_axisbelow(True)
    ax.tick_params(length=0, pad=9)
    return fig, ax


def save(fig, name):
    ax = fig.axes[0]
    handles, labels = ax.get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper left", bbox_to_anchor=(.12, .87),
               ncol=len(labels), frameon=False, borderaxespad=0, columnspacing=2.2)
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    for text in fig.texts:
        box = text.get_window_extent(renderer)
        assert box.x0 >= 0 and box.y0 >= 0 and box.x1 <= fig.bbox.width and box.y1 <= fig.bbox.height, text.get_text()
    for extension in ("png", "svg"):
        fig.savefig(OUT / f"{name}.{extension}", dpi=180, facecolor=fig.get_facecolor(), metadata={"Date": None} if extension == "svg" else None)
    plt.close(fig)


fig, ax = canvas("IA roster exposure")
points = DATA["roster"]
x = list(range(len(points)))
before = [p["upstream_roster_entries"] for p in points]
after = [p["new_search_candidates"] for p in points]
width = .29
bars_before = ax.bar([i-width/2 for i in x], before, width, color=BEFORE, label="Before")
bars_after = ax.bar([i+width/2 for i in x], after, width, color=AFTER, label="After")
for bars in (bars_before, bars_after):
    for bar in bars:
        ax.annotate(f"{bar.get_height():,.0f}", (bar.get_x()+bar.get_width()/2, bar.get_height()),
                    xytext=(0, 8), textcoords="offset points", ha="center", fontsize=12, weight="bold")
ax.set_xticks(x, [f"{p['saved_agents']:,} saved EAs" for p in points])
ax.set_ylabel("Roster entries / search candidates")
ax.set_ylim(0, max(before)*1.21)
ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
save(fig, "01-roster-context")


fig, ax = canvas("EA history context")
points = DATA["history"]["points"]
x = [p["completed_exchanges"] for p in points]
before = [p["upstream_estimated_tokens"] for p in points]
after = [p["new_estimated_tokens"] for p in points]
ax.plot(x, before, color=BEFORE, linewidth=2.8, marker="o", markersize=5, label="Before")
ax.plot(x, after, color=AFTER, linewidth=2.8, marker="o", markersize=5, label="After")
ax.axhline(DATA["history"]["context_budget"], color=MUTED, linestyle=(0,(4,4)), linewidth=1.2, label="8,000-token limit")
ax.set_xlabel("Completed exchanges (synthetic)")
ax.set_ylabel("Estimated context tokens")
ax.set_xlim(0, 112)
ax.set_ylim(0, max(max(before)*1.18, 10000))
ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v/1000:g}k"))
for values, color, offset in ((before, BEFORE, 4), (after, AFTER, -4)):
    ax.annotate(f"{values[-1]:,}", (x[-1], values[-1]), xytext=(8, offset), textcoords="offset points", color=color, weight="bold")
save(fig, "02-history-context")


fig, ax = canvas("EA execution concurrency")
points = DATA["concurrency"]["points"]
x = [p["simulated_seconds"] for p in points]
before = [p["upstream_running"] for p in points]
after = [p["new_running"] for p in points]
ax.step(x, before, where="post", color=BEFORE, linewidth=3, label="Before")
ax.step(x, after, where="post", color=AFTER, linewidth=3, label="After")
ax.fill_between(x, after, step="post", alpha=.12, color=AFTER)
ax.axhline(4, color=AFTER, linestyle=(0,(3,4)), linewidth=1, alpha=.55)
ax.annotate("24", (.5, 24), xytext=(0, 10), textcoords="offset points", ha="center", color=BEFORE, weight="bold")
ax.annotate("4", (3, 4), xytext=(0, 10), textcoords="offset points", ha="center", color=AFTER, weight="bold")
ax.set_xlabel("Simulated time (seconds)")
ax.set_ylabel("Concurrent EA jobs")
ax.set_xlim(-.15, 6.3)
ax.set_ylim(0, 28)
ax.set_yticks([0,4,8,12,16,20,24])
ax.xaxis.set_major_locator(MaxNLocator(integer=True))
save(fig, "03-execution-concurrency")
print("Created three PNGs and three editable SVGs in", OUT)
