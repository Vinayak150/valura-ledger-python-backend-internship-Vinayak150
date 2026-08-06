# Ledger Arena: starter kit

You are building a double-entry book of record. We stream you a broker's event
feed; you post the journal legs each event produces and answer state
checkpoints. We score it continuously against a reference implementation.

Every awkward case in here is one that has cost a real back office real money,
and most of them still balanced perfectly while being wrong.

## Start here

```bash
git clone <your copy of this repo>
cd ledger-arena-starter
pip install -r requirements.txt

python client.py --key ak_your_key_here
```

Get your key from **https://hiring-arena.twocc.in** by entering the email your
invitation was sent to.

That first run will connect, stream, and score somewhere in the low tens. It is
meant to: `book.py` implements one event type as a worked example and raises on
the rest. Seeing the whole loop work before you have written a line means any
later failure is yours and not ours.

It does not score zero because roughly one event in seven correctly produces no
legs at all, and submitting nothing for those is the right answer. Treat the
number you get on that first run as the floor, not as progress.

It ends by printing what it could not post, which is your to-do list:

```
not implemented yet (1174 events skipped):
  order_filled                     287 events
  trade_settled                    281 events
  ...
```

Then read **`PROTOCOL.md`**. It is the entire specification: the accounts, all
twenty event types, every posting rule, and how the scoring works.

## What is already done for you

`client.py` is finished. It subscribes, survives the deliberate mid-run replay,
resumes from an offset, batches postings, and answers checkpoints on time. That
is transport, and it is not what we are assessing.

`book.py` is where you work. It hands you one event and takes back its legs.

## Two things to get right before anything else

**Use `Decimal`, never `float`.** Money here does not always divide evenly. A
float implementation will disagree with us by a cent in places that are very
hard to find afterwards.

**Key balances by `(customer, account)`, not by account.** At least one event
moves money between two customers on the same account. An account-level book
shows nothing wrong at all, and its trial balance agrees with it.

## Tiers

| Tier | Attempts | Score | What it is for |
| --- | --- | --- | --- |
| `practice` | unlimited | shown, with the correct legs on every event | develop here |
| `submission` | 3 | shown | scored; tuning against it is expected |
| `final` | 1 | withheld | this is what ranks you |

Practice returns the expected legs on every response. Use it hard: it is the
executable version of the specification, and anything ambiguous in the document
is settled by running against it.

Each attempt draws a fresh dataset, so a retry is a new problem rather than a
retake of one you have already seen scored.

## Rules

- **One address, one candidate.** Your key is your identity.
- **Use AI tools if you normally do.** We do. There is no penalty and no
  detection game. But you will walk us through the code in a live session and
  change it while we watch, so be able to defend every line of it.
- **Ask in Discord, not by DM.** Anything clarified there becomes canon for
  everyone, which is fairer than rewarding whoever thought to ask privately.
- **If you run out of time, stop and write down what is missing** and how you
  would have done it. That costs you nothing and reads far better than
  something half-built and unexplained.

## Things the stream will do to you

All deliberate, all in `PROTOCOL.md`, none of them bugs: duplicate delivery, a
forced disconnect that rewinds you several hundred events, fills that arrive
before their placement, oversells, reversals of events you never received, and
payloads that will not parse.

A server that rejects one bad event and keeps consuming beats one that stops.

## Running a graded tier

```bash
python client.py --key ak_... --mode submission
```

It will ask you to confirm, because attempts are limited. A run that cannot
finish before the deadline is refused rather than started, so you will not lose
an attempt to the clock.

# My Implementation

## Architecture

The ledger is event-driven: `Book.apply(ev)` is the single entry point, and
every event type is handled by a dedicated `on_<event_type>` method dispatched
by name (`getattr(self, "on_" + ev["type"])`).

All postings are double-entry. The `leg()` helper always produces a paired
debit/credit entry, and `_post()` sums every leg's debit and credit sides and
raises `AssertionError` if they do not match before any balance is updated.

Balances are kept per customer, not per account: `self.balances` is keyed by
`(customer_id, account)`, so two customers holding the same account (e.g.
`2010`) never share a bucket.

All monetary arithmetic uses `decimal.Decimal` (aliased as `D`), never
`float`. `money()` quantizes every amount to two decimal places with
`ROUND_HALF_UP`.

Processing is replay-safe: `apply()` checks `ev["event_id"]` against
`self.seen` before doing anything else, so an event delivered more than once
(duplicate delivery, stream resets) is posted at most once.

## High-Level Flow

```text
Event Stream
      │
      ▼
┌──────────────┐
│  client.py   │
│ (provided)   │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│  Book.apply  │
└──────┬───────┘
       │
       ├───────────────┐
       ▼               ▼
 Event Handlers     Rejected
       │
       ▼
 Double-entry Legs
       │
       ▼
    _post()
       │
       ├─────────────┐
       ▼             ▼
 balances        Internal State
                 (lots, holds,
                  trades, fees,
                  withdrawals...)
       │
       ▼
  snapshot()
```

