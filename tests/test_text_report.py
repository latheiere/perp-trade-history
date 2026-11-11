from perp_trade_history.text_report import build_parser, render_text_report


def test_text_report_exposes_one_view_selector_and_keeps_existing_view_switches() -> None:
    parser = build_parser()
    assert parser.parse_args([]).view == "venue"
    for view in ("venue", "symbol", "contract"):
        assert parser.parse_args(["--view", view]).view == view
    assert parser.parse_args(["--compact"]).view == "venue"
    assert parser.parse_args(["--verbose"]).view == "contract"
    assert not any(flag in parser.format_help() for flag in ("--compact", "--verbose", "--secrets"))


def test_extra_columns_can_override_the_wrapper_default() -> None:
    parser = build_parser()
    assert parser.parse_args([]).extra_columns is True
    assert parser.parse_args(["--no-extra-columns"]).extra_columns is False
    assert parser.parse_args(["--no-extra-columns", "--extra-columns"]).extra_columns is True


def test_text_report_sorts_detail_rows_by_total_descending() -> None:
    rows = [
        {
            "venue": "gate",
            "symbol": "ASSET_A_USDT",
            "event_type": "realized_pnl",
            "currency": "USDT",
            "amount": "1",
            "events": 1,
            "coverage_status": "complete",
        },
        {
            "venue": "mexc",
            "symbol": "ASSET_B_USDT",
            "event_type": "realized_pnl",
            "currency": "USDT",
            "amount": "3.456",
            "events": 1,
            "coverage_status": "complete",
        },
    ]

    report = render_text_report(rows, venues={"gate", "mexc"}, period="none")

    assert report.index("ASSET_B") < report.index("ASSET_A")
    assert "ASSET_B_USDT" not in report
    assert "CURRENCY" not in report
    assert "3.46" in report
    assert "0.00" not in report


def test_venue_view_groups_contract_quotes_by_base_symbol_within_each_venue() -> None:
    rows = [
        {
            "venue": "binance",
            "symbol": "ASSETUSDT",
            "event_type": "realized_pnl",
            "currency": "USDT",
            "amount": "2",
            "events": 1,
            "coverage_status": "complete",
        },
        {
            "venue": "binance",
            "symbol": "ASSETBUSD",
            "event_type": "commission",
            "currency": "USDT",
            "amount": "-0.5",
            "events": 1,
            "coverage_status": "complete",
        },
    ]

    report = render_text_report(rows, venues={"binance"}, period="none")

    assert report.count("ASSET") == 1
    assert "TOTAL (USDT)" in report
    assert "1.50" in report


def test_contract_view_preserves_contract_symbols_and_currency_column() -> None:
    rows = [
        {
            "venue": "binance",
            "symbol": "ASSETUSDT",
            "event_type": "realized_pnl",
            "currency": "USDT",
            "amount": "2",
            "events": 1,
            "coverage_status": "complete",
        },
        {
            "venue": "binance",
            "symbol": "ASSETBUSD",
            "event_type": "commission",
            "currency": "USDT",
            "amount": "-0.5",
            "events": 1,
            "coverage_status": "complete",
        },
    ]

    report = render_text_report(
        rows,
        venues={"binance"},
        period="none",
        view="contract",
    )

    assert "CURRENCY" in report
    assert "ASSETUSDT" in report
    assert "ASSETBUSD" in report


def test_text_report_prints_other_breakdown_by_subtype() -> None:
    rows = [
        {
            "venue": "binance",
            "symbol": "(account)",
            "event_type": "other",
            "currency": "USDC",
            "amount": "78.64169571",
            "events": 168,
            "coverage_status": "complete",
            "other_event_breakdown": (
                '[{"event_subtype":"BFUSD_REWARD","events":168,"amount":"78.64169571"}]'
            ),
        }
    ]
    report = render_text_report(rows, venues={"binance"}, period="none")
    assert "OTHER breakdown by original subtype" in report
    assert "BFUSD_REWARD" in report
    assert "78.64" in report


def _report_table(report):
    header, rows = _report_table_rows(report)
    return header, rows[0]


def _report_table_rows(report):
    lines = report.splitlines()
    header_index = next(i for i, line in enumerate(lines) if line.startswith("PERIOD"))
    data_start = header_index + 2
    data_end = lines.index("", data_start) if "" in lines[data_start:] else len(lines)
    return lines[header_index].split(), [line.split() for line in lines[data_start:data_end]]


