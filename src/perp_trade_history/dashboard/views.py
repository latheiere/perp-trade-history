from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict

import dash_mantine_components as dmc
import plotly.graph_objects as go
from dash import dcc, html
from plotly.subplots import make_subplots

from perp_trade_history.dashboard.models import (
    AttributionItem,
    BuildSummary,
    CoverageSummary,
    DashboardSnapshot,
    Episode,
    ExposureBand,
    Kpi,
    NotableChange,
    UnsupportedMetric,
    YearSummary,
)

COLORS = {
    "background": "#07131d",
    "surface": "#0b1b27",
    "surface_alt": "#0d202d",
    "border": "#1b3342",
    "grid": "#1b3342",
    "text": "#d9e7f0",
    "muted": "#8194a3",
    "blue": "#2ea8ff",
    "green": "#58c484",
    "red": "#ff5753",
}


def kpi_cards(items: Iterable[Kpi]) -> list[dmc.Paper]:
    return [
        dmc.Paper(
            [
                dmc.Text(item.label.upper(), className="kpi-label"),
                dmc.Text(item.value, className=f"kpi-value tone-{item.tone}"),
                dmc.Group(
                    [
                        dmc.Text(item.change, className=f"kpi-change tone-{item.tone}"),
                        dmc.Text(item.note, className="kpi-note"),
                    ],
                    gap="sm",
                ),
            ],
            className="kpi-card",
        )
        for item in items
    ]


def performance_figure(snapshot: DashboardSnapshot) -> go.Figure:
    if not snapshot.performance:
        return empty_figure("No performance history matches the current filters")
    timestamps = [point.timestamp for point in snapshot.performance]
    figure = make_subplots(specs=[[{"secondary_y": True}]])
    figure.add_trace(
        go.Scatter(
            x=timestamps,
            y=[point.net for point in snapshot.performance],
            mode="lines",
            name="Cumulative result",
            line={"color": COLORS["blue"], "width": 2},
            fill="tozeroy",
            fillcolor="rgba(46, 168, 255, 0.12)",
            hovertemplate="%{x|%b %Y}<br>Net %{y:,.0f}<extra></extra>",
        ),
        secondary_y=False,
    )
    if any(point.benchmark is not None for point in snapshot.performance):
        figure.add_trace(
            go.Scatter(
                x=timestamps,
                y=[point.benchmark for point in snapshot.performance],
                mode="lines",
                name="Equity (Benchmark)",
                line={"color": "#268bd2", "width": 1.4, "dash": "dash"},
                hovertemplate="%{x|%b %Y}<br>Benchmark %{y:,.0f}<extra></extra>",
            ),
            secondary_y=False,
        )
    if any(point.drawdown is not None for point in snapshot.performance):
        figure.add_trace(
            go.Scatter(
                x=timestamps,
                y=[point.drawdown for point in snapshot.performance],
                mode="lines",
                name="Drawdown",
                line={"color": COLORS["red"], "width": 1.5},
                hovertemplate="%{x|%b %Y}<br>Drawdown %{y:.1f}%<extra></extra>",
            ),
            secondary_y=True,
        )
    for regime in snapshot.regimes:
        figure.add_vrect(
            x0=regime.start,
            x1=regime.end,
            fillcolor="rgba(255,255,255,0.012)",
            line_width=1,
            line_dash="dot",
            line_color="rgba(129,148,163,0.28)",
            layer="below",
        )
        figure.add_annotation(
            x=regime.start,
            y=1.05,
            xref="x",
            yref="paper",
            text=regime.label,
            showarrow=False,
            xanchor="left",
            font={
                "size": 10,
                "color": COLORS[regime.tone] if regime.tone in COLORS else COLORS["muted"],
            },
        )
    _style_figure(figure, height=330, margins={"l": 54, "r": 54, "t": 52, "b": 38})
    figure.update_layout(
        hovermode="x unified",
        legend={"orientation": "h", "x": 0, "y": 1.18, "font": {"size": 10}},
        uirevision="performance-history",
    )
    figure.update_yaxes(
        tickprefix="$",
        tickformat="~s",
        title_text="Cumulative result",
        secondary_y=False,
        zeroline=True,
        zerolinecolor=COLORS["border"],
    )
    figure.update_yaxes(
        ticksuffix="%",
        title_text="Drawdown",
        range=[-45, 4],
        secondary_y=True,
    )
    return figure


