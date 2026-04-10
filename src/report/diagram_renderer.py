from pathlib import Path
from typing import Dict, List

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager, rcParams

from src.core.paper import Paper


def _configure_cjk_font() -> None:
    candidates = [
        "Microsoft YaHei",
        "SimHei",
        "Noto Sans CJK SC",
        "Source Han Sans SC",
        "WenQuanYi Zen Hei",
    ]
    installed = {f.name for f in font_manager.fontManager.ttflist}
    for name in candidates:
        if name in installed:
            rcParams["font.sans-serif"] = [name]
            rcParams["axes.unicode_minus"] = False
            return


_configure_cjk_font()


class DiagramRenderer:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.diagram_dir = run_dir / "generated_diagrams"
        self.diagram_dir.mkdir(parents=True, exist_ok=True)

    def render(self, papers: List[Paper]) -> Dict[str, int]:
        created = 0
        for paper in papers:
            steps = paper.process_flow_steps or []
            if not steps:
                continue
            rel_path = self._render_flowchart(paper.uid(), paper.title_cn or paper.title, steps)
            paper.generated_diagram_path = rel_path
            created += 1
        return {"diagrams_created": created}

    def _render_flowchart(self, uid: str, title: str, steps: List[str]) -> str:
        safe_uid = uid.replace("/", "_").replace(":", "_")[:80]
        out_path = self.diagram_dir / f"{safe_uid}_flow.png"

        count = max(1, len(steps))
        height = max(4, count * 1.45 + 1.8)
        fig, ax = plt.subplots(figsize=(9, height))
        ax.set_xlim(0, 10)
        ax.set_ylim(0, count * 2 + 2)
        ax.axis("off")

        ax.text(
            5,
            count * 2 + 1.2,
            title[:90],
            ha="center",
            va="center",
            fontsize=14,
            fontweight="bold",
        )

        y_positions = list(range(count * 2, 0, -2))
        for idx, (step, ypos) in enumerate(zip(steps, y_positions), start=1):
            ax.text(
                5,
                ypos,
                f"{idx}. {step}",
                ha="center",
                va="center",
                fontsize=11,
                bbox={
                    "boxstyle": "round,pad=0.5",
                    "facecolor": "#eef3ff",
                    "edgecolor": "#3b5b92",
                    "linewidth": 1.5,
                },
            )
            if idx < count:
                ax.annotate(
                    "",
                    xy=(5, ypos - 1.1),
                    xytext=(5, ypos - 0.45),
                    arrowprops={"arrowstyle": "->", "linewidth": 1.6, "color": "#3b5b92"},
                )

        plt.tight_layout()
        plt.savefig(out_path, dpi=160, bbox_inches="tight")
        plt.close(fig)
        return out_path.relative_to(self.run_dir).as_posix()
