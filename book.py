"""Your ledger. This is the whole assignment.

`client.py` handles the network and hands you one event at a time. You return
the journal legs it produced. Some events correctly produce none: return an
empty list, not None-as-an-accident.

One event type is implemented as a worked example. The rest raise, with the rule
from PROTOCOL.md quoted in the message, so a practice run tells you exactly what
is left rather than silently scoring zero.

Two things to get right before anything else:

  * Use `Decimal`, never `float`. Money here does not always divide evenly, and
    a float implementation will disagree with us by a cent in places you will
    struggle to find.
  * Key balances by (customer, account), not by account. At least one event
    moves money between two customers on the same account, and an
    account-level book shows nothing wrong at all.
"""
from __future__ import annotations

from collections import defaultdict, deque
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

D = Decimal
ZERO = D("0.00")

# -- new accounts, Tasksheet.md section 2 ------------------------------------
# The eleven accounts already in use (1100..4200) are unchanged and stay as
# literals at their existing call sites; only the accounts introduced by
# Tasksheet.md are named here, for later passes to post to.
ACCT_CUSTODIAN_FEES_PAYABLE = "2420"
ACCT_PARTNER_SHARE_PAYABLE = "2430"
ACCT_CUSTODY_REVENUE = "4010"
ACCT_BROKERAGE_COST = "5000"
ACCT_CUSTODY_COST = "5010"
ACCT_PARTNER_REVENUE_SHARE = "5100"

# Per-broker tariff, Tasksheet.md section 4 ("The tariff"). bps figures are
# per unit of principal; brokerage is floored at min_fee; ticket is a flat
# per-fill cost on top. payable_account is that broker's own 241x account,
# where its accrued broker cost is mirrored on the credit side.
BROKER_TARIFF = {
    "BRK-A": {"asset_classes": {"equity", "etf"},
              "brokerage_bps": D("20"), "custody_bps": D("4"),
              "broker_cost_bps": D("9"), "custody_cost_bps": D("2"),
              "min_fee": D("1.00"), "ticket": D("0.35"),
              "payable_account": "2411"},
    "BRK-B": {"asset_classes": {"equity", "bond"},
              "brokerage_bps": D("15"), "custody_bps": D("5"),
              "broker_cost_bps": D("8"), "custody_cost_bps": D("3"),
              "min_fee": D("2.50"), "ticket": D("3.00"),
              "payable_account": "2412"},
    "BRK-C": {"asset_classes": {"etf", "bond"},
              "brokerage_bps": D("25"), "custody_bps": D("3"),
              "broker_cost_bps": D("12"), "custody_cost_bps": D("1"),
              "min_fee": D("0.50"), "ticket": D("0.20"),
              "payable_account": "2413"},
}

# Regulatory fee, Tasksheet.md section 4: 8 bps of principal, every fill.
REG_FEE_BPS = D("8")


def money(x: Decimal) -> Decimal:
    """2 decimal places, half away from zero. Not round(), which is half-even."""
    return x.quantize(D("0.01"), rounding=ROUND_HALF_UP)


def leg(account: str, customer_id: str, debit=ZERO, credit=ZERO) -> dict:
    return {"account": account, "customer_id": customer_id,
            "debit": str(money(D(debit))), "credit": str(money(D(credit)))}