def heatmap_figure(snapshot: DashboardSnapshot) -> go.Figure:
    heatmap = snapshot.heatmap
    if not heatmap.values:
        return empty_figure("No weekday or hour pattern is available", height=260)
    observed = [abs(value) for row in heatmap.values for value in row if value is not None]
    maximum = max(observed, default=1.0) or 1.0
    colorbar_ticks = [-maximum, 0, maximum] if heatmap.diverging else [0, maximum]
    tick_format = ".2f" if heatmap.diverging else ".0f"
    figure = go.Figure(
        go.Heatmap(
            x=list(heatmap.hours),
            y=list(heatmap.weekdays),
            z=[list(row) for row in heatmap.values],
            zmin=-maximum if heatmap.diverging else 0,
            zmax=maximum,
            zmid=0 if heatmap.diverging else None,
            colorscale=(
                [[0, COLORS["red"]], [0.5, "#162834"], [1, COLORS["green"]]]
                if heatmap.diverging
                else [[0, "#162834"], [1, COLORS["blue"]]]
            ),
            showscale=True,
            colorbar={
                "orientation": "h",
                "thickness": 7,
                "len": 0.76,
                "x": 0.54,
                "y": -0.25,
                "tickvals": colorbar_ticks,
                "ticktext": [f"{value:{tick_format}} {heatmap.unit}" for value in colorbar_ticks],
                "tickfont": {"size": 9},
                "outlinewidth": 0,
            },
            xgap=2,
            ygap=2,
            hovertemplate=(
                f"%{{y}} %{{x}}:00<br>%{{z:+.2f}} {heatmap.unit}<extra></extra>"
                if heatmap.diverging
                else f"%{{y}} %{{x}}:00<br>%{{z:.0f}} {heatmap.unit}<extra></extra>"
            ),
        )
    )
    _style_figure(figure, height=255, margins={"l": 44, "r": 8, "t": 8, "b": 56})
    figure.update_yaxes(autorange="reversed")
    return figure


def notable_changes(items: Iterable[NotableChange]) -> list[dmc.Box]:
    rendered: list[dmc.Box] = []
    for item in items:
        rendered.append(
            dmc.Box(
                [
                    dmc.Group(
                        [
                            dmc.Text(str(item.rank), className=f"change-rank tone-{item.tone}"),
                            dmc.Text(item.title, className="change-title"),
                            dmc.Badge(
                                item.tone.title(),
                                color=_badge_color(item.tone),
                                variant="light",
                                size="xs",
                            ),
                        ],
                        gap="sm",
                        wrap="nowrap",
                    ),
                    dmc.Text(item.summary, className="change-summary"),
                    dmc.Group(
                        [
                            dmc.Text(f"{item.episodes:,} episodes", className="micro-copy"),
                            dmc.Badge(
                                item.confidence,
                                color=(
                                    "yellow"
                                    if item.confidence.lower().startswith("low")
                                    else "blue"
                                ),
                                variant="light",
                                size="xs",
                            ),
                        ],
                        gap="lg",
                        ml=42,
                    ),
                ],
                className="change-item",
            )
        )
    return rendered or [
        dmc.Alert(
            "The current selection does not contain two sufficiently observed comparison periods.",
            title="Notable changes unavailable",
            color="yellow",
            variant="light",
            className="change-unavailable",
        )
    ]


def exposure_panel(
    items: Iterable[ExposureBand],
    metric_label: str = "Avg R",
    net_label: str = "Net R",
) -> dmc.Box:
    rows = list(items)
    if not rows:
        return empty_state("No duration pattern is available")
    max_count = max(max(item.long_count, item.short_count) for item in rows) or 1
    return dmc.Box(
        [
            dmc.SimpleGrid(
                [
                    _exposure_column("Long", rows, max_count, "long", metric_label),
                    _exposure_column("Short", rows, max_count, "short", metric_label),
                    _net_column(rows, net_label),
                ],
                cols={"base": 1, "sm": 3},
                spacing="xl",
            )
        ],
        className="exposure-content",
    )


