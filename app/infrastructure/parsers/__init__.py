"""Deterministic local transaction parsers and classification pipeline."""

from app.infrastructure.parsers.banks import (
    CathayTransactionParser,
    FirstBankTransactionParser,
    FubonTransactionParser,
    HncbTransactionParser,
    SinopacTransactionParser,
    StandardCharteredTransactionParser,
    SunnyTransactionParser,
)
from app.infrastructure.parsers.generic import GenericTransactionParser
from app.infrastructure.parsers.pipeline import ClassificationPipeline
from app.infrastructure.parsers.registry import DefaultParserRegistry

__all__ = [
    "CathayTransactionParser",
    "ClassificationPipeline",
    "DefaultParserRegistry",
    "FirstBankTransactionParser",
    "FubonTransactionParser",
    "GenericTransactionParser",
    "HncbTransactionParser",
    "SinopacTransactionParser",
    "StandardCharteredTransactionParser",
    "SunnyTransactionParser",
]
