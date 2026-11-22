"""Render standalone charts from the saved benchmark results."""

from collections import Counter
from io import BytesIO
from textwrap import fill
from threading import Lock

PLOTS = ("outcomes", "verdicts")
render_lock = Lock()


def available(run, kind):
    if kind == "outcomes":
        return any(not case.get("error") for case in run["cases"])
    if kind == "verdicts":
        return any(case.get("reviews") for case in run["cases"])
    return False


def outcomes(figure, cases, sns):
    names = [fill(case["name"], 27) for case in cases]
    axes = figure.subplots(1, 3, sharey=True)
    panels = (
        ("detection_minutes", "First alert", "Virtual minutes from case start", "No alert", "#1769aa"),
        ("recovery_minutes", "Recovery", "Virtual minutes after fault end", "Still open or no incident", "#337056"),
        ("incident_count", "Incidents", "Count", "Unavailable", "#762a66"),
    )
    for ax, (key, title, unit, absent, color) in zip(axes, panels):
        values = [case.get(key) for case in cases]
        rows = [(name, value) for name, value in zip(names, values) if value is not None]
        if rows:
            sns.barplot(x=[value for _, value in rows], y=[name for name, _ in rows], order=names,
                        color=color, errorbar=None, ax=ax)
        maximum = max((value for _, value in rows), default=1) or 1
        ax.set(yticks=range(len(names)), yticklabels=names, ylim=(len(names) - .5, -.5), xlim=(0, maximum * 1.2),
               title=title, xlabel=unit, ylabel="")
        for i, (case, value) in enumerate(zip(cases, values)):
            if value is None:
                note = "Replay failed" if case.get("error") else absent
                if key == "recovery_minutes" and case["benign"] and not case.get("error"):
                    note = "—"
                ax.annotate(note, (0, i), xytext=(5, 0), textcoords="offset points", va="center", fontsize=8)
            else:
                ax.annotate(f"{value:g}", (value, i), xytext=(4, 0), textcoords="offset points", va="center", fontsize=9)
        sns.despine(ax=ax, left=True)


def verdicts(figure, cases, sns):
    reviews = [review for case in cases for review in case.get("reviews", ())]
    ratings = ("supported", "partial", "unsupported", "insufficient")
    columns = ("observable_break", "scope", "evidence")
    labels, counts = [], []
    for phase, title in (("initial", "Initial report"), ("deeper", "Deeper findings")):
        for field in columns:
            tally = Counter(row[field] for row in reviews if row["id"].endswith("/" + phase))
            labels.append(title + " · " + field.replace("_", " "))
            counts.append([tally[rating] for rating in ratings])
    sns.heatmap(counts, annot=True, fmt="d", cmap="Blues", vmin=0, cbar=False, linewidths=.5,
                xticklabels=ratings, yticklabels=labels, ax=figure.subplots())


def render(run, kind):
    with render_lock:
        import seaborn as sns
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure

        with sns.axes_style("whitegrid"):
            figure = Figure(figsize=(13, max(3.4, .53 * len(run["cases"]) + 1.1)) if kind == "outcomes" else (8, 3.6),
                            layout="constrained", dpi=140, facecolor="white")
            (outcomes if kind == "outcomes" else verdicts)(figure, run["cases"], sns)
            image = BytesIO()
            FigureCanvasAgg(figure).print_png(image)
            return image.getvalue()