def _exposure_column(
    title: str,
    rows: list[ExposureBand],
    max_count: int,
    side: str,
    metric_label: str,
) -> dmc.Box:
    return dmc.Box(
        [
            dmc.Text(title, className="exposure-heading"),
            dmc.Group(
                [dmc.Text("Count"), dmc.Text(metric_label)],
                className="exposure-column-labels",
                justify="space-between",
            ),
            *[
                dmc.Group(
                    [
                        dmc.Text(item.label, className="duration-label"),
                        dmc.Text(str(getattr(item, f"{side}_count")), className="duration-count"),
                        html.Div(
                            html.Div(
                                className=f"metric-bar-fill {side}",
                                style={
                                    "width": (
                                        f"{getattr(item, f'{side}_count') / max_count * 100:.1f}%"
                                    )
                                },
                            ),
                            className="metric-bar",
                        ),
                        _optional_metric(getattr(item, f"{side}_average")),
                    ],
                    className="exposure-row",
                    gap="xs",
                    wrap="nowrap",
                )
                for item in rows
            ],
        ],
        className="exposure-column",
    )


def _net_column(rows: list[ExposureBand], net_label: str) -> dmc.Box:
    maximum = (
        max(
            (abs(item.net_average) for item in rows if item.net_average is not None),
            default=1,
        )
        or 1
    )
    return dmc.Box(
        [
            dmc.Text("Net", className="exposure-heading"),
            dmc.Text(net_label, className="exposure-column-labels"),
            *[
                dmc.Group(
                    [
                        html.Div(
                            html.Div(
                                className="metric-bar-fill net",
                                style={
                                    "width": (
                                        f"{abs(item.net_average) / maximum * 100:.1f}%"
                                        if item.net_average is not None
                                        else "0%"
                                    )
                                },
                            ),
                            className="metric-bar net-bar",
                        ),
                        _optional_metric(item.net_average),
                    ],
                    className="exposure-row net-row",
                    gap="sm",
                    wrap="nowrap",
                )
                for item in rows
            ],
        ],
        className="exposure-column net-column",
    )


def _optional_metric(value: float | None) -> dmc.Text:
    if value is None:
        return dmc.Text("Unavailable", className="metric-number unavailable-metric")
    return dmc.Text(
        f"{value:+.2f}",
        className="metric-number positive" if value >= 0 else "metric-number negative",
    )


def context_card(coverage: CoverageSummary, timezone: str) -> dmc.Paper:
    coverage_state = "Complete" if coverage.percent >= 99.999 else "Partial"
    return dmc.Paper(
        [
            panel_title("Context & coverage"),
            dmc.SimpleGrid(
                [
                    _context_stat("Total episodes", f"{coverage.total:,}"),
                    _context_stat("Coverage", f"{coverage.percent:.1f}%", coverage_state),
                    _context_stat("Date range", coverage.date_range),
                ],
                cols={"base": 1, "xs": 3},
                spacing="md",
            ),
            dmc.Text(f"All times shown in {timezone}", className="micro-copy context-timezone"),
        ],
        className="panel context-panel",
    )


def _context_stat(label: str, value: str, accent: str = "") -> dmc.Box:
    children = [
        dmc.Text(label, className="context-label"),
        dmc.Text(value, className="context-value"),
    ]
    if accent:
        children.append(dmc.Text(accent, className="context-accent"))
    return dmc.Box(children)


def year_cards(items: Iterable[YearSummary], currency: str) -> list[dmc.Paper]:
    return [_year_card(item, currency) for item in items]


