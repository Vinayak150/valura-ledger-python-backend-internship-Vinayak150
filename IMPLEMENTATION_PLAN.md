# Ledger Engine Implementation

## Overview

`book.py` implements an event-driven double-entry ledger. Events are applied
one at a time through a single dispatch method, each event type is posted as
a set of debit/credit journal legs, and every leg is expressed in
`decimal.Decimal` rather than binary floating point. Balances are tracked per
`(customer_id, account)` pair rather than per account, and event processing
is replay-safe: an event whose `event_id` has already been seen is not
posted a second time.

## Architecture

The `Book` class owns all ledger state and exposes two operations to its
caller: `apply(ev)`, which posts one event and returns the legs it produced,
and `snapshot()`, which reports the current state for a checkpoint.

`apply()` is the single dispatch point. It looks up a handler by event type
(`getattr(self, "on_" + ev["type"])`), calls it with the event's payload, and
is responsible for the concerns that do not belong in any individual
handler: deduplication via `seen`, tracking unimplemented event types in
`todo`, and converting exceptions raised by a handler (`NotImplementedError`,
`Rejected`, `decimal.InvalidOperation`) into an empty list of legs so
processing continues.

Journal posting is centralized in `_post()`. Every handler returns a list of
legs built by the `leg()` helper; `_post()` sums the debit and credit sides
of that list, and if they do not match (after rounding via `money()`) raises
`AssertionError` rather than applying any part of the posting. Only after
this check passes are `balances` updated, one leg at a time.

Checkpoint generation is handled by `snapshot()`, which derives a trial
balance and per-customer view (`wallet_cash`, `cash_hold`, `positions`)
entirely from existing state (`balances`, `holds`, `lots`) rather than from
any separately maintained reporting structure.

## Implemented Features

### Cash Operations

- `deposit` — debits omnibus cash, credits the customer's wallet.
- `fx_deposit` — converts an incoming foreign-currency deposit at the
  customer's rate, crediting the wallet at that rate and the difference to
  the market rate to firm income; a customer rate better than market is
  rejected.
- `interest_credited` — splits interest earned on the omnibus balance
  between the customer's wallet and firm income.
- `transfer_between_customers` — moves a balance between two customers'
  wallets on account `2010`, with no external cash movement.

### Fee Processing

- `fee_charged` — debits the customer's wallet and credits omnibus cash for
  a firm-charged fee; the amount is recorded so it can later be refunded.
- `fee_refund` — reverses a previously charged fee by looking up its amount
  from the recorded `fee_charged` event; refunding the same fee twice is
  rejected.

### Withdrawals

- `withdrawal_requested` — debits the customer's wallet and credits
  withdrawals-in-transit; the amount and customer are recorded for the
  events that follow.
- `withdrawal_settled` — discharges the in-transit liability against
  omnibus cash, using the amount recorded at request time.
- `withdrawal_rejected` — returns the in-transit amount to the customer's
  wallet, using the same recorded amount.

### Trading

- `order_placed` — posts no legs; records a cash hold for a buy order
  (`quantity * limit_price + est_commission`) or a zero cash hold for a
  sell order, since a sell holds shares rather than cash.
- `order_partially_filled` / `order_filled` — post the trade-date legs for a
  buy or sell fill. A buy adds a FIFO lot at cost equal to principal and
  posts the unsettled payable; a sell consumes FIFO lots to determine cost,
  posts a settlement receivable, and posts the regulatory fee owed to the
  venue. Both sides release a proportional (partial fill) or full (closing
  fill) share of the order's cash hold.
- `trade_settled` — discharges the settlement obligation recorded by the
  fill that produced the given `trade_id`, with different legs for buy and
  sell.
- `order_cancelled` / `order_rejected` — post no legs; release whatever
  remains of the order's cash hold.

### Corporate Actions

- `dividend_cash` — credits the customer's wallet with the net dividend
  amount; no tax payable is raised since only the net is ever received.
- `dividend_reinvested` — moves custody and the securities claim by the net
  amount and adds a new FIFO lot at that cost; no cash is involved.
- `stock_split` — posts no legs; rescales the quantity of every existing lot
  for the symbol by the split ratio, leaving total cost unchanged.
- `symbol_change` — posts no legs; re-keys a customer's FIFO lot queue from
  the old symbol to the new one as a single object, preserving lot identity,
  quantity, cost, and order.

