from __future__ import annotations

from collections.abc import Iterable

import dash_mantine_components as dmc
import plotly.graph_objects as go
from dash import dcc, html

from perp_trade_history.dashboard.models import (
    BuildSummary,
    CashflowDetail,
    CoverageGap,
    CoverageSummary,
    DashboardSnapshot,
    Episode,
    ExecutionDetail,
    ExposureBand,
    Kpi,
    NotableChange,
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


def performance_figure(snapshot: DashboardSnapshot, view: str = "cumulative") -> go.Figure:
    if not snapshot.performance:
        return empty_figure("No performance history matches the current filters")
    fields = {
        "cumulative": ("net", "Cumulative net result", COLORS["blue"]),
        "period": ("period", "Period net result", COLORS["green"]),
        "rolling": ("rolling", "Rolling net result", COLORS["blue"]),
        "drawdown": ("drawdown", "Absolute drawdown", COLORS["red"]),
    }
    field, label, color = fields.get(view, fields["cumulative"])
    timestamps = [point.timestamp for point in snapshot.performance]
    values = [getattr(point, field) for point in snapshot.performance]
    figure = go.Figure()
    if view == "period":
        figure.add_trace(
            go.Bar(
                x=timestamps,
                y=values,
                name=label,
                marker_color=[COLORS["green"] if value >= 0 else COLORS["red"] for value in values],
                hovertemplate=f"%{{x|%b %Y}}<br>{label} %{{y:,.2f}}<extra></extra>",
            )
        )
    else:
        figure.add_trace(
            go.Scatter(
                x=timestamps,
                y=values,
                mode="lines",
                name=label,
                line={"color": color, "width": 2},
                fill="tozeroy",
                fillcolor=(
                    "rgba(255, 87, 83, 0.10)" if view == "drawdown" else "rgba(46, 168, 255, 0.12)"
                ),
                hovertemplate=f"%{{x|%b %Y}}<br>{label} %{{y:,.2f}}<extra></extra>",
            )
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
        uirevision=f"performance-{view}",
    )
    figure.update_yaxes(
        tickformat="~s",
        title_text=f"{label} ({snapshot.currency})",
        zeroline=True,
        zerolinecolor=COLORS["border"],
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
    metric_label: str = "Average result",
    net_label: str = "Net result",
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
                dmc.UnstyledButton(
                    dmc.Group(
                        [
                            dmc.Text(item.label, className="duration-label"),
                            dmc.Text(
                                str(getattr(item, f"{side}_count")),
                                className="duration-count",
                            ),
                            html.Div(
                                html.Div(
                                    className=(
                                        "metric-bar-fill "
                                        + _outcome_class(getattr(item, f"{side}_average"))
                                    ),
                                    style={
                                        "width": _percent_width(
                                            getattr(item, f"{side}_count"), max_count
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
                    ),
                    id={
                        "type": "exposure-drill",
                        "value": item.filter_value,
                        "side": side,
                    },
                    className="exposure-drill",
                    **{"aria-label": f"Filter episodes to {item.label}, {title.lower()}"},
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
                dmc.UnstyledButton(
                    dmc.Group(
                        [
                            dmc.Text(item.label, className="duration-label"),
                            html.Div(
                                html.Div(
                                    className=(
                                        "metric-bar-fill "
                                        + _outcome_class(item.net_average)
                                    ),
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
                    ),
                    id={
                        "type": "exposure-drill",
                        "value": item.filter_value,
                        "side": "net",
                    },
                    className="exposure-drill",
                    **{"aria-label": f"Filter episodes to {item.label}"},
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


def _outcome_class(value: float | None) -> str:
    if value is None or value == 0:
        return "neutral"
    return "positive" if value > 0 else "negative"


def _percent_width(value: int, maximum: int) -> str:
    return f"{value / maximum * 100:.1f}%"


def context_card(coverage: CoverageSummary, timezone: str) -> dmc.Paper:
    return dmc.Paper(
        [
            panel_title("Context"),
            dmc.SimpleGrid(
                [
                    _context_stat("Total episodes", f"{coverage.total:,}"),
                    _context_stat("Date range", coverage.date_range),
                    _context_stat("Timezone", timezone),
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
    bin_labels = (
        ("< -100", "-100 – -25", "-25 – 0", "0 – 25", "25 – 100", "> 100")
        if len(item.distribution) == 6
        else tuple(f"Bin {index}" for index in range(1, len(item.distribution) + 1))
    )
    distribution = go.Figure(
        go.Bar(
            x=list(bin_labels),
            y=list(item.distribution),
            marker_color=[
                COLORS["red"] if index < len(item.distribution) / 2 else COLORS["green"]
                for index in range(len(item.distribution))
            ],
            hovertemplate=f"%{{x}} {currency}<br>%{{y}} episodes<extra></extra>",
        )
    )
    _style_figure(distribution, height=76, margins={"l": 0, "r": 0, "t": 2, "b": 28})
    distribution.update_xaxes(tickfont={"size": 6}, showgrid=False)
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
            dmc.Text("Cashflow distribution", className="year-label"),
            (
                dcc.Graph(
                    figure=distribution,
                    config={"displayModeBar": False},
                    className="year-distribution",
                )
                if item.distribution
                else dmc.Text("No comparable cashflows", className="micro-copy")
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
            "instrument": episode.instrument,
            "profile": episode.profile,
            "market_class": episode.market_class,
            "direction": episode.direction,
            "status": episode.status,
            "duration": episode.duration,
            "duration_bucket": episode.duration_bucket,
            "entry": episode.entry,
            "exit": episode.exit,
            "net_result": episode.net_result,
            "currency": episode.result_currency,
            "outcome": episode.outcome,
            "tags": " · ".join(episode.tags),
        }
        for episode in episodes
    ]


def episode_detail(episode: Episode | None) -> dmc.Box:
    if episode is None:
        return empty_state("Select an episode to inspect executions and cashflows")
    return dmc.Box(
        [
            dmc.Group(
                [
                    dmc.Box(
                        [
                            dmc.Text(_episode_title(episode), className="detail-title"),
                            dmc.Text(
                                (
                                    f"{episode.instrument}  ·  {episode.profile}  ·  "
                                    f"{episode.market_class}"
                                ),
                                className="micro-copy",
                            ),
                            dmc.Text(
                                f"{episode.occurred_at}  ·  {episode.direction}  ·  "
                                f"{episode.duration}",
                                className="micro-copy",
                            ),
                        ]
                    ),
                    dmc.Badge(
                        episode.status,
                        color="blue" if episode.status.lower() == "open" else "gray",
                        variant="outline",
                    ),
                    dmc.Badge(
                        _episode_result(episode),
                        color=(
                            "green"
                            if episode.net_result is not None and episode.net_result > 0
                            else "red"
                            if episode.net_result is not None and episode.net_result < 0
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
                            dmc.TabsTab(
                                f"Executions ({len(episode.executions)})", value="executions"
                            ),
                            dmc.TabsTab(f"Cashflows ({len(episode.cashflows)})", value="cashflows"),
                            dmc.TabsTab("Quality", value="quality"),
                        ]
                    ),
                    dmc.TabsPanel(
                        [
                            dmc.SimpleGrid(
                                [
                                    _detail_stat("Net result", _episode_result(episode)),
                                    _detail_stat("Entry", episode.entry),
                                    _detail_stat("Exit", episode.exit),
                                    _detail_stat("Duration", episode.duration),
                                    _detail_stat("Direction", episode.direction),
                                    _detail_stat("Status", episode.status),
                                ],
                                cols={"base": 2, "sm": 3},
                                spacing="sm",
                            ),
                            dmc.Text("Tags", className="detail-section-title"),
                            dmc.Group(
                                [dmc.Badge(tag, variant="outline") for tag in episode.tags],
                                gap="xs",
                            ),
                        ],
                        value="overview",
                    ),
                    dmc.TabsPanel(
                        (
                            [
                                dmc.Text(
                                    "Execution price timeline",
                                    className="detail-section-title",
                                ),
                                dcc.Graph(
                                    figure=_execution_figure(episode.executions),
                                    config={"displayModeBar": False, "responsive": True},
                                ),
                                _execution_table(episode.executions),
                            ]
                            if episode.executions
                            else empty_state("No execution rows are linked to this episode")
                        ),
                        value="executions",
                    ),
                    dmc.TabsPanel(
                        (
                            [
                                dmc.Text(
                                    "Reporting cashflows",
                                    className="detail-section-title",
                                ),
                                dcc.Graph(
                                    figure=_cashflow_figure(episode.cashflows),
                                    config={"displayModeBar": False, "responsive": True},
                                ),
                                _cashflow_table(episode.cashflows),
                            ]
                            if episode.cashflows
                            else empty_state("No attributed cashflows are linked to this episode")
                        ),
                        value="cashflows",
                    ),
                    dmc.TabsPanel(
                        [
                            dmc.Text("Quality indicators", className="detail-section-title"),
                            _quality_rows(episode.quality),
                            dmc.Group(
                                [
                                    dmc.Text("Confidence", className="micro-copy"),
                                    dmc.Badge(
                                        episode.confidence,
                                        color=(
                                            "yellow"
                                            if episode.confidence.lower().startswith("low")
                                            else "green"
                                        ),
                                        variant="light",
                                        size="sm",
                                    ),
                                    dmc.Text("Executions", className="micro-copy", ml="auto"),
                                    dmc.Text(str(episode.samples), className="detail-value"),
                                ],
                                className="detail-footer",
                            ),
                        ],
                        value="quality",
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


def _episode_result(episode: Episode) -> str:
    if episode.net_result is None:
        return "Pending" if episode.status.lower() == "open" else "Not attributed"
    return f"{episode.net_result:+,.2f} {episode.result_currency}".strip()


def _detail_stat(label: str, value: str) -> dmc.Paper:
    return dmc.Paper(
        [
            dmc.Text(label, className="quality-label"),
            dmc.Text(value or "—", className="detail-stat-value"),
        ],
        className="detail-stat",
    )


def _execution_table(items: Iterable[ExecutionDetail]) -> object:
    rows = list(items)
    if not rows:
        return empty_state("No execution rows are linked to this episode")
    return _detail_table(
        ("Time", "Transition", "Side", "Price", "Quantity", "Notional", "Fee", "Order"),
        (
            (
                item.occurred_at,
                item.transition,
                item.side,
                item.price,
                f"{item.quantity} {item.quantity_unit}".strip(),
                item.notional or "—",
                f"{item.fee} {item.fee_currency}".strip() or "—",
                item.order_id or "—",
            )
            for item in rows
        ),
    )


def _execution_figure(items: Iterable[ExecutionDetail]) -> go.Figure:
    rows = list(items)
    observed = [(item, _number(item.price)) for item in rows]
    observed = [(item, price) for item, price in observed if price is not None]
    if not observed:
        return empty_figure("Price values are absent from the linked execution rows", height=190)
    price_decimals = _price_decimals(price for _item, price in observed)
    figure = go.Figure(
        go.Scatter(
            x=[item.occurred_at for item, _price in observed],
            y=[price for _item, price in observed],
            mode="lines+markers",
            line={"color": COLORS["blue"], "width": 1.4},
            marker={
                "size": 8,
                "color": [
                    COLORS["green"]
                    if "open" in item.transition.lower()
                    else COLORS["red"]
                    if "close" in item.transition.lower()
                    else COLORS["blue"]
                    for item, _price in observed
                ],
                "symbol": [
                    "triangle-up"
                    if "open" in item.transition.lower()
                    else "triangle-down"
                    if "close" in item.transition.lower()
                    else "circle"
                    for item, _price in observed
                ],
            },
            customdata=[
                [
                    item.transition,
                    item.side,
                    item.quantity,
                    item.quantity_unit,
                    f"{price:,.{price_decimals}f}",
                ]
                for item, price in observed
            ],
            hovertemplate=(
                "%{x}<br>Price %{customdata[4]}<br>%{customdata[0]} · %{customdata[1]}"
                "<br>Quantity %{customdata[2]} %{customdata[3]}<extra></extra>"
            ),
        )
    )
    _style_figure(figure, height=190, margins={"l": 48, "r": 8, "t": 8, "b": 36})
    figure.update_yaxes(
        title_text="Execution price",
        tickformat=f",.{price_decimals}f",
    )
    return figure


def _cashflow_table(items: Iterable[CashflowDetail]) -> object:
    rows = list(items)
    if not rows:
        return empty_state("No attributed cashflows are linked to this episode")
    return _detail_table(
        ("Time", "Event", "Source amount", "Reporting amount", "Method"),
        (
            (
                item.occurred_at,
                item.event_type,
                f"{item.amount} {item.currency}".strip(),
                f"{item.reporting_amount} {item.reporting_currency}".strip(),
                item.method,
            )
            for item in rows
        ),
    )


def _cashflow_figure(items: Iterable[CashflowDetail]) -> go.Figure:
    rows = list(items)
    observed = [(item, _number(item.reporting_amount)) for item in rows]
    observed = [(item, amount) for item, amount in observed if amount is not None]
    if not observed:
        return empty_figure(
            "Reporting amounts are absent from the attributed cashflow rows",
            height=190,
        )
    figure = go.Figure(
        go.Bar(
            x=[item.occurred_at for item, _amount in observed],
            y=[amount for _item, amount in observed],
            marker_color=[
                COLORS["green"] if amount >= 0 else COLORS["red"] for _item, amount in observed
            ],
            customdata=[
                [item.event_type, item.reporting_currency, item.method]
                for item, _amount in observed
            ],
            hovertemplate=(
                "%{x}<br>%{customdata[0]} %{y:,.2f} %{customdata[1]}"
                "<br>%{customdata[2]}<extra></extra>"
            ),
        )
    )
    _style_figure(figure, height=190, margins={"l": 48, "r": 8, "t": 8, "b": 36})
    figure.update_yaxes(title_text="Reporting amount")
    return figure


def _detail_table(headers: tuple[str, ...], rows: Iterable[tuple[str, ...]]) -> html.Div:
    return html.Div(
        html.Table(
            [
                html.Thead(html.Tr([html.Th(header) for header in headers])),
                html.Tbody([html.Tr([html.Td(value) for value in row]) for row in rows]),
            ],
            className="detail-data-table",
        ),
        className="detail-table-scroll",
    )


def _number(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _price_decimals(values: Iterable[float]) -> int:
    magnitudes = [abs(value) for value in values if value]
    if not magnitudes:
        return 2
    smallest = min(magnitudes)
    if smallest >= 100:
        return 2
    if smallest >= 1:
        return 4
    if smallest >= 0.01:
        return 6
    if smallest >= 0.0001:
        return 8
    return 10


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
    build: BuildSummary,
) -> list[dmc.Paper]:
    complete = coverage.percent >= 99.999
    return [
        dmc.Paper(
            [
                panel_title("Source interval coverage"),
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
                dmc.Text("Observed source intervals marked complete", className="quality-copy"),
                dmc.Text(f"{coverage.complete:,} / {coverage.total:,}", className="quality-ratio"),
                dmc.Progress(value=coverage.percent, color="green", size="sm", mt="lg"),
            ],
            className="panel quality-card",
        ),
        dmc.Paper(
            [
                panel_title("Reconstructed boundaries"),
                dmc.Group(
                    [
                        dmc.Text(
                            f"{coverage.boundary_complete:,}",
                            className="quality-hero tone-info",
                        ),
                        dmc.Badge("Closed and complete", color="blue", variant="light"),
                    ],
                    align="center",
                ),
                dmc.SimpleGrid(
                    [
                        _context_stat("Open episodes", f"{coverage.open_episodes:,}"),
                        _context_stat("Left-censored", f"{coverage.left_censored:,}"),
                    ],
                    cols=2,
                    spacing="sm",
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


def collection_build_report(gaps: Iterable[CoverageGap]) -> dmc.Accordion:
    rows = list(gaps)
    content: object
    if rows:
        content = _detail_table(
            ("Profile", "Market class", "Start", "End", "Status", "Reason", "Source"),
            (
                (
                    item.profile,
                    item.market_class,
                    item.start,
                    item.end,
                    item.status,
                    item.reason,
                    item.source,
                )
                for item in rows
            ),
        )
    else:
        content = dmc.Text(
            "No exact source-coverage gaps are reported for the current selection.",
            className="quality-copy",
        )
    return dmc.Accordion(
        dmc.AccordionItem(
            [
                dmc.AccordionControl(
                    dmc.Group(
                        [
                            dmc.Text("Collection build report", className="panel-title"),
                            dmc.Badge(
                                f"{len(rows):,} gaps" if rows else "No gaps",
                                color="yellow" if rows else "green",
                                variant="light",
                            ),
                        ],
                        gap="sm",
                    )
                ),
                dmc.AccordionPanel(content),
            ],
            value="collection-build-report",
        ),
        value=None,
        className="collection-report panel",
    )


def _build_row(label: str, value: str) -> dmc.Group:
    return dmc.Group(
        [dmc.Text(label, className="quality-label"), dmc.Text(value, className="quality-copy")],
        justify="space-between",
        wrap="nowrap",
        mt=7,
    )


def panel_title(title: str) -> dmc.Group:
    return dmc.Group(
        dmc.Text(title, className="panel-title"),
        justify="space-between",
        wrap="nowrap",
        className="panel-title-row",
    )


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
        hoverlabel={
            "bgcolor": "#102b3b",
            "bordercolor": COLORS["blue"],
            "font": {
                "color": "#f4f9fc",
                "family": "Inter, system-ui, sans-serif",
                "size": 12,
            },
        },
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