def _year_card(item: YearSummary, currency: str) -> dmc.Paper:
    available_win_rate = item.win_rate is not None
    win_rate = item.win_rate or 0.0
    donut = go.Figure(
        go.Pie(
            values=[win_rate, 100 - win_rate] if available_win_rate else [100],
            hole=0.68,
            marker={"colors": [COLORS["green"], "#203441"], "line": {"width": 0}},
            sort=False,
            direction="clockwise",
            textinfo="none",
            hoverinfo="skip",
        )
    )
    _style_figure(donut, height=82, margins={"l": 0, "r": 0, "t": 0, "b": 0})
    donut.add_annotation(
        text=f"{win_rate:.1f}%" if available_win_rate else "N/A",
        showarrow=False,
        font={"size": 11, "color": COLORS["text"]},
    )
    distribution = go.Figure(
        go.Bar(
            x=list(range(-5, 6)),
            y=list(item.distribution),
            marker_color=[COLORS["red"]] * 5 + ["#536979"] + [COLORS["green"]] * 5,
            hoverinfo="skip",
        )
    )
    _style_figure(distribution, height=70, margins={"l": 0, "r": 0, "t": 0, "b": 13})
    distribution.update_xaxes(
        tickvals=[-5, 0, 5], ticktext=["-2R", "0", "+2R"], tickfont={"size": 7}
    )
    distribution.update_yaxes(visible=False)
    positive = item.net_result is not None and item.net_result >= 0
    return dmc.Paper(
        [
            dmc.Text(item.label, className="year-title"),
            dmc.Text(f"{item.episodes:,} episodes", className="micro-copy"),
            html.Div(className="year-divider"),
            dmc.Text(item.result_label, className="year-label"),
            dmc.Group(
                [
                    dmc.Text(
                        _money(item.net_result) if item.net_result is not None else "Unavailable",
                        className="year-net positive" if positive else "year-net negative",
                    ),
                    dmc.Text(
                        f"{item.change:+.1f}%" if item.change is not None else "",
                        className="year-change positive"
                        if item.change is not None and item.change >= 0
                        else "year-change negative",
                    ),
                ],
                justify="space-between",
                wrap="nowrap",
            ),
            dmc.Text("Win rate", className="year-label"),
            dcc.Graph(figure=donut, config={"displayModeBar": False}, className="year-donut"),
            dmc.Text("Result distribution (R)", className="year-label"),
            (
                dcc.Graph(figure=distribution, config={"displayModeBar": False})
                if item.distribution
                else dmc.Text("Unavailable", className="micro-copy unavailable-metric")
            ),
            dmc.Text("Avg hold time", className="year-label"),
            dmc.Text(item.average_hold, className="year-hold"),
            dmc.Text(currency, className="sr-only"),
        ],
        className="year-card",
    )


def episode_rows(episodes: Iterable[Episode]) -> list[dict[str, object]]:
    return [
        {
            "episode_id": episode.episode_id,
            "episode": episode.occurred_at,
            "profile": episode.profile,
            "direction": episode.direction,
            "duration": episode.duration,
            "entry": episode.entry,
            "exit": episode.exit,
            "r_multiple": episode.r_multiple if episode.r_multiple is not None else "Unavailable",
            "result": episode.outcome,
            "mae": episode.mae if episode.mae is not None else "Unavailable",
            "mfe": episode.mfe if episode.mfe is not None else "Unavailable",
            "tags": " · ".join(episode.tags),
        }
        for episode in episodes
    ]


