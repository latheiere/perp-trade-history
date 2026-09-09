"""Exchange-specific, read-only perpetual history adapters."""

from perp_trade_history.adapters.base import VenueAdapter
from perp_trade_history.adapters.binance import BinanceAdapter
from perp_trade_history.adapters.gate import GateAdapter
from perp_trade_history.adapters.mexc import MexcAdapter

ADAPTERS: dict[str, type[VenueAdapter]] = {
    "binance": BinanceAdapter,
    "gate": GateAdapter,
    "mexc": MexcAdapter,
}
