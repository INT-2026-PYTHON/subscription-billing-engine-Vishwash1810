"""
CLI entrypoint.

Subcommands to implement (Day 4):
    billing init                              -- create / migrate the DB
    billing customer add <name> <email> <country> [--state CODE]
    billing plan list
    billing subscribe <customer_id> <plan_id> [--trial-days N] [--discount CODE]
    billing bill run [--date YYYY-MM-DD]
    billing invoice show <invoice_id>          -- prints PLAIN TEXT invoice
    billing upgrade <subscription_id> <new_plan_id> [--date YYYY-MM-DD]   (STRETCH)
    billing demo                              -- run the scripted scenario

Use argparse with subparsers. Keep each subcommand handler in its own function.

PDF rendering is OUT OF SCOPE for the core project — `invoice show` should
print a clean PLAIN-TEXT invoice (see helper `format_invoice_text` below).
PDF generation is BONUS: see `billing_engine/pdf/renderer.py`.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from billing_engine.db.database import Database
from billing_engine.db.repository import (
    CustomerRepository, PlanRepository, SubscriptionRepository,
    UsageRecordRepository, InvoiceRepository, InvoiceLineItemRepository,
    LedgerRepository, PaymentAttemptRepository,
)
from billing_engine.models import Invoice, BillingPeriod, PricingType, SubscriptionStatus, Subscription
from billing_engine.money import Money


DB_PATH = Path.home() / ".billing_engine" / "billing.db"


def format_invoice_text(invoice: Invoice, customer_name: str, plan_name: str) -> str:
    """Render an invoice as a plain-text receipt. Pure function — easy to test."""
    lines = [
        "=" * 60,
        f"INVOICE #{invoice.id}".center(60),
        "=" * 60,
        f"Customer: {customer_name}",
        f"Plan:     {plan_name}",
        f"Period:   {invoice.period_start} to {invoice.period_end}",
        "-" * 60,
    ]
    
    # Line items
    for line_item in invoice.line_items:
        lines.append(f"{line_item.description:45} {line_item.amount}")
    
    lines.append("-" * 60)
    lines.append(f"{'TOTAL':45} {invoice.total}")
    lines.append(f"Status: {invoice.status.value}")
    lines.append("=" * 60)
    
    return "\n".join(lines)


def cmd_init(args) -> int:
    """Initialize the database."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = Database(str(DB_PATH))
    db.init_schema()
    print(f"✓ Database initialized at {DB_PATH}")
    return 0


def cmd_demo(args) -> int:
    """Run the demo scenario."""
    return run_demo()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="billing", description="Subscription Billing CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # Subcommands
    init_parser = sub.add_parser("init", help="initialize the database")
    init_parser.set_defaults(func=cmd_init)
    
    demo_parser = sub.add_parser("demo", help="run the demo scenario")
    demo_parser.set_defaults(func=cmd_demo)

    args = parser.parse_args(argv)
    return args.func(args)


def run_demo() -> int:
    """Scripted end-to-end scenario for the `demo` subcommand."""
    from datetime import datetime
    from billing_engine.billing.cycle import BillingCycle
    from billing_engine.billing.dunning import DunningProcess
    from billing_engine.models import Customer, Plan
    from billing_engine.pricing import FlatRate
    from billing_engine.taxes import NoTax, TaxContext
    from billing_engine.payments.gateway import ScriptedGateway, PaymentResult
    
    # Ensure DB is initialized
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = Database(str(DB_PATH))
    if not DB_PATH.exists():
        db.init_schema()
    
    # Set up repositories
    customers = CustomerRepository(db)
    plans = PlanRepository(db)
    subscriptions = SubscriptionRepository(db)
    invoices = InvoiceRepository(db)
    line_items = InvoiceLineItemRepository(db)
    ledger = LedgerRepository(db)
    attempts = PaymentAttemptRepository(db)
    usage = UsageRecordRepository(db)
    
    # Create customer
    print("\n=== DEMO: Subscription Billing System ===\n")
    print("1. Creating customer Alice...")
    cust = customers.add(Customer(None, "Alice", "alice@example.com", "AE"))
    print(f"   ✓ Customer created (ID: {cust.id})")
    
    # Create plan
    print("2. Creating Pro plan (₹1000/month)...")
    plan = plans.add(Plan(None, "Pro", PricingType.FLAT, BillingPeriod.MONTHLY, "INR"))
    print(f"   ✓ Plan created (ID: {plan.id})")
    
    # Create subscription
    print("3. Creating active subscription...")
    sub = subscriptions.add(
        Subscription(
            None, cust.id, plan.id, SubscriptionStatus.ACTIVE,
            date(2026, 1, 1), date(2026, 2, 1),
        )
    )
    print(f"   ✓ Subscription created (ID: {sub.id})")
    
    # Run billing cycle
    print("4. Running billing cycle on 2026-02-01...")
    cycle = BillingCycle(
        db=db,
        customer_repo=customers,
        plan_repo=plans,
        subscription_repo=subscriptions,
        usage_repo=usage,
        invoice_repo=invoices,
        line_item_repo=line_items,
        ledger_repo=ledger,
        strategy_factory=lambda p: FlatRate(Money("1000", "INR")),
        discount_factory=lambda d: None,
        tax_factory=lambda c: (NoTax(), TaxContext(customer_country="AE")),
    )
    result = cycle.run(as_of=date(2026, 2, 1))
    print(f"   ✓ Invoiced {result.invoices_created} subscription(s)")
    
    # Fetch invoice
    print("5. Processing payment...")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM invoices WHERE subscription_id=?", (sub.id,)
        ).fetchone()
    inv = invoices.get(row["id"])
    
    # Process payment
    dunning = DunningProcess(
        gateway=ScriptedGateway([PaymentResult(True)]),
        invoice_repo=invoices,
        ledger_repo=ledger,
        subscription_repo=subscriptions,
        attempt_repo=attempts,
    )
    outcome = dunning.attempt(inv, cust.id, datetime(2026, 2, 1, 10, 0))
    print(f"   ✓ Payment successful ({outcome.state.value})")
    
    # Show ledger
    print("6. Final ledger:")
    entries = ledger.list_for_customer(cust.id)
    for e in entries:
        print(f"   {e.direction.value:6} {e.amount}")
    
    print("\n=== DEMO COMPLETE ===\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
