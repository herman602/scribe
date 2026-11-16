"""Bank-specific transaction parsers."""

from app.infrastructure.parsers.banks.cathay import CathayTransactionParser
from app.infrastructure.parsers.banks.first_bank import FirstBankTransactionParser
from app.infrastructure.parsers.banks.fubon import FubonTransactionParser
from app.infrastructure.parsers.banks.hncb import HncbTransactionParser
from app.infrastructure.parsers.banks.sinopac import SinopacTransactionParser
from app.infrastructure.parsers.banks.standard_chartered import (
    StandardCharteredTransactionParser,
)
from app.infrastructure.parsers.banks.sunny import SunnyTransactionParser

__all__ = [
    "CathayTransactionParser",
    "FirstBankTransactionParser",
    "FubonTransactionParser",
    "HncbTransactionParser",
    "SinopacTransactionParser",
    "StandardCharteredTransactionParser",
    "SunnyTransactionParser",
]
