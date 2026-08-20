from perp_trade_history.text_report import build_parser, render_text_report


def test_text_report_mode_defaults_to_compact_and_accepts_verbose() -> None:
    parser = build_parser()
    assert parser.parse_args([]).compact is True
    assert parser.parse_args(["--compact"]).compact is True
    assert parser.parse_args(["--verbose"]).compact is False


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
