"""Transaction reporting semantics shared by dashboard queries."""

from app.domain.enums import TransactionType

# Types treated as consumption rather than money movement or reimbursement.
SPENDING_TRANSACTION_TYPES = frozenset(
    {
        TransactionType.PURCHASE,
        TransactionType.BILL_PAYMENT,
        TransactionType.SUBSCRIPTION,
        TransactionType.FEE,
    }
)