### Recovery

- `reversal` — posts the exact inverse of a previously posted event's legs,
  read from the archived journal rather than recomputed, and undoes the
  matching lot-book effect (removing a created lot, or restoring lots
  consumed by a reversed sell).

## Internal State

- `balances` — dict keyed by `(customer_id, account)`, value is a
  debit-positive `Decimal`. Holds the running balance for every account a
  customer has ever posted to; keyed per customer, not per account, because
  some events move money between two customers on the same account.
- `seen` — set of `event_id`s. Records every event that has been posted, so
  a duplicate delivery of the same event (redelivery, stream reset) is
  recognized and skipped instead of posted again.
- `withdrawals` — dict keyed by `withdrawal_id`, value is
  `{"customer_id", "amount"}`. Records what a `withdrawal_requested` event
  established, since `withdrawal_settled` and `withdrawal_rejected` carry
  only the `withdrawal_id` and must resolve both fields from here.
- `trades` — dict keyed by `trade_id`, value is
  `{"customer_id", "principal", "side"}`. Records what the fill that
  produced a trade established, since `trade_settled` carries only the
  `trade_id`.
- `holds` — dict keyed by `order_id`, value is
  `{"customer_id", "original_hold", "order_quantity", "remaining_hold"}`.
  Holds are never posted as journal legs, so this is the only record of an
  order's outstanding cash hold, used both to release it on fills and
  cancellations and to report it at checkpoints.
- `lots` — dict keyed by `(customer_id, symbol)`, value is a `deque` of
  `{"lot_id", "quantity", "cost"}` in FIFO order. Represents each customer's
  open position per symbol, consumed from the front by sells and appended to
  at the back by buys and reinvested dividends.
- `fees` — dict keyed by the `fee_charged` event's own `event_id`, value is
  `{"amount", "refunded"}`. Records a charged fee's amount, since
  `fee_refund` carries only a reference to the original event, and whether
  it has already been refunded, to reject a duplicate refund.
- `legs_by_event` — dict keyed by `event_id`, value is the list of legs
  actually posted for that event. Recorded for every posted event so that a
  later `reversal`, whose payload carries only `reverses_event_id`, can
  replay the exact original legs rather than recomputing them.
- `lot_locations` — dict keyed by a lot-creating event's own `event_id`,
  value is `(customer_id, symbol)`. Identifies which `lots` queue a
  reversible lot lives in, since a `symbol_change` can move it after
  creation and `reverses_event_id` alone does not carry that information.
- `sell_consumption` — dict keyed by a sell fill's `event_id`, value is the
  ordered list of `{"lot_id", "quantity", "cost"}` it drew from FIFO.
  Records exactly which lots a sell consumed so a reversal can restore that
  consumption precisely rather than approximating it.

## Accounting Model

Every posting is expressed as a list of legs, each carrying an account, a
customer, and a debit or credit amount. `_post()` sums the debit and credit
sides of a posting and requires them to be equal, after rounding, before any
balance is updated; an imbalanced posting raises `AssertionError` and is
never partially applied.

Customer-facing liabilities (wallet cash on `2010`, securities claim on
`2100`, withdrawals in transit on `2300`, unsettled trade payable on `2350`)
and firm-side assets (omnibus cash on `1100`, settlement receivable on
`1150`, omnibus custody on `1200`) are both tracked through the same
`balances` structure, keyed per customer where the account is
customer-specific.

Inventory is costed on a FIFO basis: `lots` holds each customer's open
position per symbol as an ordered queue, and a sell walks that queue from
the front, consuming whole or partial lots until the sold quantity is
covered, to compute the cost of goods sold for that fill.

Trade settlement is modeled as a two-step lifecycle: a fill posts the
trade-date legs and records the customer, principal, and side under the
fill's `trade_id`; the corresponding `trade_settled` event later discharges
that obligation by posting the settlement legs and removing the record.

Cost basis is maintained entirely through the `lots` structure: buys and
reinvested dividends append lots at their cost, sells reduce or remove lots
as they are consumed, stock splits rescale quantity without changing total
cost, and symbol changes re-key the queue without altering any lot's
quantity or cost.

Reversal does not recompute amounts. It looks up the original event's
posted legs in `legs_by_event` and posts them back with debit and credit
swapped, and separately reverses the lot-book side effect using
`lot_locations` (to remove a created lot) or `sell_consumption` (to restore
consumed lots), rejecting the reversal if the referenced event, lot, or
consumption record cannot be found.

