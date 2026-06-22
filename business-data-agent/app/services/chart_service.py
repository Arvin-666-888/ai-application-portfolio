import logging
import os
import uuid

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from app.config import settings

logger = logging.getLogger("kb_qa.chart")

_platform_fonts = {
    "Windows": ['SimHei', 'Microsoft YaHei'],
    "Darwin": ['Arial Unicode MS', 'PingFang SC'],
    "Linux": ['WenQuanYi Micro Hei', 'Noto Sans CJK SC'],
}

import platform
_system = platform.system()
_fonts = _platform_fonts.get(_system, ['DejaVu Sans'])
plt.rcParams['font.sans-serif'] = _fonts + ['DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False


CHART_COLORS = ["#4C72B0", "#DD8452", "#55A868", "#C44E52", "#8172B2",
                "#64B5CD", "#8C8C8C", "#CCB974", "#937860", "#DA8BC3"]


def create_chart(
    data: list[dict],
    chart_type: str,
    title: str,
    x_field: str,
    y_field: str,
) -> str:
    data = _prepare_data(data, x_field, y_field)

    x_values = [str(row.get(x_field, "")) for row in data]
    y_values = []
    for row in data:
        try:
            y_values.append(float(row.get(y_field, 0)))
        except (ValueError, TypeError):
            y_values.append(0.0)

    fig, ax = plt.subplots(figsize=(10, 6))

    if chart_type == "bar":
        bars = ax.bar(range(len(x_values)), y_values, color=CHART_COLORS[:len(x_values)])
        ax.set_xticks(range(len(x_values)))
        ax.set_xticklabels(x_values, rotation=45, ha='right')
        ax.set_xlabel(x_field)
        ax.set_ylabel(y_field)
        for bar, val in zip(bars, y_values):
            ax.text(bar.get_x() + bar.get_width() / 2., bar.get_height(),
                    f'{val:.1f}', ha='center', va='bottom', fontsize=9)

    elif chart_type == "line":
        ax.plot(x_values, y_values, marker='o', color=CHART_COLORS[0], linewidth=2, markersize=6)
        ax.set_xlabel(x_field)
        ax.set_ylabel(y_field)
        plt.xticks(rotation=45, ha='right')
        for i, val in enumerate(y_values):
            ax.annotate(f'{val:.1f}', (x_values[i], val), textcoords="offset points",
                        xytext=(0, 10), ha='center', fontsize=9)

    elif chart_type == "pie":
        colors = CHART_COLORS[:len(x_values)]
        wedges, texts, autotexts = ax.pie(
            y_values, labels=x_values, colors=colors,
            autopct='%1.1f%%', startangle=90,
        )
        for text in autotexts:
            text.set_fontsize(9)

    else:
        plt.close(fig)
        raise ValueError(f"不支持的图表类型: {chart_type}")

    ax.set_title(title, fontsize=14, fontweight='bold')
    plt.tight_layout()

    filename = f"chart_{uuid.uuid4().hex[:8]}.png"
    filepath = os.path.join(settings.CHART_DIR, filename)
    fig.savefig(filepath, dpi=100, bbox_inches='tight')
    plt.close(fig)

    logger.info(f"Chart generated: {filename}")
    return filename


def _prepare_data(data: list[dict], x_field: str, y_field: str) -> list[dict]:
    if len(data) <= settings.MAX_CHART_ITEMS:
        return data

    sorted_data = sorted(data, key=lambda x: float(x.get(y_field, 0)), reverse=True)
    top = sorted_data[:settings.MAX_CHART_ITEMS - 1]
    others_sum = sum(float(row.get(y_field, 0)) for row in sorted_data[settings.MAX_CHART_ITEMS - 1:])
    top.append({x_field: "其他", y_field: others_sum})
    return top