def episode_detail(episode: Episode | None) -> dmc.Box:
    if episode is None:
        return empty_state("Select an episode to inspect its timeline and quality")
    timeline = go.Figure(
        go.Scatter(
            x=list(episode.timeline_labels),
            y=list(episode.timeline_values),
            mode="lines+markers",
            line={"color": COLORS["blue"], "width": 1.5, "shape": "hv"},
            marker={"size": 4, "color": COLORS["blue"]},
            fill="tozeroy",
            fillcolor="rgba(46,168,255,0.08)",
            hovertemplate="%{x}<br>%{y:+.2f}R<extra></extra>",
        )
    )
    _style_figure(timeline, height=190, margins={"l": 38, "r": 8, "t": 12, "b": 28})
    timeline.update_yaxes(ticksuffix="R")
    return dmc.Box(
        [
            dmc.Group(
                [
                    dmc.Box(
                        [
                            dmc.Text(_episode_title(episode), className="detail-title"),
                            dmc.Text(
                                (
                                    f"{episode.occurred_at}  ·  {episode.profile}  ·  "
                                    f"{episode.direction}"
                                ),
                                className="micro-copy",
                            ),
                            dmc.Text(
                                (
                                    f"Duration {episode.duration}  ·  R multiple "
                                    f"{episode.r_multiple:+.2f}"
                                    if episode.r_multiple is not None
                                    else f"Duration {episode.duration}  ·  R multiple unavailable"
                                ),
                                className="micro-copy",
                            ),
                        ]
                    ),
                    dmc.Badge(
                        episode.outcome,
                        color=(
                            "green"
                            if episode.outcome == "Win"
                            else "red"
                            if episode.outcome == "Loss"
                            else "gray"
                        ),
                        variant="light",
                    ),
                ],
                justify="space-between",
                align="flex-start",
            ),
            dmc.Tabs(
                [
                    dmc.TabsList(
                        [
                            dmc.TabsTab("Overview", value="overview"),
                            dmc.TabsTab("Execution", value="execution"),
                            dmc.TabsTab("Notes", value="notes"),
                            dmc.TabsTab("Tags", value="tags"),
                        ]
                    ),
                    dmc.TabsPanel(
                        [
                            dmc.Text(
                                "Execution timeline (R)"
                                if episode.timeline_values
                                else "Execution timeline",
                                className="detail-section-title",
                            ),
                            (
                                dcc.Graph(
                                    figure=timeline,
                                    config={"displayModeBar": False, "responsive": True},
                                )
                                if episode.timeline_values
                                else empty_state(
                                    "Timeline metrics are unavailable for this episode"
                                )
                            ),
                            dmc.Text(
                                "Cashflow attribution (R)"
                                if episode.attribution
                                else "Cashflow attribution",
                                className="detail-section-title",
                            ),
                            (
                                _attribution_rows(episode.attribution)
                                if episode.attribution
                                else empty_state("R attribution is unavailable for this episode")
                            ),
                            dmc.Text("Quality indicators", className="detail-section-title"),
                            _quality_rows(episode.quality),
                            dmc.Group(
                                [
                                    dmc.Text("Confidence", className="micro-copy"),
                                    dmc.Badge(
                                        episode.confidence,
                                        color="green",
                                        variant="light",
                                        size="sm",
                                    ),
                                    dmc.Text("Samples", className="micro-copy", ml="auto"),
                                    dmc.Text(str(episode.samples), className="detail-value"),
                                ],
                                className="detail-footer",
                            ),
                        ],
                        value="overview",
                    ),
                    dmc.TabsPanel(
                        empty_state("Execution diagnostics are supplied by the analytics provider"),
                        value="execution",
                    ),
                    dmc.TabsPanel(
                        empty_state("No notes are attached to this episode"), value="notes"
                    ),
                    dmc.TabsPanel(
                        dmc.Group([dmc.Badge(tag, variant="outline") for tag in episode.tags]),
                        value="tags",
                    ),
                ],
                value="overview",
                className="detail-tabs",
            ),
        ],
        className="episode-detail-content",
    )


def _episode_title(episode: Episode) -> str:
    identifier = episode.episode_id
    if identifier.startswith("episode-"):
        return f"Episode {identifier.removeprefix('episode-')}"
    if identifier.startswith("episode:"):
        return f"Episode {identifier[-8:].upper()}"
    return "Episode"


def _attribution_rows(items: Iterable[AttributionItem]) -> dmc.Stack:
    rows = list(items)
    maximum = max((abs(item.value) for item in rows), default=1) or 1
    total = sum(item.value for item in rows)
    return dmc.Stack(
        [
            *[
                dmc.Group(
                    [
                        dmc.Text(item.label, className="attribution-label"),
                        html.Div(
                            html.Div(
                                className="attribution-fill positive"
                                if item.value >= 0
                                else "attribution-fill negative",
                                style={"width": f"{abs(item.value) / maximum * 100:.1f}%"},
                            ),
                            className="attribution-track",
                        ),
                        dmc.Text(f"{item.value:+.2f}R", className="attribution-value"),
                    ],
                    wrap="nowrap",
                    gap="xs",
                )
                for item in rows
            ],
            dmc.Text(f"{total:+.2f}R", className="attribution-total"),
        ],
        gap=5,
    )


def _quality_rows(items: Iterable) -> dmc.Stack:
    return dmc.Stack(
        [
            dmc.Group(
                [
                    dmc.Text(item.label, className="quality-label"),
                    dmc.Badge(
                        item.value, color=_badge_color(item.tone), variant="light", size="xs"
                    ),
                ],
                justify="space-between",
            )
            for item in items
        ],
        gap=6,
    )


