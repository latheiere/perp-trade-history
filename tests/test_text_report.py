from perp_trade_history.text_report import render_text_report


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

    assert report.index("ASSET_B_USDT") < report.index("ASSET_A_USDT")
    assert "3.46" in report
    assert "0.00" not in report


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
