"""
BillingCycle — finds due subscriptions, generates invoices, posts ledger DEBITs,
advances the subscription period. Must be IDEMPOTENT (safe to run twice).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional
import sqlite3

from billing_engine.billing.pipeline import build_invoice
from billing_engine.billing.proration import compute_proration
from billing_engine.db import (
    Database,
    CustomerRepository, PlanRepository, SubscriptionRepository,
    UsageRecordRepository, InvoiceRepository, InvoiceLineItemRepository,
    LedgerRepository,
)
from billing_engine.models import (
    Subscription, SubscriptionStatus, InvoiceStatus, LedgerDirection, LedgerEntry, InvoiceLineItem, LineItemKind, Invoice
)


@dataclass
class BillingResult:
    invoices_created: int
    invoices_skipped_duplicate: int
    trials_activated: int


def _add_months(d: date, months: int) -> date:
    """Helper to add months to a date (for monthly billing periods)."""
    month = d.month + months
    year = d.year
    while month > 12:
        month -= 12
        year += 1
    # Handle day-of-month overflow (e.g., Jan 31 + 1 month)
    day = d.day
    while True:
        try:
            return date(year, month, day)
        except ValueError:
            day -= 1


class BillingCycle:
    """Day-3 deliverable. Day-4 stretch: add `upgrade_subscription(...)`."""

    def __init__(
        self,
        db: Database,
        customer_repo: CustomerRepository,
        plan_repo: PlanRepository,
        subscription_repo: SubscriptionRepository,
        usage_repo: UsageRecordRepository,
        invoice_repo: InvoiceRepository,
        line_item_repo: InvoiceLineItemRepository,
        ledger_repo: LedgerRepository,
        strategy_factory: Callable,    # given a Plan, returns a PricingStrategy
        discount_factory: Callable,    # given a discount_id or None, returns a Discount or None
        tax_factory: Callable,         # given a Customer, returns (TaxCalculator, TaxContext)
    ) -> None:
        self.db = db
        self.customer_repo = customer_repo
        self.plan_repo = plan_repo
        self.subscription_repo = subscription_repo
        self.usage_repo = usage_repo
        self.invoice_repo = invoice_repo
        self.line_item_repo = line_item_repo
        self.ledger_repo = ledger_repo
        self.strategy_factory = strategy_factory
        self.discount_factory = discount_factory
        self.tax_factory = tax_factory

    # --------------------------------------------------------
    def run(self, as_of: date) -> BillingResult:
        """Bill all subscriptions whose current period ends on or before `as_of`."""
        invoices_created = 0
        invoices_skipped = 0
        trials_activated = 0

        # Step 1: Activate trial subscriptions
        for sub in self.subscription_repo.list_all():
            if (sub.status == SubscriptionStatus.TRIAL and 
                sub.trial_end and sub.trial_end <= as_of):
                self.subscription_repo.update_status(sub.id, SubscriptionStatus.ACTIVE, None)
                trials_activated += 1

        # Step 2: Process due subscriptions
        due = self.subscription_repo.get_due_for_billing(as_of)
        for sub in due:
            # Get customer, plan, and factories
            customer = self.customer_repo.get(sub.customer_id)
            plan = self.plan_repo.get(sub.plan_id)
            
            # Get strategy, discount, and tax calculator
            strategy = self.strategy_factory(plan)
            discount = self.discount_factory(sub.discount_id)
            tax_calc, tax_context = self.tax_factory(customer)
            
            # Get usage for the billing period
            usage_quantity = self.usage_repo.sum_for_period(
                sub.id, plan.pricing_type.value.lower(), 
                sub.current_period_start, sub.current_period_end
            )
            
            # Get invoice count so far for FirstMonthFree discount context
            invoice_count = self.invoice_repo.count_for_subscription(sub.id)
            
            # Build the invoice
            invoice = build_invoice(
                subscription=sub,
                plan=plan,
                strategy=strategy,
                discount=discount,
                tax_calc=tax_calc,
                tax_context=tax_context,
                usage_quantity=usage_quantity,
                period_start=sub.current_period_start,
                period_end=sub.current_period_end,
                invoice_count_so_far=invoice_count,
            )
            
            # Mark as ISSUED (not DRAFT)
            invoice.status = InvoiceStatus.ISSUED
            
            # Try to save invoice and line items (catches duplicate period errors)
            try:
                # Save invoice
                saved_invoice = self.invoice_repo.add(invoice)
                
                # Save line items with invoice_id set
                for line_item in invoice.line_items:
                    line_item_with_id = InvoiceLineItem(
                        id=None,
                        invoice_id=saved_invoice.id,
                        description=line_item.description,
                        amount=line_item.amount,
                        kind=line_item.kind,
                    )
                    self.line_item_repo.add(line_item_with_id)
                
                # Post ledger DEBIT
                ledger_entry = LedgerEntry(
                    id=None,
                    invoice_id=saved_invoice.id,
                    customer_id=customer.id,
                    amount=invoice.total,
                    direction=LedgerDirection.DEBIT,
                    reason="Invoice issued",
                )
                self.ledger_repo.add(ledger_entry)
                
                # Advance the subscription period
                next_start = sub.current_period_end
                next_end = _add_months(next_start, 1)
                self.subscription_repo.update_period(sub.id, next_start, next_end)
                
                invoices_created += 1
            except sqlite3.IntegrityError:
                # Duplicate (subscription_id, period_start) already exists
                invoices_skipped += 1

        return BillingResult(invoices_created, invoices_skipped, trials_activated)

    # --------------------------------------------------------
    def upgrade_subscription(self, subscription_id: int, new_plan_id: int, switch_date: date) -> None:
        """Mid-cycle upgrade — Day 4 stretch."""
        # Load subscription, old plan, new plan, and customer
        sub = self.subscription_repo.get(subscription_id)
        if not sub:
            raise ValueError(f"Subscription {subscription_id} not found")
        
        old_plan = self.plan_repo.get(sub.plan_id)
        new_plan = self.plan_repo.get(new_plan_id)
        customer = self.customer_repo.get(sub.customer_id)
        
        # Get pricing strategies for both plans
        old_strategy = self.strategy_factory(old_plan)
        new_strategy = self.strategy_factory(new_plan)
        
        # Get tax calculator
        tax_calc, tax_context = self.tax_factory(customer)
        
        # Compute old and new plan prices (for a full period usage)
        # For flat-rate plans, calculate(1) gives the full price
        old_plan_price = old_strategy.calculate(1)
        new_plan_price = new_strategy.calculate(1)
        
        # Call compute_proration
        proration = compute_proration(
            old_plan_price=old_plan_price,
            new_plan_price=new_plan_price,
            period_start=sub.current_period_start,
            period_end=sub.current_period_end,
            switch_date=switch_date,
            tax_calc=tax_calc,
            tax_context=tax_context,
        )
        
        # Create a proration invoice with credit and charge line items
        line_items = []
        
        # Credit line item (negative amount)
        credit_total = proration.credit_amount + proration.credit_tax
        line_items.append(InvoiceLineItem(
            id=None, invoice_id=None,
            description=f"Credit for {old_plan.name} (remaining {(sub.current_period_end - switch_date).days} days)",
            amount=credit_total,
            kind=LineItemKind.PRORATION_CREDIT,
        ))
        
        # Charge line item (positive amount)
        charge_total = proration.charge_amount + proration.charge_tax
        line_items.append(InvoiceLineItem(
            id=None, invoice_id=None,
            description=f"Charge for {new_plan.name} (remaining {(sub.current_period_end - switch_date).days} days)",
            amount=charge_total,
            kind=LineItemKind.PRORATION_CHARGE,
        ))
        
        # Compute net amount (charge - credit)
        net_amount = charge_total - credit_total
        
        # Create invoice
        proration_invoice = Invoice(
            id=None,
            subscription_id=subscription_id,
            period_start=switch_date,
            period_end=sub.current_period_end,
            subtotal=proration.charge_amount,
            discount_total=proration.credit_amount,
            tax_total=proration.charge_tax - proration.credit_tax,
            total=net_amount,
            status=InvoiceStatus.ISSUED,
            line_items=line_items,
        )
        
        # Save invoice and line items
        saved_invoice = self.invoice_repo.add(proration_invoice)
        for line_item in line_items:
            new_line_item = InvoiceLineItem(
                id=None,
                invoice_id=saved_invoice.id,
                description=line_item.description,
                amount=line_item.amount,
                kind=line_item.kind,
            )
            self.line_item_repo.add(new_line_item)
        
        # Post ledger DEBIT for the net charge
        ledger_entry = LedgerEntry(
            id=None,
            invoice_id=saved_invoice.id,
            customer_id=sub.customer_id,
            amount=net_amount if net_amount.is_positive() else -net_amount,
            direction=LedgerDirection.DEBIT if net_amount.is_positive() else LedgerDirection.CREDIT,
            reason=f"Proration invoice for upgrade from {old_plan.name} to {new_plan.name}",
        )
        self.ledger_repo.add(ledger_entry)
        
        # Switch subscription's plan_id
        self.subscription_repo.update_plan(subscription_id, new_plan_id)
