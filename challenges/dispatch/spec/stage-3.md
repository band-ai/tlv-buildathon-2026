# Dispatch — Stage 3: traffic, shifts, scheduled orders and delivery estimates

The requirements of stages 1 and 2 continue to apply, with the changes below. Numbered
section references such as §3 and §6 refer to `stage-1.md`.

Three things that were constant now vary: how long a block takes to travel, when a courier
works, and when an order has to be ready. And every order on its way carries an estimate of
when it will be delivered.

## Traffic

The reset body may carry `"traffic"`: how many seconds one block takes, by time of day.

```json
"traffic": [{"from": 0, "seconds_per_block": 20},
            {"from": 14400, "seconds_per_block": 32},
            {"from": 21600, "seconds_per_block": 20}]
```

The table has at least one entry, the first entry has `from` 0, `from` rises strictly from
entry to entry, and
`seconds_per_block` is an integer from 5 to 120. Without `traffic`, a block takes 20 seconds
all day.

The rate at a time `t` is the `seconds_per_block` of the last entry whose `from` is at most
`t`. The travel time of §3 becomes

```
travel_seconds(a, b, t) = rate(t) × (|a.x − b.x| + |a.y − b.y|)
```

where `t` is the moment the courier leaves `a`. The whole trip takes the rate in force when
it starts.

An order's promise uses the rate at its `ready_at`:

```
promised_by = ready_at + travel_seconds(restaurant, dropoff, ready_at) + 900
```

The courier's share of §9 still counts blocks, not seconds.

## Shifts

A courier in the reset body may carry `"shift": {"start": 3600, "end": 28800}`, integers with
`0 ≤ start < end`. A courier without it is on shift all day. The courier record gains `shift`,
`null` for a courier without one.

A courier is **on shift** at `at` when `start ≤ at < end`.

- A dispatch names a courier who is on shift at the request's `at`.
- A courier keeps the orders it holds when its shift ends, and finishes its route.
- Invariant 4 of §1 counts only couriers who are on shift: no response leaves a courier who
  is on shift idle while an accepted order is undispatched past its `promised_by`.

## Scheduled orders

`POST /orders` may carry `"scheduled_for"`, an integer: the customer wants the food made for
that time. It must be at least `at + prep_seconds` of the restaurant; an earlier value is
`422 validation_failed`.

For a scheduled order `ready_at = scheduled_for`. Its `promised_by` follows from `ready_at` as
for any order. The order object gains `scheduled_for`, `null` for an order that is not
scheduled.

A scheduled order is accepted or refused when it is placed, like any other.

## Late reports

Sometimes a courier's app has no signal when the courier makes a stop. The courier waits at
the stop, and the app sends the report when the signal returns, with the `at` at which the
stop was made. That `at` may be lower than the highest accepted `at`.

- Such a report is not refused with `stale_time` unless its `at` is lower than the `at` of
  the courier's previous accepted report, or, for `picked_up`, lower than the order's
  `dispatched_at`. Both are checked where §4 orders the `stale_time` check.
- It takes effect at its own `at`: `picked_up_at` or `delivered_at` is the report's `at`, and
  the courier is located at the stop. `picked_up` still needs `at ≥ ready_at`.
- The order's value (§9) follows from its `delivered_at`, even if the order had already been
  counted as undelivered past `promised_by + 1800`.
- The report does not lower the highest accepted `at`. The ledger keeps the order in which
  `delivered` reports were accepted.
- Dispatches in its response are made at the highest accepted `at`: that is their
  `dispatched_at`. The courier leaves its stop then.

## Delivery estimates

The order object gains `eta`: when the order will be delivered if every courier makes the
stops of its route in turn, as the courier apps do, and every trip takes exactly its
`travel_seconds`. It is worked out from the routes as they stand at the highest accepted
`at`:

- A courier leaves for the first stop of its route when and where it set out for it: at the
  time the dispatches of the response that sent it there were made, from its location then.
- It reaches a stop `travel_seconds(from, stop, t)` after leaving at `t`. At a pickup it
  waits until the order's `ready_at` if it is early. It leaves for the next stop as soon as
  it has made one.
- A stop the courier has not reported is made no earlier than the highest accepted `at`.
- An order's `eta` is the time its courier makes its dropoff.

`eta` is `null` for an order that is not `dispatched` or `picked_up`, and in a read of the
past (`as_of`, stage 2).

## Upgrades

`POST /_test/import` also accepts a document exported by the service of any earlier stage. No
order in it is scheduled, no courier has a shift, and a block takes 20 seconds all day. A
response completed before the export is repeated exactly as it was.

## Console

`courier-{courier_id}-shift` shows the courier's shift as `3600-28800`, and is empty for a
courier without one.

## A starting policy

The stage-2 policy, with three changes:

> Travel times in a plan use the rate in force when the courier would leave. A courier whose
> last report was late leaves its stop no earlier than the highest accepted `at` then.
>
> A courier's plan starts no earlier than its shift does, and a plan is impossible if the
> courier would leave for an order's pickup at or after the end of its shift.
>
> An order is not dispatched earlier than 1,800 seconds before its `ready_at`. A courier with
> room is dispatched the planned orders that are within that time, in plan order, passing over
> those that are not.