def test_symbol_view_combines_same_base_names_across_venues_within_each_period():
    first, second = "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"
    rows = [
        {"period_start": bucket, "venue": venue, "symbol": symbol, "event_type": event_type,
         "amount": amount, "events": events, "coverage_status": coverage}
        for bucket, venue, symbol, event_type, amount, events, coverage in (
            (first, "venue_a", "ASSET_A_USDT", "realized_pnl", "9", 2, "complete"),
            (first, "venue_b", "ASSET_AUSDC", "commission", "-1", 1, "incomplete"),
            (first, "venue_b", "1000ASSET_A_USDT", "funding", "2", 1, "complete"),
            (second, "venue_b", "ASSET_AUSDC", "funding", "3", 1, "complete"),
        )
    ]
    report = render_text_report(
        rows, venues={"venue_a", "venue_b", "venue_c"}, period="month", view="symbol",
        trade_counts={
            (first, "venue_a", "ASSET_A_USDT"): 2,
            (first, "venue_b", "ASSET_AUSDC"): 3,
            (first, "venue_a", "1000ASSET_A_USDT"): 1,
        },
        extra_columns=False,
    )
    header, table_rows = _report_table_rows(report)
    assert "VENUE" not in header
    assert all(venue not in report for venue in ("venue_a", "venue_b", "venue_c"))
    assert "1 of 3 selected venues" in report
    assert "Total (USDT): 13.00" in report
    by_key = {(row[0], row[1]): dict(zip(header, row, strict=True)) for row in table_rows}
    assert set(by_key) == {(first, "ASSET_A"), (first, "1000ASSET_A"), (second, "ASSET_A")}
    combined = by_key[first, "ASSET_A"]
    assert combined["REALIZED"] == "9.00"
    assert combined["COMMISSION"] == "-1.00"
    assert combined["TOTAL"] == "8.00"
    assert combined["TRADES"] == "5"
    assert combined["EVENTS"] == "3"
    assert combined["COVERAGE"] == "incomplete"
    assert by_key[first, "1000ASSET_A"]["COVERAGE"] == "unknown"
    assert by_key[second, "ASSET_A"]["COVERAGE"] == "complete"


def test_symbol_view_keeps_unattributed_cashflows_separate_from_contracts():
    rows = [
        {"venue": venue, "symbol": symbol, "event_type": "funding", "amount": "1", "events": 1}
        for venue, symbol in (("venue_a", ""), ("venue_b", "(account)"), ("venue_b", "ASSET_USDT"))
    ]
    report = render_text_report(rows, venues={"venue_a", "venue_b"}, period="none", view="symbol")
    header, table_rows = _report_table_rows(report)
    assert {row[header.index("SYMBOL")]: row[header.index("TOTAL")] for row in table_rows} == {
        "(account)": "2.00",
        "ASSET": "1.00",
    }


def test_trade_count_sits_between_total_and_events_and_is_not_repeated_per_component():
    rows = [
        {"venue": "binance", "symbol": symbol, "currency": "USDT",
         "event_type": component, "amount": "1", "events": 1}
        for symbol in ("ASSETUSDT", "ASSETBUSD")
        for component in ("realized_pnl", "funding", "commission")
    ]
    report = render_text_report(
        rows, venues={"binance"}, period="none",
        trade_counts={("all", "binance", "ASSETUSDT"): 2, ("all", "binance", "ASSETBUSD"): 3},
    )
    header, row = _report_table(report)
    assert header[header.index("TOTAL") + 1:header.index("EVENTS") + 1] == ["TRADES", "EVENTS"]
    assert row[header.index("TRADES")] == "5"
    assert row[header.index("EVENTS")] == "6"
    assert row[header.index("TOTAL")] == "6.00"


def test_hiding_extra_columns_keeps_total_and_event_count():
    rows = [
        {"venue": "venue", "symbol": "ASSET_USDT", "currency": "USDT",
         "event_type": component, "amount": "1", "events": 1}
        for component in ("realized_pnl", "reward", "settlement", "rebate", "bonus",
                          "premium", "insurance", "other")
    ]
    for view in ("venue", "symbol", "contract"):
        report = render_text_report(
            rows, venues={"venue"}, period="none", extra_columns=False, view=view,
            trade_counts={("all", "venue", "ASSET_USDT"): 2},
        )
        header, row = _report_table(report)
        assert not set(header) & {"REWARD", "SETTLEMENT", "REBATE", "BONUS", "PREMIUM",
                                  "INSURANCE", "OTHER"}
        assert row[header.index("TOTAL")] == "8.00"
        assert row[header.index("EVENTS")] == "8"
        assert row[header.index("TRADES")] == "2"
    header, _ = _report_table(render_text_report(rows, venues={"venue"}, period="none"))
    assert header[header.index("REWARD"):header.index("OTHER") + 1] == [
        "REWARD", "SETTLEMENT", "REBATE", "BONUS", "PREMIUM", "INSURANCE", "OTHER",
    ]


def test_trade_without_cashflow_still_appears_in_report():
    report = render_text_report(
        [], venues={"venue"}, period="none",
        trade_counts={("all", "venue", "ASSET_USDT"): 1},
    )
    header, row = _report_table(report)
    assert row[header.index("TOTAL")] == "0"
    assert row[header.index("EVENTS")] == "0"
    assert row[header.index("TRADES")] == "1"
