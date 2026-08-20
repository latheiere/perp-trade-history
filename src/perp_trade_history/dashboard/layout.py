from __future__ import annotations

import dash_ag_grid as dag
import dash_mantine_components as dmc
from dash import dcc

from perp_trade_history.dashboard.models import DashboardSnapshot
from perp_trade_history.dashboard.views import (
    context_card,
    data_quality_cards,
    episode_detail,
    episode_rows,
    exposure_panel,
    heatmap_figure,
    kpi_cards,
    notable_changes,
    panel_title,
    performance_figure,
    section_heading,
    year_cards,
)

EPISODE_COLUMNS = [
    {"field": "episode", "headerName": "Episode", "minWidth": 150, "pinned": "left"},
    {"field": "profile", "headerName": "Profile", "minWidth": 120},
    {"field": "direction", "headerName": "Direction", "minWidth": 90},
    {"field": "duration", "headerName": "Duration", "minWidth": 95},
    {"field": "entry", "headerName": "Entry", "minWidth": 74},
    {"field": "exit", "headerName": "Exit", "minWidth": 74},
    {"field": "r_multiple", "headerName": "R multiple", "minWidth": 105, "type": "numericColumn"},
    {"field": "result", "headerName": "Result", "minWidth": 82},
    {"field": "mae", "headerName": "MAE (R)", "minWidth": 90, "type": "numericColumn"},
    {"field": "mfe", "headerName": "MFE (R)", "minWidth": 90, "type": "numericColumn"},
    {"field": "tags", "headerName": "Tags", "minWidth": 170, "flex": 1},
]

GRID_THEME = (
    "themeQuartz.withParams({"
    "backgroundColor:'#0b1b27',foregroundColor:'#d9e7f0',accentColor:'#2ea8ff',"
    "headerTextColor:'#8194a3',headerBackgroundColor:'#0d202d',"
    "oddRowBackgroundColor:'#0d1f2b',borderColor:'#1b3342',"
    "rowBorderColor:'#1b3342',fontSize:11,spacing:5,wrapperBorderRadius:0"
    "})"
)


def build_layout(snapshot: DashboardSnapshot, *, refresh_interval_ms: int) -> dmc.MantineProvider:
    first_episode = snapshot.episodes[0] if snapshot.episodes else None
    return dmc.MantineProvider(
        dmc.Box(
            [
                dcc.Store(
                    id="snapshot-signal",
                    data={
                        "revision": snapshot.revision,
                        "status": snapshot.status,
                        "message": snapshot.message,
                        "sequence": 0,
                    },
                ),
                dcc.Interval(
                    id="snapshot-poll",
                    interval=refresh_interval_ms,
                    n_intervals=0,
                ),
                _command_bar(snapshot),
                _initial_state_banner(snapshot),
                dmc.Box(
                    [
                        dmc.SimpleGrid(
                            kpi_cards(snapshot.kpis),
                            id="kpi-grid",
                            cols={"base": 1, "xs": 2, "md": 4},
                            spacing=0,
                            className="kpi-grid",
                        ),
                        _executive_overview(snapshot),
                        _patterns(snapshot),
                        _across_years(snapshot),
                        _episodes(snapshot, first_episode),
                        _data_quality(snapshot),
                        dmc.Text(
                            f"All times shown in {snapshot.timezone}",
                            id="footer-timezone",
                            className="page-footer",
                        ),
                    ],
                    className="dashboard-scroll",
                ),
            ],
            className="dashboard-root",
        ),
        defaultColorScheme="dark",
        forceColorScheme="dark",
        theme={
            "primaryColor": "blue",
            "fontFamily": "Inter, ui-sans-serif, system-ui, sans-serif",
            "headings": {"fontFamily": "Inter, ui-sans-serif, system-ui, sans-serif"},
        },
    )


def _command_bar(snapshot: DashboardSnapshot) -> dmc.Box:
    return dmc.Box(
        dmc.Group(
            [
                dmc.Burger(
                    size="sm",
                    opened=False,
                    **{"aria-label": "Open navigation"},
                ),
                dmc.Select(
                    id="profile-filter",
                    data=[
                        {"label": option.label, "value": option.value}
                        for option in snapshot.profiles
                    ],
                    value="all",
                    allowDeselect=False,
                    w=150,
                    **{"aria-label": "Profile"},
                ),
                dmc.Select(
                    id="market-class-filter",
                    data=[
                        {"label": option.label, "value": option.value}
                        for option in snapshot.market_classes
                    ],
                    value="all",
                    allowDeselect=False,
                    w=165,
                    **{"aria-label": "Market class"},
                ),
                dmc.Button(snapshot.date_range, variant="default", className="date-range-control"),
                dmc.Select(
                    id="interval-filter",
                    data=[
                        {"label": "Monthly", "value": "month"},
                        {"label": "Quarterly", "value": "quarter"},
                        {"label": "Yearly", "value": "year"},
                    ],
                    value="month",
                    allowDeselect=False,
                    w=120,
                    **{"aria-label": "Aggregation interval"},
                ),
                dmc.Select(
                    id="timezone-filter",
                    data=list(dict.fromkeys((snapshot.timezone, "UTC"))),
                    value=snapshot.timezone,
                    allowDeselect=False,
                    w=175,
                    **{"aria-label": "Timezone"},
                ),
                dmc.Text(snapshot.refreshed_label, id="refresh-status", className="refresh-status"),
                dmc.Group(
                    [
                        dmc.Button("Theme", variant="subtle", size="compact-sm"),
                        dmc.Button("Settings", variant="subtle", size="compact-sm"),
                    ],
                    gap="xs",
                    ml="auto",
                    className="utility-actions",
                ),
            ],
            gap="sm",
            wrap="nowrap",
            className="command-bar-inner",
        ),
        className="command-bar",
    )