Checkpoint snapshots are computed, not stored: `snapshot()` builds a trial
balance by summing `balances` across customers per account, derives each
customer's wallet cash from their `2010` balance, their cash hold from
`holds`, and their open positions from `lots`.

## Error Handling

- **Duplicate events** are ignored: `apply()` checks `event_id` against
  `seen` before dispatching, and returns an empty list of legs for anything
  already posted.
- **Rejected business events** (an oversell, a reference to an unknown
  withdrawal, trade, order, or reversed event, an invalid stock split ratio,
  a negative fx spread, a duplicate fee refund) raise `Rejected` from the
  handler; `apply()` catches it and returns an empty list of legs, leaving
  the book unchanged.
- **Malformed numeric payloads** are converted into rejections: `apply()`
  catches `decimal.InvalidOperation` raised while constructing a `Decimal`
  from a payload field and raises `Rejected` in its place, so a value that
  will not parse is handled the same way as any other rejected event
  instead of stopping processing.
- **Unimplemented event types** are tracked, not fatal: if no `on_<type>`
  handler exists, or a handler raises `NotImplementedError`, the event type
  is counted in `todo` and processing continues.
- **Oversells** are explicitly checked before any lot is consumed: if the
  requested sell quantity exceeds the sum of available lot quantities, the
  fill is rejected before the FIFO consumption loop runs.
- **Unknown references** (a `withdrawal_id`, `trade_id`, `order_id`, or
  `reverses_event_id` not present in the corresponding record) are rejected
  rather than treated as a no-op or a crash.

## Design Decisions

- **`Decimal` instead of `float`** for every monetary value, with a single
  `money()` helper that quantizes to two decimal places using
  `ROUND_HALF_UP`, applied consistently rather than left to each call site.
- **`defaultdict` usage** for `balances` (defaulting to zero) and `lots`
  (defaulting to an empty deque), so reading a balance or a position that
  has never been touched behaves the same as reading one that nets to zero.
- **`deque` for FIFO lots**, giving O(1) removal from the front (normal sell
  consumption) and O(1) insertion at the front (restoring lots on reversal
  of a partially consumed sell).
- **Centralized dispatch** in `apply()`, so deduplication, unimplemented-type
  tracking, and exception-to-rejection conversion are handled once rather
  than repeated in every handler.
- **Centralized posting** in `_post()`, so the balance requirement is
  enforced in exactly one place regardless of which handler produced the
  legs.
- **Archived journal legs for reversal**: `legs_by_event` stores the exact
  legs posted for every event, since a reversal's payload carries only
  `reverses_event_id` and no amounts, making the archive the only accurate
  source for what to invert.
- **Proportional hold release**: a partial fill releases
  `original_hold * quantity / order_quantity` of the order's hold rather
  than an independently tracked remainder, and the closing fill,
  cancellation, or rejection clears whatever remains to exactly zero instead
  of relying on that computation to land there.
- **Replay-safe event processing** via the `seen` set, checked before any
  handler runs, so the stream's deliberate redelivery of already-processed
  events does not double-post them.

## Complexity

Posting a single event is proportional to the number of legs it produces,
which is a small constant per event type (at most six, for a sell fill).

A FIFO sell fill is proportional to the number of lots it consumes, since
the consumption loop advances one lot at a time from the front of the deque
until the requested quantity is covered.

Snapshot generation is proportional to the total number of
`(customer_id, account)` balance entries, open holds, and open lot queues,
since it iterates each of those structures once to build the trial balance
and per-customer view.

## Files Modified

**book.py**
- Complete ledger implementation.

**README.md**
- Added implementation documentation.

**IMPLEMENTATION_PLAN.md**
- Converted from planning document into implementation documentation.

**client.py**
- Left functionally unchanged.

## Summary

The ledger engine implements all twenty protocol event types across cash
operations, fee processing, withdrawals, trading, corporate actions, and
reversal, through a single dispatch method that enforces deduplication,
double-entry balance validation, and rejection of malformed or invalid
events without stopping stream processing. State is held in a small set of
purpose-built structures, each recording exactly what a later event needs
and cannot otherwise recover from its own payload, and checkpoint reporting
is derived from that same state rather than tracked separately.