def data_quality_cards(
    coverage: CoverageSummary,
    unsupported: Iterable[UnsupportedMetric],
    build: BuildSummary,
) -> list[dmc.Paper]:
    complete = coverage.percent >= 99.999
    return [
        dmc.Paper(
            [
                panel_title("Coverage"),
                dmc.Group(
                    [
                        dmc.Text(f"{coverage.percent:.1f}%", className="quality-hero positive"),
                        dmc.Badge(
                            "Complete" if complete else "Partial",
                            color="green" if complete else "yellow",
                            variant="light",
                        ),
                    ],
                    align="center",
                ),
                dmc.Text("Episodes with complete data", className="quality-copy"),
                dmc.Text(f"{coverage.complete:,} / {coverage.total:,}", className="quality-ratio"),
                dmc.Progress(value=coverage.percent, color="green", size="sm", mt="lg"),
            ],
            className="panel quality-card",
        ),
        dmc.Paper(
            [
                panel_title("Unsupported metrics"),
                dmc.Text(
                    "Some metrics are not supported for all episodes.", className="quality-copy"
                ),
                dmc.Stack(
                    [
                        dmc.Group(
                            [
                                dmc.Text(item.label, className="quality-label"),
                                dmc.Text(
                                    f"{item.percent:.1f}%"
                                    if item.percent is not None
                                    else "Unavailable",
                                    className="detail-value unavailable-metric",
                                ),
                            ],
                            justify="space-between",
                        )
                        for item in unsupported
                    ],
                    gap=8,
                    mt="md",
                ),
            ],
            className="panel quality-card",
        ),
        dmc.Paper(
            [
                panel_title("Latest build"),
                dmc.Text(build.timestamp, className="build-time"),
                _build_row("Build ID", build.build_id),
                _build_row("Source data up to", build.source_data),
                _build_row("Next scheduled build", build.next_build),
                dmc.Group(
                    [
                        dmc.Text("Status", className="quality-label"),
                        dmc.Badge(build.status, color="green", variant="dot"),
                    ],
                    justify="space-between",
                    mt="md",
                ),
            ],
            className="panel quality-card",
        ),
    ]


def _build_row(label: str, value: str) -> dmc.Group:
    return dmc.Group(
        [dmc.Text(label, className="quality-label"), dmc.Text(value, className="quality-copy")],
        justify="space-between",
        wrap="nowrap",
        mt=7,
    )


def panel_title(title: str, action: str | None = None) -> dmc.Group:
    children: list = [dmc.Text(title, className="panel-title")]
    if action:
        children.append(
            dmc.Button(action, variant="subtle", size="compact-xs", className="panel-action")
        )
    return dmc.Group(children, justify="space-between", wrap="nowrap", className="panel-title-row")


def section_heading(number: int, title: str) -> dmc.Group:
    return dmc.Group(
        [
            dmc.Text(str(number), className="section-number"),
            dmc.Text(title, className="section-title"),
        ],
        gap="sm",
        className="section-heading",
    )


def empty_state(message: str) -> dmc.Center:
    return dmc.Center(
        dmc.Stack(
            [
                dmc.Text("No data", className="empty-title", ta="center"),
                dmc.Text(message, className="empty-copy", ta="center"),
            ],
            gap=4,
        ),
        className="empty-state",
    )


def empty_figure(message: str, *, height: int = 330) -> go.Figure:
    figure = go.Figure()
    _style_figure(figure, height=height, margins={"l": 16, "r": 16, "t": 16, "b": 16})
    figure.add_annotation(
        text=message,
        showarrow=False,
        x=0.5,
        y=0.5,
        xref="paper",
        yref="paper",
        font={"color": COLORS["muted"], "size": 12},
    )
    return figure


def _style_figure(figure: go.Figure, *, height: int, margins: dict[str, int]) -> None:
    figure.update_layout(
        height=height,
        margin=margins,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"family": "Inter, system-ui, sans-serif", "color": COLORS["muted"], "size": 10},
        hoverlabel={"bgcolor": COLORS["surface_alt"], "bordercolor": COLORS["border"]},
        showlegend=False,
    )
    figure.update_xaxes(
        showgrid=True,
        gridcolor=COLORS["grid"],
        gridwidth=0.5,
        zeroline=False,
        tickfont={"color": COLORS["muted"]},
    )
    figure.update_yaxes(
        showgrid=True,
        gridcolor=COLORS["grid"],
        gridwidth=0.5,
        zeroline=False,
        tickfont={"color": COLORS["muted"]},
    )


def _money(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):,.0f}"


def _badge_color(tone: str) -> str:
    return {"positive": "green", "negative": "red", "info": "blue"}.get(tone, "gray")


def episode_asdict(episode: Episode) -> dict:
    return asdict(episode)