def _initial_state_banner(snapshot: DashboardSnapshot) -> dmc.Box:
    if snapshot.status == "ready":
        return dmc.Box(id="data-state-banner", className="data-state-banner is-hidden")
    return dmc.Box(
        dmc.Alert(
            snapshot.message or "Analytics data is unavailable for the current selection.",
            title="No matching data" if snapshot.status == "empty" else "Data unavailable",
            color="blue" if snapshot.status == "empty" else "red",
            variant="light",
        ),
        id="data-state-banner",
        className="data-state-banner",
    )


def _executive_overview(snapshot: DashboardSnapshot) -> dmc.Box:
    return dmc.Box(
        [
            section_heading(1, "Executive overview"),
            dmc.Grid(
                [
                    dmc.GridCol(
                        dmc.Paper(
                            [
                                dmc.Group(
                                    [
                                        panel_title("Performance history"),
                                        dmc.Group(
                                            [
                                                dmc.Select(
                                                    id="benchmark-filter",
                                                    data=(
                                                        ["Compare to benchmark", "Net only"]
                                                        if any(
                                                            point.benchmark is not None
                                                            for point in snapshot.performance
                                                        )
                                                        else ["Cumulative result only"]
                                                    ),
                                                    value=(
                                                        "Compare to benchmark"
                                                        if any(
                                                            point.benchmark is not None
                                                            for point in snapshot.performance
                                                        )
                                                        else "Cumulative result only"
                                                    ),
                                                    allowDeselect=False,
                                                    size="xs",
                                                    w=180,
                                                ),
                                                dmc.SegmentedControl(
                                                    id="horizon-filter",
                                                    data=[
                                                        {"label": "1Y", "value": "1y"},
                                                        {"label": "2Y", "value": "2y"},
                                                        {"label": "3Y", "value": "3y"},
                                                        {"label": "All", "value": "all"},
                                                    ],
                                                    value="all",
                                                    size="xs",
                                                ),
                                            ],
                                            gap="sm",
                                            className="performance-controls",
                                        ),
                                    ],
                                    justify="space-between",
                                    align="flex-start",
                                ),
                                dcc.Graph(
                                    id="performance-graph",
                                    figure=performance_figure(snapshot),
                                    config={"displayModeBar": False, "responsive": True},
                                ),
                                dmc.Text(
                                    _coverage_caption(snapshot),
                                    id="performance-caption",
                                    className="coverage-caption",
                                ),
                            ],
                            className="panel performance-panel",
                        ),
                        span={"base": 12, "lg": 8},
                    ),
                    dmc.GridCol(
                        dmc.Paper(
                            [
                                panel_title("Notable changes", "View all"),
                                dmc.Box(
                                    notable_changes(snapshot.notable_changes),
                                    id="notable-changes",
                                    className="notable-list",
                                ),
                            ],
                            className="panel notable-panel",
                        ),
                        span={"base": 12, "lg": 4},
                    ),
                ],
                gutter="md",
            ),
        ],
        className="dashboard-section",
    )


def _patterns(snapshot: DashboardSnapshot) -> dmc.Box:
    return dmc.Box(
        [
            section_heading(2, "Patterns"),
            dmc.Grid(
                [
                    dmc.GridCol(
                        dmc.Stack(
                            [
                                dmc.Paper(
                                    [
                                        panel_title(snapshot.heatmap.metric_label),
                                        dcc.Graph(
                                            id="heatmap-graph",
                                            figure=heatmap_figure(snapshot),
                                            config={"displayModeBar": False, "responsive": True},
                                        ),
                                    ],
                                    className="panel heatmap-panel",
                                ),
                                dmc.Box(
                                    context_card(snapshot.coverage, snapshot.timezone),
                                    id="context-card",
                                ),
                            ],
                            gap="md",
                        ),
                        span={"base": 12, "lg": 4},
                    ),
                    dmc.GridCol(
                        dmc.Paper(
                            [
                                panel_title("Direction & exposure duration (by episode)"),
                                dmc.Box(
                                    exposure_panel(
                                        snapshot.exposure,
                                        snapshot.exposure_metric_label,
                                        snapshot.exposure_net_label,
                                    ),
                                    id="exposure-panel",
                                ),
                            ],
                            className="panel exposure-panel",
                        ),
                        span={"base": 12, "lg": 8},
                    ),
                ],
                gutter="md",
            ),
        ],
        className="dashboard-section",
    )