## Implemented Event Types

### Cash Operations
- `deposit`
- `fx_deposit`
- `interest_credited`
- `transfer_between_customers`

### Fees
- `fee_charged`
- `fee_refund`

### Withdrawals
- `withdrawal_requested`
- `withdrawal_settled`
- `withdrawal_rejected`

### Trading
- `order_placed`
- `order_partially_filled`
- `order_filled`
- `trade_settled`
- `order_cancelled`
- `order_rejected`

### Corporate Actions
- `dividend_cash`
- `dividend_reinvested`
- `stock_split`
- `symbol_change`

### Recovery
- `reversal`

## State Maintained

- `balances` — debit-positive running balance for every `(customer_id, account)` pair posted to.
- `seen` — the set of `event_id`s already posted, so a redelivered event is not posted twice.
- `withdrawals` — `withdrawal_id` to the customer and amount recorded at `withdrawal_requested`, since `withdrawal_settled`/`withdrawal_rejected` carry only the id.
- `trades` — `trade_id` to the customer, principal, and side recorded at the fill that produced it, since `trade_settled` carries only the id.
- `holds` — `order_id` to the original and remaining cash hold for that order, since holds are never posted as legs and would otherwise be unrecoverable.
- `lots` — `(customer_id, symbol)` to a FIFO `deque` of lots, each with a quantity and cost, used to price sells and compute cost basis.
- `fees` — the `fee_charged` event's own id to its amount and whether it has been refunded, since `fee_refund` carries only a reference to the original event.
- `legs_by_event` — every posted event's `event_id` to the exact legs it produced, so a later reversal can replay them without recomputation.
- `lot_locations` — a lot-creating event's `event_id` to the `(customer_id, symbol)` queue it lives in, so a reversal can find it after a possible `symbol_change`.
- `sell_consumption` — a sell fill's `event_id` to the ordered list of lots (and quantities/costs) it drew from FIFO, so a reversal can undo exactly that consumption.

## Accounting Rules

Every handler returns a list of legs; `_post()` enforces double-entry by
summing debits and credits and rejecting any imbalance before touching
`balances`.

Sells are priced against inventory using FIFO: `on_order_filled` walks the
customer's lot queue for that symbol from the front, consuming whole or
partial lots until the sold quantity is covered, and records what it
consumed in `sell_consumption`.

Customer cash is tracked as a liability on account `2010`, credit-positive,
per customer.

Order placement creates a hold in `holds` rather than a posting: buy orders
hold `quantity * limit_price + est_commission`, sell orders hold no cash.
Each partial fill releases a proportional share of the original hold
(`original_hold * quantity / order_quantity`); the closing `order_filled`,
`order_cancelled`, or `order_rejected` releases whatever remains outright.

Trade settlement is modeled as a lifecycle: a fill records the trade's
customer, principal, and side in `trades`; the matching `trade_settled`
event pops that record and posts the settlement legs, which differ for buy
and sell.

Reversal is driven entirely by `legs_by_event`: `on_reversal` looks up the
original event's posted legs by `reverses_event_id` and posts them back with
debit and credit swapped, never recomputing amounts from its own payload. It
also unwinds the corresponding lot-book effect using `lot_locations` and
`sell_consumption`, restoring or removing lots as appropriate.

Checkpoint snapshots are generated by `snapshot()`, which derives a trial
balance from `balances` and, per customer, wallet cash from `2010` balances,
cash holds from `holds`, and open positions from `lots` — all computed from
existing state, nothing tracked separately for reporting.

## Design Decisions

- **`Decimal` instead of `float`** for all money, with a single `money()`
  helper quantizing to two decimal places using `ROUND_HALF_UP` rather than
  Python's default banker's rounding.
- **FIFO queues implemented with `deque`** for `lots`, since sells consume
  from the front and reversals of partially-consumed sells need to reinsert
  lots at the front in their original order.
- **Replay-safe event processing** via the `seen` set, checked before any
  handler runs, so duplicate delivery from the stream is a no-op rather than
  a double posting.
- **Centralized malformed numeric payload rejection**: `apply()` catches
  `decimal.InvalidOperation` around the handler dispatch and converts it into
  `Rejected`, handled identically to any other business rejection, so no
  individual handler guards its own `Decimal` conversions.
- **Proportional hold release**: partial fills reduce a hold by
  `original_hold * quantity / order_quantity` rather than tracking a
  separately-computed remainder, and the closing fill or cancellation clears
  whatever is left to exactly zero instead of relying on that computation to
  land there.
- **Immutable journal reversal using archived legs**: reversal never
  recomputes amounts from its own payload (which carries only
  `reverses_event_id`); it always replays the exact legs recorded in
  `legs_by_event` at post time, with debit and credit swapped.

## Repository Notes

`client.py` was provided by the assignment and left functionally unchanged.
All implementation work is contained in `book.py`. The solution follows
`PROTOCOL.md`.
