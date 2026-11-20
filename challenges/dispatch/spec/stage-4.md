# Dispatch — Stage 4: cancellations, couriers going offline, undone reports and two-restaurant orders

The requirements of stages 1 to 3 continue to apply, with the changes below. Numbered section
references such as §6 and §9 refer to `stage-1.md`.

A day no longer goes to plan. Customers cancel, couriers stop working in the middle of a
route, couriers' apps take back reports sent by mistake, and an order may come from two
restaurants.

## Cancellations

```
POST /orders/{order_id}/cancel        customer
{"at": 3700}
```

The request needs an `Idempotency-Key` and follows §10. Another customer's order is
`404 not_found`.

- While the order is `placed`: it becomes `cancelled`, with `cancelled_at = at`. The response
  is `200 {"order": {...}, "dispatches": [...], "routes": [...]}`.
- In any other status: `409 too_late`, changing nothing.

`cancelled` is a fifth order status. The order object gains `cancelled_at`, `null` unless
cancelled, and in a read of the past (`as_of`, stage 2) also when the order was cancelled
after `as_of`. A cancelled order is never dispatched, has value 0 even if it had already been
counted as lost, and does not count for invariant 4. `GET /orders` accepts
`status=cancelled`. `GET /summary` gains `cancelled`, the number of cancelled orders; `accepted`
still counts them. As of a time, `cancelled` counts the orders cancelled by then, and an order
cancelled later is valued as it stood then.

## Couriers going offline

A courier's app may report that the courier has stopped taking work:

```
POST /couriers/{courier_id}/reports
{"event": "offline", "at": 20400}
```

An `offline` report has no `order_id`; one with an `order_id` is `422 validation_failed`. It
is never late: an `at` lower than the highest accepted `at` is `409 stale_time`. The
app sends it only when the courier is idle, or right after the courier has reported a stop
and before it leaves for the next one.

- The courier is offline for the rest of the day. The courier record gains `offline`, a
  boolean. A second `offline` report is `409 wrong_state`.
- Every order the courier holds and has picked nothing up for goes back to `placed`: its
  `courier_id` and `dispatched_at` are `null` again and its stops leave the courier's route.
  It may be dispatched again, to another courier.
- The courier keeps the other orders it holds and finishes its route.
- No dispatch names an offline courier, and invariant 4 does not count one.

The response is `200 {"courier": {...}, "dispatches": [...], "routes": [...]}`, as for any
report. The offline courier has not left yet, so its remaining stops may be reordered in it.

Invariant 1 of §1 becomes: an order is dispatched to at most one courier at a time. A
dispatch names an order that is `placed`.

## Undoing a report

A courier's app may take back the courier's last report of a stop when it was sent by
mistake, before the courier had made the stop:

```
POST /couriers/{courier_id}/reports
{"event": "undo", "order_id": "...", "at": 20460}
```

`order_id` names the order of the report taken back. Like `offline`, an `undo` is never late:
an `at` lower than the highest accepted `at` is `409 stale_time`. Refusals, each changing
nothing, in this order: the courier's last accepted report is not `picked_up`,
`second_picked_up` or `delivered`, which includes a courier with no report and one whose last
report was `offline` or `undo`, `409 wrong_state`; `order_id` is not that report's order,
`409 wrong_order`.

An undo puts back what the report it takes back changed:

- The stop is the first stop of the courier's route again. The order's `picked_up_at`,
  `second_picked_up_at` or `delivered_at` is `null` again, with the status that follows. A
  delivered order is held by the courier again, in its place in `order_ids`; its ledger entry
  is removed, the courier's `delivered_count` goes back down and the order is valued as one
  not delivered.
- The `pay` the report added to the orders on board (stage 2, "Shared travel") is taken off
  again.
- The courier is located where it was before that report.
- Every order dispatched to the courier in the response to that report or since goes back
  to `placed`, as when a courier goes offline.

The courier is on its way to the stop again: it sets out for it at the undo's `at`, from where
it was before the report, and a new route for it must start with that stop. In a read of the
past (`as_of`), the stop was never made; everything else the undo changes, it changes at its
own `at`.

