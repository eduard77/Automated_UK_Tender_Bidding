from tender_agent.adapters.atamis import AtamisAdapter
from tender_agent.adapters.base import SourceAdapter
from tender_agent.adapters.contracts_finder import ContractsFinderAdapter
from tender_agent.adapters.etendersni import ETendersNIAdapter
from tender_agent.adapters.eu_supply import EUSupplyAdapter
from tender_agent.adapters.fts import FTSAdapter
from tender_agent.adapters.pcs import PCSAdapter
from tender_agent.adapters.sell2wales import Sell2WalesAdapter

ADAPTERS: dict[str, type[SourceAdapter]] = {
    "FTS": FTSAdapter,
    "CF": ContractsFinderAdapter,
    "PCS": PCSAdapter,
    "S2W": Sell2WalesAdapter,
    "NI": ETendersNIAdapter,
    "EU_SUPPLY": EUSupplyAdapter,
    "ATAMIS": AtamisAdapter,
}

__all__ = [
    "ADAPTERS",
    "AtamisAdapter",
    "ContractsFinderAdapter",
    "ETendersNIAdapter",
    "EUSupplyAdapter",
    "FTSAdapter",
    "PCSAdapter",
    "Sell2WalesAdapter",
    "SourceAdapter",
]
