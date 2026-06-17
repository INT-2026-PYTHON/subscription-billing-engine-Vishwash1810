"""
build_invoice — PURE function that turns inputs into an Invoice dataclass.

⚠️ NO database calls here. No `datetime.now()`. No PDF. Just math.

The order is FIXED:
    1. base       = strategy.calculate(usage)
    2. discount   = discount.apply(base) if discount else 0
    3. taxable    = base - discount
    4. tax        = tax_calc.apply(taxable)
    5. total      = taxable + tax.total
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from billing_engine.money import Money
from billing_engine.models import (
    Invoice, InvoiceStatus, InvoiceLineItem, LineItemKind, Subscription, Plan,
)
from billing_engine.pricing.base import PricingStrategy
from billing_engine.discounts.base import Discount, DiscountContext
from billing_engine.taxes.base import TaxCalculator, TaxContext


def build_invoice(
    subscription: Subscription,
    plan: Plan,
    strategy: PricingStrategy,
    discount: Optional[Discount],
    tax_calc: TaxCalculator,
    tax_context: TaxContext,
    usage_quantity: int,
    period_start: date,
    period_end: date,
    invoice_count_so_far: int,
) -> Invoice:
    """Pure function. Returns an Invoice (id=None, status=DRAFT) ready to be persisted."""
    # Step 1: Compute base charge
    subtotal = strategy.calculate(usage_quantity)
    
    # Step 2: Apply discount if present
    discount_context = DiscountContext(invoice_count_so_far=invoice_count_so_far)
    discount_total = Money.zero(subtotal.currency)
    if discount:
        discount_total = discount.apply(subtotal, discount_context)
    
    # Step 3: Compute taxable amount
    taxable = subtotal - discount_total
    
    # Step 4: Apply tax
    tax_breakdown = tax_calc.apply(taxable, tax_context)
    
    # Step 5: Build line items
    line_items = []
    line_items.append(InvoiceLineItem(
        id=None, invoice_id=None,
        description="Base charge", amount=subtotal, kind=LineItemKind.BASE
    ))
    
    if discount and not discount_total.is_zero():
        line_items.append(InvoiceLineItem(
            id=None, invoice_id=None,
            description="Discount", amount=discount_total, kind=LineItemKind.DISCOUNT
        ))
    
    for component_label, component_amount in tax_breakdown.components:
        line_items.append(InvoiceLineItem(
            id=None, invoice_id=None,
            description=component_label, amount=component_amount, kind=LineItemKind.TAX
        ))
    
    # Step 6: Build and return invoice
    total = taxable + tax_breakdown.total
    return Invoice(
        id=None, subscription_id=subscription.id, period_start=period_start, period_end=period_end,
        subtotal=subtotal, discount_total=discount_total,
        tax_total=tax_breakdown.total, total=total,
        status=InvoiceStatus.DRAFT, line_items=line_items
    )