The response is `200 {"courier": {...}, "dispatches": [...], "routes": [...]}`.

## Two-restaurant orders

`POST /orders` may carry `"second_restaurant_id"`: part of the order comes from a second
restaurant. It must name a known restaurant other than `restaurant_id`; anything else is
`422 validation_failed`.

The order object gains three fields, all `null` for an order from one restaurant:
`second_restaurant_id`, `second_ready_at` and `second_picked_up_at`. In a read of the past,
`second_picked_up_at` is `null` if the second pickup was made after `as_of`.

- `second_ready_at = placed_at + prep_seconds` of the second restaurant, or `scheduled_for`
  for a scheduled order. The earliest `scheduled_for` allowed still follows from the first
  restaurant's `prep_seconds` alone.
- With `ready = max(ready_at, second_ready_at)`:

  ```
  promised_by = ready + travel_seconds(restaurant, second restaurant, ready)
                      + travel_seconds(second restaurant, dropoff, ready) + 900
  ```

**Stops.** The order needs three stops: `pickup`, at the first restaurant; `second_pickup`,
at the second; and `dropoff`. The two pickups may come in either order; the dropoff comes
after both. A dispatch adds them to the end of the route as pickup, second pickup, dropoff.

**Reports.** `picked_up` reports the first restaurant's stop, needs `at ≥ ready_at` and sets
`picked_up_at`, whether or not the other pickup has been made. A new
event, `second_picked_up`, reports the second restaurant's stop, needs `at ≥ second_ready_at`
and sets `second_picked_up_at`. The courier is then located at that restaurant. The
refusals of stage 2 apply to both. At either pickup the courier app waits for that
restaurant's ready time, and so does a courier in `eta` (stage 3). A late
`second_picked_up` follows the stage-3 rules for a late `picked_up`.

**Status.** The order is `picked_up` once both pickups are reported. Until then it stays
`dispatched`. A courier's `status` is `to_pickup` when its first stop is either kind of
pickup. An order with one pickup reported has been picked up for the purposes of a
cancellation and of a courier going offline: it cannot be cancelled, and the courier keeps
it.

**Money.** The order is on board from its first pickup, either one, until it is delivered;
`pay` is its share of the legs travelled in that time (stage 2).

```
restaurants       = floor(price × 70 / 100)
second_restaurant = floor(restaurants / 2)
restaurant        = restaurants − second_restaurant
courier           = min(600 + pay, price − restaurants)
platform          = price − restaurants − courier
```

A ledger entry gains `second_restaurant_id` and `second_restaurant`, `null` and 0 for an
order from one restaurant. `restaurant_total` in the summary sums both restaurants' parts.

## Upgrades

`POST /_test/import` also accepts a document exported by the service of any earlier stage. No
order in it is cancelled or has a second restaurant, and no courier is offline. A response
completed before the export is repeated exactly as it was.

## Console

- `orders-status` has a sixth option, `cancelled`.
- A second pickup appears in `courier-{courier_id}-route` as `second_pickup o4`.
- `summary-cancelled` shows the summary's `cancelled`.

## A starting policy

The stage-3 policy, with these changes:

> An order from two restaurants is tried with its two pickups next to each other, in either
> order.
>
> A cancelled order leaves the plan it was in.
>
> When a courier goes offline, its plan keeps only the orders it still holds. Each order
> that left it is tried in the other couriers' plans as a newly placed order is, earliest
> `promised_by` first, and stays unplanned where a newly placed order would be refused.
>
> An offline courier's plan takes no new order.
>
> After an undo, the courier's plan starts with the stop taken back and loses the orders that
> went back to `placed`. Each of those is tried in every plan as a newly placed order is,
> earliest `promised_by` first, and stays unplanned where a newly placed order would be
> refused.
>
> An order that is undispatched past its `promised_by` is dispatched to an idle courier who
> is on shift and not offline, if there is one: the lowest `courier_id`.