def _across_years(snapshot: DashboardSnapshot) -> dmc.Box:
    return dmc.Box(
        [
            section_heading(3, "Across years"),
            dmc.SimpleGrid(
                year_cards(snapshot.years, snapshot.currency),
                id="year-grid",
                cols={"base": 1, "xs": 2, "md": 3, "lg": 6},
                spacing=0,
                className="year-grid",
            ),
            dmc.Text(
                f"Year boundaries follow {snapshot.timezone}",
                className="year-boundary",
            ),
        ],
        className="dashboard-section panel year-section",
    )


def _episodes(snapshot: DashboardSnapshot, selected: object) -> dmc.Box:
    rows = episode_rows(snapshot.episodes)
    return dmc.Box(
        [
            section_heading(4, "Episodes"),
            dmc.Grid(
                [
                    dmc.GridCol(
                        dmc.Paper(
                            [
                                dmc.Group(
                                    [
                                        dmc.Select(
                                            id="direction-filter",
                                            data=["All directions", "Long", "Short"],
                                            value="All directions",
                                            allowDeselect=False,
                                            size="xs",
                                            w=130,
                                        ),
                                        dmc.Select(
                                            id="duration-filter",
                                            data=[
                                                {"label": "All durations", "value": "all"},
                                                {"label": "Under 1 hour", "value": "short"},
                                                {"label": "1 hour – 1 day", "value": "medium"},
                                                {"label": "Over 1 day", "value": "long"},
                                            ],
                                            value="all",
                                            allowDeselect=False,
                                            size="xs",
                                            w=135,
                                        ),
                                        dmc.Select(
                                            id="outcome-filter",
                                            data=["All outcomes", "Win", "Loss"],
                                            value="All outcomes",
                                            allowDeselect=False,
                                            size="xs",
                                            w=125,
                                        ),
                                        dmc.TextInput(
                                            id="episode-search",
                                            placeholder="Search episodes…",
                                            size="xs",
                                            className="episode-search",
                                        ),
                                        dmc.Text(
                                            f"Showing 1–{min(20, len(rows))} of {len(rows):,}",
                                            id="episode-count",
                                            className="micro-copy episode-count",
                                        ),
                                        dmc.Button("Export", variant="subtle", size="compact-xs"),
                                    ],
                                    gap="xs",
                                    className="episode-toolbar",
                                ),
                                dag.AgGrid(
                                    id="episode-grid",
                                    rowData=rows,
                                    columnDefs=EPISODE_COLUMNS,
                                    defaultColDef={
                                        "sortable": True,
                                        "filter": True,
                                        "resizable": True,
                                    },
                                    columnSize="responsiveSizeToFit",
                                    getRowId="params.data.episode_id",
                                    selectedRows=rows[:1],
                                    dashGridOptions={
                                        "theme": {"function": GRID_THEME},
                                        "animateRows": False,
                                        "pagination": True,
                                        "paginationPageSize": 10,
                                        "paginationPageSizeSelector": [10, 20, 50],
                                        "suppressCellFocus": True,
                                        "rowSelection": {
                                            "mode": "singleRow",
                                            "checkboxes": False,
                                            "enableClickSelection": True,
                                        },
                                    },
                                    style={"height": 530, "width": "100%"},
                                ),
                            ],
                            className="panel episode-ledger",
                        ),
                        span={"base": 12, "lg": 8},
                    ),
                    dmc.GridCol(
                        dmc.Paper(
                            dmc.Box(episode_detail(selected), id="episode-detail"),
                            className="panel detail-panel",
                        ),
                        span={"base": 12, "lg": 4},
                    ),
                ],
                gutter="md",
            ),
        ],
        className="dashboard-section",
    )


def _data_quality(snapshot: DashboardSnapshot) -> dmc.Box:
    return dmc.Box(
        [
            section_heading(5, "Data quality"),
            dmc.SimpleGrid(
                data_quality_cards(snapshot.coverage, snapshot.unsupported_metrics, snapshot.build),
                id="data-quality-grid",
                cols={"base": 1, "md": 3},
                spacing="md",
            ),
        ],
        className="dashboard-section",
    )


def _coverage_caption(snapshot: DashboardSnapshot) -> str:
    if not snapshot.coverage.total:
        return "Coverage unavailable"
    return f"{snapshot.coverage.percent:.1f}% complete coverage"