class Book:
    def __init__(self) -> None:
        # balances[(customer_id, account)] = debit-positive balance
        self.balances: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        self.seen: set[str] = set()

        # withdrawal_id -> {"customer_id", "amount"}, recorded from withdrawal_requested.
        # withdrawal_settled/withdrawal_rejected carry only withdrawal_id in their
        # payload, so both fields must be resolved from here.
        self.withdrawals: dict[str, dict] = {}

        # trade_id -> {"customer_id", "principal"}, recorded from the fill that
        # produced it. trade_settled carries only trade_id; principal is never
        # restated in its own payload.
        self.trades: dict[str, dict] = {}

        # order_id -> {"customer_id", "original_hold", "order_quantity", "remaining_hold"}.
        # Holds are never posted as legs, so nothing in balances can reconstruct
        # them; this is the only source for a checkpoint's cash_hold.
        self.holds: dict[str, dict] = {}

        # order_id -> broker_id, decided at order_placed by the routing rule
        # (Tasksheet.md "Routing"). Fills name their own broker directly, so
        # this is read only for a checkpoint's open_order_routes -- the only
        # place the routing decision is ours to report.
        self.order_routes: dict[str, str] = {}

        # (customer_id, symbol) -> FIFO queue of lots, each {"lot_id", "quantity", "cost"}.
        # Sells consume lots in delivery order, not just by running total, and a
        # reversal must be able to find and undo one specific lot.
        self.lots: dict[tuple[str, str], deque] = defaultdict(deque)

        # The fee_charged event's own event_id -> {"amount", "refunded"}.
        # fee_refund's payload never carries the amount, only refunds_source_id
        # naming the original event, so this is the only way to resolve it; the
        # refunded flag is what makes refunding the same fee twice an error.
        self.fees: dict[str, dict] = {}

        # event_id -> the exact legs posted for that event, for every event
        # that was actually posted. A reversal must post "the exact inverse
        # of the original's legs", and reversal's own payload carries only
        # reverses_event_id -- none of the original amounts.
        self.legs_by_event: dict[str, list[dict]] = {}

        # A lot-creating event's own event_id -> the (customer_id, symbol)
        # it was added under. lot_id is already that same event_id; this is
        # the one thing reversal can't get from reverses_event_id alone --
        # which self.lots queue to search. Stale if a later symbol_change
        # re-keys that queue; not corrected here (see on_symbol_change).
        self.lot_locations: dict[str, tuple[str, str]] = {}

        # A sell fill's own event_id -> ordered [{"lot_id", "quantity",
        # "cost"}, ...] recording exactly which lot(s) it drew from FIFO
        # and how much of each. Required to undo a sell's lot-book effect
        # precisely, symmetric to lot_locations for the buy/dividend case.
        self.sell_consumption: dict[str, list[dict]] = {}

        # What you have not written yet. An unimplemented handler must not stop
        # the run: the client keeps consuming and tells you the list at the end.
        self.todo: dict[str, int] = defaultdict(int)

        # Every event ever handed to apply(), in the exact order received.
        # apply() is the only door an event enters through, so this is the
        # only place able to record delivery order; it is what makes an
        # as_of_event_id checkpoint (Tasksheet.md section 3) answerable --
        # replay a fresh Book up through that event and snapshot it there.
        self.event_log: list[dict] = []

    # -----------------------------------------------------------------------
    def apply(self, ev: dict) -> list[dict]:
        """Post one event and return its legs.

        The same event_id can arrive more than once, and the server will
        deliberately re-send several hundred events partway through the run.
        Posting twice is the single most expensive mistake available here.
        """
        self.event_log.append(ev)
        eid = ev["event_id"]
        if eid in self.seen:
            return []                      # already posted; nothing new happens
        self.seen.add(eid)

        handler = getattr(self, "on_" + ev["type"], None)
        if handler is None:
            self.todo[ev["type"]] += 1
            return []
        try:
            try:
                legs = handler(ev["payload"], ev) or []
            except InvalidOperation:
                raise Rejected("malformed numeric payload")
        except NotImplementedError:
            # Not written yet. Submit nothing for it and carry on, so one
            # missing handler costs you that event rather than the whole run.
            self.todo[ev["type"]] += 1
            return []
        except Rejected:
            # An event you refuse still gets a submission, with no legs, and it
            # must leave your book exactly as it was.
            return []
        self._post(legs)
        self.legs_by_event[eid] = legs
        return legs

    def _post(self, legs: list[dict]) -> None:
        # Explicit ZERO start: sum() over an empty legs list otherwise
        # returns int 0, and money(0) has no .quantize to call.
        dr = sum((D(l["debit"]) for l in legs), ZERO)
        cr = sum((D(l["credit"]) for l in legs), ZERO)
        if money(dr) != money(cr):
            raise AssertionError(f"unbalanced: dr {dr} cr {cr}")
        for l in legs:
            self.balances[(l["customer_id"], l["account"])] += (
                D(l["debit"]) - D(l["credit"]))

    # -- worked example -----------------------------------------------------
    def on_deposit(self, p: dict, ev: dict) -> list[dict]:
        """Cash arrives, and the firm owes the customer more.

            Dr 1100 amount        Cr 2010 amount
        """
        amount = money(D(p["amount"]))
        cid = p["customer_id"]
        return [leg("1100", cid, debit=amount),
                leg("2010", cid, credit=amount)]

    # -- yours --------------------------------------------------------------
    def on_fee_charged(self, p, ev):
        """The firm charges a fee; the customer's wallet is debited.

            Dr 2010 amount        Cr 1100 amount
        """
        amount = money(D(p["amount"]))
        cid = p["customer_id"]
        # Recorded so a later fee_refund can find the amount (not in its own
        # payload) and so a second refund of this fee can be refused.
        self.fees[ev["event_id"]] = {"amount": amount, "refunded": False}
        return [leg("2010", cid, debit=amount),
                leg("1100", cid, credit=amount)]

    def on_fee_refund(self, p, ev):
        """Refunds a fee charged earlier. The amount is not in this payload:
        look it up from the fee_charged event named by refunds_source_id.
        Refunding the same fee twice is an error.

            Dr 1100 amount        Cr 2010 amount
        """
        source = self.fees.get(p["refunds_source_id"])
        if source is None or source["refunded"]:
            raise Rejected("unknown or already-refunded fee_charged")
        source["refunded"] = True
        cid = p["customer_id"]
        return [leg("1100", cid, debit=source["amount"]),
                leg("2010", cid, credit=source["amount"])]

    def on_interest_credited(self, p, ev):
        """Interest earned on the omnibus balance, shared with the customer.
        The firm keeps the remainder -- this is not a pass-through.

            Dr 1100 gross     Cr 2010 customer_share
                                  Cr 4200 gross - customer_share
        """
        gross = money(D(p["gross_amount"]))
        customer_share = money(D(p["customer_share"]))
        cid = p["customer_id"]
        return [leg("1100", cid, debit=gross),
                leg("2010", cid, credit=customer_share),
                leg("4200", cid, credit=gross - customer_share)]

    def on_transfer_between_customers(self, p, ev):
        """No external cash moves. Both legs land on 2010, so the account
        nets to zero while each customer's own balance moves.

            Dr 2010 amount  (from_customer_id)
                                  Cr 2010 amount  (to_customer_id)
        """
        amount = money(D(p["amount"]))
        return [leg("2010", p["from_customer_id"], debit=amount),
                leg("2010", p["to_customer_id"], credit=amount)]

    def on_fx_deposit(self, p, ev):
        """Money arrives in another currency and is converted. The customer
        is credited at their rate; the gap to the market rate is the firm's
        spread. A negative spread (customer rate better than market) is bad
        data, not a gift, and is rejected.

            Dr 1100 usd_at_market_rate     Cr 2010 usd_at_customer_rate
                                           Cr 4100 the difference
        """
        usd_market = money(D(p["usd_at_market_rate"]))
        usd_customer = money(D(p["usd_at_customer_rate"]))
        spread = usd_market - usd_customer
        if spread < ZERO:
            raise Rejected("fx_deposit: customer rate better than market rate")
        cid = p["customer_id"]
        return [leg("1100", cid, debit=usd_market),
                leg("2010", cid, credit=usd_customer),
                leg("4100", cid, credit=spread)]

    def on_withdrawal_requested(self, p, ev):
        """The money has left the customer's wallet but not yet the broker.

            Dr 2010 amount        Cr 2300 amount
        """
        amount = money(D(p["amount"]))
        cid = p["customer_id"]
        # Recorded so withdrawal_settled/withdrawal_rejected -- which carry
        # only withdrawal_id -- can resolve the customer and the amount.
        self.withdrawals[p["withdrawal_id"]] = {"customer_id": cid, "amount": amount}
        return [leg("2010", cid, debit=amount),
                leg("2300", cid, credit=amount)]

    def on_withdrawal_settled(self, p, ev):
        """Cash actually leaves the broker. Look up the amount from the request.

            Dr 2300 amount        Cr 1100 amount
        """
        source = self.withdrawals.pop(p["withdrawal_id"], None)
        if source is None:
            raise Rejected("unknown withdrawal_id")
        cid, amount = source["customer_id"], source["amount"]
        return [leg("2300", cid, debit=amount),
                leg("1100", cid, credit=amount)]

    def on_withdrawal_rejected(self, p, ev):
        """The broker refuses the withdrawal; it returns to the wallet.

            Dr 2300 amount        Cr 2010 amount
        """
        source = self.withdrawals.pop(p["withdrawal_id"], None)
        if source is None:
            raise Rejected("unknown withdrawal_id")
        cid, amount = source["customer_id"], source["amount"]
        return [leg("2300", cid, debit=amount),
                leg("2010", cid, credit=amount)]

    def on_order_placed(self, p, ev):
        """No legs: a placement moves no money. It creates a hold -- for a
        buy, cash of quantity * limit_price + est_charges is no longer
        spendable; a sell holds the shares instead, which carries no cash
        figure. Holds are reported at checkpoints, never posted.

        Also decides the route: the broker with the lowest total customer
        charge (brokerage + custody) on quantity * limit_price, among
        brokers trading this asset_class, ties broken by broker id
        ascending. Fills name their own broker; this decision is only ever
        read back for a checkpoint's open_order_routes.
        """
        quantity = D(p["quantity"])
        notional = quantity * D(p["limit_price"])
        hold = (money(notional + D(p.get("est_charges", 0)))
                if p["side"] == "buy" else ZERO)
        self.holds[p["order_id"]] = {
            "customer_id": p["customer_id"],
            "original_hold": hold,
            "order_quantity": quantity,
            "remaining_hold": hold,
        }

        asset_class = p["asset_class"]
        best_broker, best_charge = None, None
        for broker_id in sorted(BROKER_TARIFF):
            tariff = BROKER_TARIFF[broker_id]
            if asset_class not in tariff["asset_classes"]:
                continue
            brokerage = max(money(notional * tariff["brokerage_bps"] / D("10000")),
                             tariff["min_fee"])
            custody = money(notional * tariff["custody_bps"] / D("10000"))
            charge = brokerage + custody
            if best_charge is None or charge < best_charge:
                best_broker, best_charge = broker_id, charge
        self.order_routes[p["order_id"]] = best_broker

        return []

    def on_order_partially_filled(self, p, ev):
        return self.on_order_filled(p, ev)

    def on_order_filled(self, p, ev):
        """buy: Dr 2010 P+b+c+r, Dr 1200 P, Dr 5000 bc, Dr 5010 cc,
        Dr 5100 ps / Cr 2350 P, Cr 2100 P, Cr 4000 b, Cr 4010 c, Cr 2400 r,
        Cr 241x bc, Cr 2420 cc, Cr 2430 ps. sell: Dr 1150 P, Dr 2100 FIFO
        cost, Dr 5000 bc, Dr 5010 cc, Dr 5100 ps / Cr 2010 P-b-c-r,
        Cr 1200 cost, Cr 4000 b, Cr 4010 c, Cr 2400 r, Cr 241x bc,
        Cr 2420 cc, Cr 2430 ps -- revenue, cost, regulatory and partner
        economics are identical on both sides (Tasksheet.md section 4);
        only what the customer's wallet is charged against differs. Cash
        does NOT move on the trade date either way -- trade_settled
        discharges the P-only obligation two days later.
        """
        cid = p["customer_id"]
        quantity = D(p["quantity"])
        principal = money(D(p["principal"]))

        # The fee amounts are never in the payload -- broker and principal
        # are, and the tariff (Tasksheet.md "The tariff") turns those into
        # money. Each derived amount is rounded to the cent independently
        # before use; the ticket is already cent-exact and adds on top of
        # the bps-derived broker cost, not rounded again with it.
        tariff = BROKER_TARIFF[p["broker"]]
        brokerage = max(money(principal * tariff["brokerage_bps"] / D("10000")),
                         tariff["min_fee"])
        custody = money(principal * tariff["custody_bps"] / D("10000"))
        reg = money(principal * REG_FEE_BPS / D("10000"))
        broker_cost = (money(principal * tariff["broker_cost_bps"] / D("10000"))
                        + tariff["ticket"])
        custody_cost = money(principal * tariff["custody_cost_bps"] / D("10000"))
        # partner_rate x (revenue - cost); floored at zero, never clawed back
        # -- a ticket fee makes roughly a quarter of fills loss-making.
        partner_share = max(
            money(D(p["partner_rate"])
                  * (brokerage + custody - broker_cost - custody_cost)),
            ZERO)
        payable_account = tariff["payable_account"]

        if p["side"] == "buy":
            # One lot per fill, cost is principal only -- none of the
            # tariff charges are part of the cost basis (Tasksheet.md
            # section 4). Identity is this event's own event_id: already
            # unique, and a future reversal will need to name this exact lot.
            self.lots[(cid, p["symbol"])].append(
                {"lot_id": ev["event_id"], "quantity": quantity, "cost": principal})
            self.lot_locations[ev["event_id"]] = (cid, p["symbol"])

            legs = [
                leg("2010", cid, debit=principal + brokerage + custody + reg),
                leg("2350", cid, credit=principal),
                leg("1200", cid, debit=principal),
                leg("2100", cid, credit=principal),
                leg("5000", cid, debit=broker_cost),
                leg("4000", cid, credit=brokerage),
                leg("5010", cid, debit=custody_cost),
                leg("4010", cid, credit=custody),
                leg("5100", cid, debit=partner_share),
                leg("2400", cid, credit=reg),
                leg(payable_account, cid, credit=broker_cost),
                leg("2420", cid, credit=custody_cost),
                leg("2430", cid, credit=partner_share),
            ]

        elif p["side"] == "sell":
            queue = self.lots[(cid, p["symbol"])]
            # Every lot left in the queue has quantity > 0 (exhausted lots
            # are removed below), so this sum is exactly what's sellable.
            available = sum(lot["quantity"] for lot in queue)
            if quantity > available:
                raise Rejected("oversell: requested quantity exceeds position")

            remaining = quantity
            cost = ZERO
            # Records exactly which lot(s) this drew from and how much, so
            # a future reversal can undo this sell's lot-book effect
            # precisely rather than approximating it. on_reversal itself is
            # still not implemented -- this only makes that possible later.
            manifest = []
            while remaining > 0:
                lot = queue[0]
                take_qty = min(lot["quantity"], remaining)
                take_cost = money(lot["cost"] * take_qty / lot["quantity"])
                manifest.append(
                    {"lot_id": lot["lot_id"], "quantity": take_qty, "cost": take_cost})
                cost += take_cost
                lot["quantity"] -= take_qty
                lot["cost"] -= take_cost
                remaining -= take_qty
                if lot["quantity"] == 0:
                    queue.popleft()
            self.sell_consumption[ev["event_id"]] = manifest

            legs = [
                leg("1150", cid, debit=principal),
                leg("2010", cid, credit=principal - brokerage - custody - reg),
                leg("2100", cid, debit=cost),
                leg("1200", cid, credit=cost),
                leg("5000", cid, debit=broker_cost),
                leg("4000", cid, credit=brokerage),
                leg("5010", cid, debit=custody_cost),
                leg("4010", cid, credit=custody),
                leg("5100", cid, debit=partner_share),
                leg("2400", cid, credit=reg),
                leg(payable_account, cid, credit=broker_cost),
                leg("2420", cid, credit=custody_cost),
                leg("2430", cid, credit=partner_share),
            ]

        else:
            raise NotImplementedError(f"unknown order side {p['side']!r}")

        # trade_settled carries only trade_id; principal and side are never
        # restated there, so both must be resolvable from the fill that
        # produced it -- true for both sides.
        self.trades[p["trade_id"]] = {
            "customer_id": cid, "principal": principal, "side": p["side"]}

        hold = self.holds.get(p["order_id"])
        if hold is not None:
            if ev["type"] == "order_filled":
                # The final fill returns whatever remains to exactly zero,
                # instead of computing one more share that could drift.
                del self.holds[p["order_id"]]
            else:
                share = money(hold["original_hold"] * quantity / hold["order_quantity"])
                hold["remaining_hold"] -= share

        return legs

    def on_trade_settled(self, p, ev):
        """Discharges the obligation from the fill named by trade_id.

        buy: Dr 2350 / Cr 1100.  sell: Dr 1100 / Cr 1150.
        """
        trade = self.trades.pop(p["trade_id"], None)
        if trade is None:
            raise Rejected("unknown trade_id")
        cid, principal = trade["customer_id"], trade["principal"]
        if trade["side"] == "buy":
            return [leg("2350", cid, debit=principal),
                    leg("1100", cid, credit=principal)]
        return [leg("1100", cid, debit=principal),
                leg("1150", cid, credit=principal)]

    def on_order_cancelled(self, p, ev):
        """No legs. Releases whatever hold remains for this order to exactly
        zero -- a fill would already have released a proportional share, so
        this only ever returns what fills haven't already taken.
        """
        if self.holds.pop(p["order_id"], None) is None:
            raise Rejected("unknown order_id")
        return []

    def on_order_rejected(self, p, ev):
        return self.on_order_cancelled(p, ev)

    def on_broker_fees_settled(self, p, ev):
        """Discharges that broker's accumulated fees for this customer, paid
        out of omnibus cash. The amount is never in the payload -- it is
        whatever self.balances already holds on that broker's 241x account
        (Tasksheet.md "Paying it all onward"). Settling nothing outstanding
        is an error.

            Dr 241x outstanding        Cr 1100 outstanding
        """
        cid = p["customer_id"]
        tariff = BROKER_TARIFF.get(p["broker"])
        if tariff is None:
            raise Rejected("unknown broker")
        account = tariff["payable_account"]
        outstanding = -self.balances.get((cid, account), ZERO)
        if outstanding == ZERO:
            raise Rejected("nothing outstanding on that account")
        return [leg(account, cid, debit=outstanding),
                leg("1100", cid, credit=outstanding)]

    def on_custodian_fees_settled(self, p, ev):
        """Discharges the custodian's accumulated fees for this customer.

            Dr 2420 outstanding        Cr 1100 outstanding
        """
        cid = p["customer_id"]
        outstanding = -self.balances.get((cid, ACCT_CUSTODIAN_FEES_PAYABLE), ZERO)
        if outstanding == ZERO:
            raise Rejected("nothing outstanding on that account")
        return [leg(ACCT_CUSTODIAN_FEES_PAYABLE, cid, debit=outstanding),
                leg("1100", cid, credit=outstanding)]

    def on_reg_fees_remitted(self, p, ev):
        """Remits the regulatory fees collected for this customer, owed
        onward to the venue.

            Dr 2400 outstanding        Cr 1100 outstanding
        """
        cid = p["customer_id"]
        outstanding = -self.balances.get((cid, "2400"), ZERO)
        if outstanding == ZERO:
            raise Rejected("nothing outstanding on that account")
        return [leg("2400", cid, debit=outstanding),
                leg("1100", cid, credit=outstanding)]

    def on_partner_payout(self, p, ev):
        """Pays out the introducing partner's accumulated share for this
        customer.

            Dr 2430 outstanding        Cr 1100 outstanding
        """
        cid = p["customer_id"]
        outstanding = -self.balances.get((cid, ACCT_PARTNER_SHARE_PAYABLE), ZERO)
        if outstanding == ZERO:
            raise Rejected("nothing outstanding on that account")
        return [leg(ACCT_PARTNER_SHARE_PAYABLE, cid, debit=outstanding),
                leg("1100", cid, credit=outstanding)]

    def on_dividend_cash(self, p, ev):
        """Tax is withheld at source; only the net ever reaches us and we
        owe the tax to nobody, so no payable is raised.

            Dr 1100 net           Cr 2010 net
        """
        net = money(D(p["net_amount"]))
        cid = p["customer_id"]
        return [leg("1100", cid, debit=net),
                leg("2010", cid, credit=net)]

    def on_dividend_reinvested(self, p, ev):
        """The broker reinvests the net dividend. Cash is not involved at all.

            Dr 1200 net           Cr 2100 net
        and add a lot of reinvest_quantity at cost net.
        """
        net = money(D(p["net_amount"]))
        cid = p["customer_id"]
        # Identity is this event's own event_id, same as a buy fill's lot --
        # already unique, and a future reversal will need to name this lot.
        self.lots[(cid, p["symbol"])].append(
            {"lot_id": ev["event_id"], "quantity": D(p["reinvest_quantity"]),
             "cost": net})
        self.lot_locations[ev["event_id"]] = (cid, p["symbol"])
        return [leg("1200", cid, debit=net),
                leg("2100", cid, credit=net)]

    def on_stock_split(self, p, ev):
        """No legs. Quantity scales by ratio_to/ratio_from; total cost is
        unchanged, so cost per share moves.
        """
        ratio_from = D(p["ratio_from"])
        ratio_to = D(p["ratio_to"])
        if ratio_from <= 0 or ratio_to <= 0:
            raise Rejected("invalid stock split ratio")
        ratio = ratio_to / ratio_from
        for lot in self.lots[(p["customer_id"], p["symbol"])]:
            lot["quantity"] *= ratio
        return []

    def on_symbol_change(self, p, ev):
        """No legs. Re-key the holding -- moves the existing FIFO queue as
        one object, so identity, quantity, cost, and order are untouched.
        """
        cid = p["customer_id"]
        queue = self.lots.pop((cid, p["old_symbol"]), None)
        if queue is not None:
            self.lots[(cid, p["new_symbol"])] = queue
            # lot_locations was recorded under old_symbol at creation time;
            # without this, a later reversal would look the lot up under a
            # key self.lots no longer has, and wrongly reject it as gone.
            for lot in queue:
                self.lot_locations[lot["lot_id"]] = (cid, p["new_symbol"])
        return []

    def on_reversal(self, p, ev):
        """Post the exact inverse of the original's legs, using only the
        archived journal (never recomputed from a payload), and undo the
        exact lot-book effect the original event had, using only the
        existing location registry and sell-consumption manifest. Holds
        and the trade registry are never touched: a reversal undoes
        postings and the lot book, not the lifecycle.
        """
        source_id = p["reverses_event_id"]
        original_legs = self.legs_by_event.get(source_id)
        if original_legs is None:
            raise Rejected("unknown reverses_event_id")

        if source_id in self.lot_locations:
            cid, symbol = self.lot_locations[source_id]
            queue = self.lots.get((cid, symbol), ())
            target = next((lot for lot in queue if lot["lot_id"] == source_id), None)
            if target is None:
                raise Rejected("reversed lot no longer exists")
            queue.remove(target)

        elif source_id in self.sell_consumption:
            manifest = self.sell_consumption[source_id]
            # Validate every consumed lot can be located before mutating
            # any of them -- a partial undo is not acceptable.
            plan = []
            for entry in manifest:
                location = self.lot_locations.get(entry["lot_id"])
                if location is None:
                    raise Rejected("consumed lot's location is unknown")
                queue = self.lots.get(location, ())
                lot = next((l for l in queue if l["lot_id"] == entry["lot_id"]), None)
                plan.append((lot, entry, location))

            # A lot fully drained by this sell was popped from the queue at
            # the time -- the FIFO loop only moves to the next lot once the
            # current one hits zero, so every entry but possibly the last
            # was popped this way and must be recreated. The queue only
            # ever appends at the back and consumes from the front, so
            # nothing currently present can be older than any lot this
            # sell drained -- recreated lots belong at the very front, in
            # the same relative order the manifest recorded them in.
            to_recreate = [(loc, e) for lot, e, loc in plan if lot is None]
            for lot, entry, _loc in plan:
                if lot is not None:
                    lot["quantity"] += entry["quantity"]
                    lot["cost"] += entry["cost"]
            for location, entry in reversed(to_recreate):
                self.lots[location].appendleft(
                    {"lot_id": entry["lot_id"], "quantity": entry["quantity"],
                     "cost": entry["cost"]})

            # Consumed, same as a buy/dividend lot is removed on reversal --
            # otherwise a second reversal of this same sell would restore
            # the same quantity/cost again and silently corrupt the lots.
            del self.sell_consumption[source_id]

        return [leg(l["account"], l["customer_id"], debit=l["credit"], credit=l["debit"])
                for l in original_legs]

    # -- reporting ----------------------------------------------------------
    def snapshot(self, as_of_event_id: str | None = None) -> dict:
        """What a checkpoint_request wants: your whole state, right now --
        or, if as_of_event_id is given, your state as it stood once that
        event (and nothing delivered after it) had been processed.

        Current state alone can't answer an as-of query, and nothing here
        keeps a snapshot per event, so the event log is replayed through a
        fresh Book up to and including the first delivery of that event_id;
        a later duplicate delivery of the same id is correctly not needed,
        since replaying through the first one already reflects its effect.
        That fresh Book is then asked for its own (unqualified) snapshot.

        Report every account you have ever posted to, including any that have
        netted back to zero. Trial balance values are debit-positive, so
        liabilities carry a negative sign.
        """
        if as_of_event_id is not None:
            cutoff = next((i for i, e in enumerate(self.event_log)
                           if e["event_id"] == as_of_event_id),
                          len(self.event_log) - 1)
            replay = Book()
            for e in self.event_log[:cutoff + 1]:
                replay.apply(e)
            return replay.snapshot()

        tb: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for (_cid, acct), bal in self.balances.items():
            tb[acct] += bal

        customers: dict[str, dict] = {}
        for (cid, acct), bal in self.balances.items():
            c = customers.setdefault(cid, {"wallet_cash": ZERO,
                                           "cash_hold": ZERO, "positions": {}})
            if acct == "2010":
                c["wallet_cash"] += -bal          # a liability, so credit-positive

        for hold in self.holds.values():
            c = customers.setdefault(hold["customer_id"], {"wallet_cash": ZERO,
                                           "cash_hold": ZERO, "positions": {}})
            c["cash_hold"] += hold["remaining_hold"]

        for (cid, symbol), lots in self.lots.items():
            total_quantity = sum((lot["quantity"] for lot in lots), D("0"))
            total_cost = sum((lot["cost"] for lot in lots), D("0"))
            if total_quantity > 0:
                c = customers.setdefault(cid, {"wallet_cash": ZERO,
                                               "cash_hold": ZERO, "positions": {}})
                c["positions"][symbol] = {
                    "quantity": str(total_quantity),
                    "cost_basis": str(money(total_cost)),
                }

        return {
            "trial_balance": {a: str(money(v)) for a, v in sorted(tb.items())},
            "customers": {cid: {"wallet_cash": str(money(c["wallet_cash"])),
                                "cash_hold": str(money(c["cash_hold"])),
                                "positions": c["positions"]}
                          for cid, c in sorted(customers.items())},
            # self.holds holds exactly the still-open orders -- a fill that
            # closes an order or a cancellation/rejection removes it there,
            # matching "orders you have seen filled or cancelled do not
            # belong here" (Tasksheet.md section 3).
            "open_order_routes": {oid: self.order_routes[oid]
                                   for oid in sorted(self.holds)
                                   if oid in self.order_routes},
        }


class Rejected(Exception):
    """Raise from a handler for an event you refuse to post.

    An oversell, a reversal of something you never received, a payload that
    will not parse. Rejecting one event and carrying on beats stopping: a
    server that stalls misses everything after it.
    """
