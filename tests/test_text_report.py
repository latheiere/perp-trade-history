from perp_trade_history.text_report import build_parser, render_text_report


def test_text_report_mode_defaults_to_compact_and_accepts_verbose() -> None:
    parser = build_parser()
    assert parser.parse_args([]).compact is True
    assert parser.parse_args(["--compact"]).compact is True
    assert parser.parse_args(["--verbose"]).compact is False
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


def test_compact_report_groups_contract_quotes_by_base_symbol_and_venue() -> None:
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


def test_verbose_report_preserves_contract_symbols_and_currency_column() -> None:
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
        compact=False,
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
    lines = report.splitlines()
    header_index = next(i for i, line in enumerate(lines) if line.startswith("PERIOD"))
    return lines[header_index].split(), lines[header_index + 2].split()


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
    for compact in (True, False):
        report = render_text_report(
            rows, venues={"venue"}, period="none", extra_columns=False, compact=compact,
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
