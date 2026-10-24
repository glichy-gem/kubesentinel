"""Turn an eval batch into the README results table, charts and summary (design doc 9).

Inputs (all committed):
  evals/results/<eval_id>.csv      one row per live chaos run (make evals)
  evals/replay/<eval_id>.csv       offline replays per configuration (make replay)
  evals/manual-baseline.csv        your own debugging times (make manual), optional
Outputs:
  docs/evals/<eval_id>/summary.md  tables, generated: do not edit by hand
  docs/evals/<eval_id>/*.png       charts

Usage: make report EVAL_ID=<id>
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
NEGATIVE = "none"

# Reference palette (light surface); see docs/evals for how it was chosen.
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
BLUE = "#2a78d6"
BLUE_RAMP = ["#fcfcfb", "#cde2fb", "#86b6ef", "#3987e5", "#256abf", "#104281"]
GRAY = "#c3c2b7"

plt.rcParams.update(
    {
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "font.family": "sans-serif",
        "font.size": 10,
        "text.color": INK,
        "axes.labelcolor": INK_2,
        "axes.edgecolor": AXIS,
        "xtick.color": MUTED,
        "ytick.color": INK_2,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.titlesize": 12,
        "axes.titleweight": "bold",
        "axes.titlelocation": "left",
    }
)

LABELS = {
    "bad-config": "Crash loop (bad config)",
    "bad-image": "Bad image tag",
    "dependency-down": "Dependency down",
    "oom": "OOMKilled",
    "saturation": "Saturation",
    "prompt-injection": "Prompt injection",
    "noisy-neighbour": "Noisy neighbour (negative)",
}


def label(s: str) -> str:
    return LABELS.get(s, s)


def truthy(series: pd.Series) -> pd.Series:
    return series.astype(str).str.lower().isin(["true", "1"])


def load_runs(eval_id: str) -> pd.DataFrame:
    df = pd.read_csv(ROOT / "evals" / "results" / f"{eval_id}.csv")
    for col in [
        "passed",
        "alerts_detected",
        "cause_exact",
        "cause_correct",
        "cause_top2",
        "action_correct",
        "mode_correct",
        "recovered",
        "marker_in_evidence",
    ]:
        df[col] = truthy(df[col])
    return df


def fraction(hits: int, total: int) -> str:
    return f"{hits}/{total}" if total else "-"


def median(values: pd.Series) -> str:
    values = values.dropna()
    return f"{values.median():.0f} s" if len(values) else "-"


def scenario_table(runs: pd.DataFrame, manual: pd.DataFrame | None) -> pd.DataFrame:
    rows = []
    for scenario, g in runs.groupby("scenario", sort=False):
        negative = (g["expected_root_cause"] == NEGATIVE).all()
        incidents = g["incidents"].sum()
        manual_time = "-"
        if manual is not None and scenario in set(manual["scenario"]):
            m = manual[manual["scenario"] == scenario]["seconds_to_diagnose"]
            manual_time = f"{m.median() / 60:.0f} min"
        rows.append(
            {
                "Scenario": label(scenario),
                "Runs": len(g),
                "RCA correct": "no incident "
                + fraction(int((g["incidents"] == 0).sum()), len(g))
                if negative
                else fraction(int(g["cause_correct"].sum()), len(g)),
                "Exact cause": "-"
                if negative
                else fraction(int(g["cause_exact"].sum()), len(g)),
                "Top 2": "-"
                if negative
                else fraction(int(g["cause_top2"].sum()), len(g)),
                "Right action": "-"
                if negative
                else fraction(int(g["action_correct"].sum()), len(g)),
                "Median time to detect": "-"
                if negative
                else median(g["time_to_detect_s"]),
                "Median time to diagnose": "-"
                if negative
                else median(g["time_to_diagnose_s"]),
                "Manual time": manual_time,
                "Alerts → incidents": f"{g['raw_alerts'].sum()} → {incidents}",
                "Unsafe actions": int(g["unsafe_actions"].sum()),
            }
        )
    return pd.DataFrame(rows)


def chart_accuracy(runs: pd.DataFrame, out: Path) -> None:
    graded = runs[runs["expected_root_cause"] != NEGATIVE]
    acc = graded.groupby("scenario")["cause_correct"].agg(["sum", "count"])
    acc = acc.assign(rate=acc["sum"] / acc["count"]).sort_values("rate")
    fig, ax = plt.subplots(figsize=(7, 0.5 * len(acc) + 1.2))
    ax.barh([label(s) for s in acc.index], acc["rate"], color=BLUE, height=0.55)
    for y, (_, r) in enumerate(acc.iterrows()):
        ax.text(
            r["rate"] + 0.01,
            y,
            f"{int(r['sum'])}/{int(r['count'])}",
            va="center",
            color=INK_2,
        )
    ax.set_xlim(0, 1.12)
    ax.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.set_title("Correct root cause, first diagnosis")
    fig.tight_layout()
    fig.savefig(out, dpi=160)
    plt.close(fig)


def chart_time_to_diagnose(runs: pd.DataFrame, out: Path) -> None:
    graded = runs[
        (runs["expected_root_cause"] != NEGATIVE) & runs["time_to_diagnose_s"].notna()
    ]
    order = (
        graded.groupby("scenario")["time_to_diagnose_s"].median().sort_values().index
    )
    fig, ax = plt.subplots(figsize=(7, 0.55 * len(order) + 1.2))
    for y, scenario in enumerate(order):
        values = graded[graded["scenario"] == scenario]["time_to_diagnose_s"]
        jitter = [(i % 5 - 2) * 0.06 for i in range(len(values))]
        ax.scatter(
            values,
            [y + j for j in jitter],
            s=36,
            color=BLUE,
            alpha=0.75,
            edgecolors=SURFACE,
            linewidths=1.5,
            zorder=3,
        )
        ax.plot(
            [values.median()] * 2, [y - 0.3, y + 0.3], color=INK, linewidth=2, zorder=4
        )
        ax.text(
            values.median(),
            y + 0.36,
            f"{values.median():.0f} s",
            ha="center",
            color=INK_2,
            fontsize=9,
        )
    ax.set_yticks(range(len(order)), [label(s) for s in order])
    ax.set_xlabel("seconds from fault injection to the agent's diagnosis")
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.set_xlim(left=0)
    ax.set_title("Time to diagnose (each dot is a run, bar is the median)")
    fig.tight_layout()
    fig.savefig(out, dpi=160)
    plt.close(fig)


def chart_confusion(runs: pd.DataFrame, out: Path) -> None:
    """Scenario x diagnosed cause. A plain square confusion matrix would show an
    accepted alternative (config_invalid for a bad config release) as an error,
    so each cell is coloured by whether the answer was accepted, and labelled."""
    graded = runs[runs["expected_root_cause"] != NEGATIVE].copy()
    graded["predicted"] = graded["root_cause"].fillna("no diagnosis")
    rows = list(dict.fromkeys(graded["scenario"]))
    cols = sorted(set(graded["predicted"]))
    fig, ax = plt.subplots(figsize=(1.3 * len(cols) + 3, 0.7 * len(rows) + 1.8))
    for i, scenario in enumerate(rows):
        for j, cause in enumerate(cols):
            cell = graded[
                (graded["scenario"] == scenario) & (graded["predicted"] == cause)
            ]
            if cell.empty:
                continue
            accepted = bool(cell["cause_correct"].all())
            ax.add_patch(
                plt.Rectangle(
                    (j - 0.45, i - 0.4),
                    0.9,
                    0.8,
                    color=BLUE if accepted else GRAY,
                    linewidth=0,
                )
            )
            ax.text(
                j,
                i,
                f"{len(cell)}  {'accepted' if accepted else 'wrong'}",
                ha="center",
                va="center",
                fontweight="bold",
                color=SURFACE if accepted else INK,
            )
    ax.set_xlim(-0.5, len(cols) - 0.5)
    ax.set_ylim(len(rows) - 0.5, -0.5)
    ax.set_xticks(range(len(cols)), cols, rotation=20, ha="right")
    ax.set_yticks(range(len(rows)), [label(s) for s in rows])
    ax.set_xlabel("agent's root cause (first diagnosis)")
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(length=0)
    ax.set_title("Agent's conclusion per failure")
    fig.tight_layout()
    fig.savefig(out, dpi=160)
    plt.close(fig)


def replay_table(replay: pd.DataFrame) -> pd.DataFrame:
    ok = replay[replay["source"].fillna("") != ""].copy()
    for col in ["cause_correct", "cause_exact", "action_correct"]:
        ok[col] = truthy(ok[col])
    rows = []
    for config, g in ok.groupby("config", sort=False):
        rows.append(
            {
                "Configuration": config,
                "Replays": len(g),
                # Counts, not percentages: on a handful of replays "50%" overstates precision.
                "RCA correct": fraction(int(g["cause_correct"].sum()), len(g)),
                "Exact cause": fraction(int(g["cause_exact"].sum()), len(g)),
                "Right action": fraction(int(g["action_correct"].sum()), len(g)),
                "Fell back to matcher": int(
                    ((g["source"] == "matcher") & (g["config"] != "matcher")).sum()
                ),
                "Median input tokens": int(g["input_tokens"].median())
                if g["input_tokens"].any()
                else 0,
            }
        )
    per_scenario = ok.pivot_table(
        index="scenario",
        columns="config",
        values="cause_correct",
        aggfunc=lambda s: fraction(int(s.sum()), len(s)),
    )
    per_scenario.index = [label(s) for s in per_scenario.index]
    return pd.DataFrame(rows), per_scenario


def chart_configs(replay: pd.DataFrame, out: Path) -> None:
    ok = replay[replay["source"].fillna("") != ""].copy()
    ok["cause_correct"] = truthy(ok["cause_correct"])
    counts = ok.groupby("config", sort=False)["cause_correct"].agg(["sum", "count"])
    acc = (counts["sum"] / counts["count"])[::-1]
    fig, ax = plt.subplots(figsize=(7, 0.5 * len(acc) + 1.2))
    ax.barh(list(acc.index), list(acc.values), color=BLUE, height=0.55)
    for y, config in enumerate(acc.index):
        hits, total = counts.loc[config]
        ax.text(acc[config] + 0.01, y, f"{hits}/{total}", va="center", color=INK_2)
    ax.set_xlim(0, 1.12)
    ax.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.set_title("Root-cause accuracy by configuration (offline replay)")
    fig.tight_layout()
    fig.savefig(out, dpi=160)
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("eval_id")
    args = parser.parse_args()

    out_dir = ROOT / "docs" / "evals" / args.eval_id
    out_dir.mkdir(parents=True, exist_ok=True)
    runs = load_runs(args.eval_id)
    manual_file = ROOT / "evals" / "manual-baseline.csv"
    manual = pd.read_csv(manual_file) if manual_file.exists() else None

    table = scenario_table(runs, manual)
    chart_accuracy(runs, out_dir / "accuracy.png")
    chart_time_to_diagnose(runs, out_dir / "time-to-diagnose.png")
    chart_confusion(runs, out_dir / "confusion.png")

    graded = runs[runs["expected_root_cause"] != NEGATIVE]
    lines = [
        f"# Eval results: {args.eval_id}",
        "",
        "Generated by `make report`; do not edit by hand. Analysis: [docs/evals.md](../../evals.md).",
        "",
        f"{len(runs)} chaos runs, agent in dry-run mode (it diagnosed and proposed; nothing ran).",
        (
            f"Root cause correct in {int(graded['cause_correct'].sum())}/{len(graded)}"
            f" graded runs ({graded['cause_correct'].mean():.0%});"
            f" unsafe actions: {int(runs['unsafe_actions'].sum())}."
        ),
        "",
        table.to_markdown(index=False),
        "",
        "![accuracy](accuracy.png)",
        "![time to diagnose](time-to-diagnose.png)",
        "![confusion](confusion.png)",
        "",
        "## Diagnosis source and cost",
        "",
        runs.groupby("source", dropna=False)
        .agg(
            runs=("scenario", "size"),
            median_input_tokens=("input_tokens", "median"),
            median_output_tokens=("output_tokens", "median"),
            median_llm_ms=("llm_ms", "median"),
        )
        .to_markdown(),
    ]
    replay_file = ROOT / "evals" / "replay" / f"{args.eval_id}.csv"
    if replay_file.exists():
        replay = pd.read_csv(replay_file)
        overall, per_scenario = replay_table(replay)
        chart_configs(replay, out_dir / "configurations.png")
        lines += [
            "",
            "## Configurations, replayed offline on the same evidence",
            "",
            overall.to_markdown(index=False),
            "",
            per_scenario.to_markdown(),
            "",
            "![configurations](configurations.png)",
        ]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n")
    print(table.to_markdown(index=False))
    print(f"\nwrote {out_dir.relative_to(ROOT)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
