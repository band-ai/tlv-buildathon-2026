# Dispatch — Stage 2: routes, reading the past and the console

The stage-1 requirements continue to apply, with the changes below. Numbered section
references such as §7 and §9 refer to `stage-1.md`.

This stage has three parts. A courier may now hold several orders at once and visit their
restaurants and customers in an order the service chooses. The operations team can read an
order and the summary as they stood earlier in the day. And it gets a console in the
browser.

## Capacity

A courier in the reset body may carry `"capacity"`, an integer from 1 to 4. A courier without
it has capacity 1.

A courier **holds** an order from the moment it is dispatched to it until it is delivered.
Invariants 2 and 3 of §1 become:

2. A courier holds at most `capacity` orders at a time.
3. A dispatch names a courier who holds fewer orders than its capacity and an accepted order
   that has not been dispatched.

A courier is `idle` when it holds no order. Invariant 4 is unchanged.

## Routes

A courier's **route** is the ordered list of the stops it still has to make. A stop is
`{"order_id": "...", "action": "pickup"}` or `{"order_id": "...", "action": "dropoff"}`.
The route always holds exactly the stops its held orders still need: a pickup and a dropoff
for an order not yet picked up, a dropoff for one that is. An order's pickup comes before its
dropoff.

A dispatch adds the order's pickup and dropoff to the end of the courier's route. One
response may name a courier in several dispatches; it still never names an order twice.

Every `200` or `201` response to placing an order, a courier report or a tick carries, beside
`dispatches`, a `routes` array. It may be empty. Each item replaces one courier's route:

```json
{"dispatches": [{"courier_id": "c1", "order_id": "o7"}],
 "routes": [{"courier_id": "c1", "stops": [
   {"order_id": "o4", "action": "pickup"}, {"order_id": "o7", "action": "pickup"},
   {"order_id": "o7", "action": "dropoff"}, {"order_id": "o4", "action": "dropoff"}]}]}
```

A route in a response is applied after that response's dispatches. It names a courier at most
once per response and must be a valid route for the orders the courier then holds.

**A courier on its way keeps its next stop.** A courier is on its way from the moment a
response leaves it with a route until it reports the route's first stop. While it is on its
way, a new route for it must start with that same stop. The response to a courier's own report
may reorder all of that courier's remaining stops: it has not left yet.

### What the courier apps do

This replaces the last paragraph of §7.

- An idle courier stays where it is. When a response gives it a route, it leaves at that
  response's `at` for the first stop.
- It reaches a stop `travel_seconds` after leaving for it. At a pickup it waits until the
  order's `ready_at` if it is early. Then it reports the stop: `picked_up` or `delivered`.
- After the response to that report it leaves at once, at the report's `at`, for the first
  stop of the route it now has. With an empty route it is idle.

### Reports

A report is allowed when the stop it reports is the first stop of the courier's route:
`picked_up` for a pickup, `delivered` for a dropoff. `picked_up` still needs `at ≥ ready_at`.
The stop then leaves the route.

Refusals, each changing nothing, in this order: the courier does not hold the order, which
includes an unknown id, `409 wrong_order`; the stop the event reports is not the first stop of
the courier's route, `409 wrong_state`; `picked_up` before `ready_at`, `409 not_ready`.

A successful report returns `200 {"courier": {...}, "dispatches": [...], "routes": [...]}`,
the courier shown as that response's dispatches and routes leave it.

### The courier record

```json
{"courier_id": "c1", "status": "to_pickup", "location": {"x": 3, "y": 40},
 "order_id": "o4", "delivered_count": 4, "capacity": 2, "order_ids": ["o4", "o7"],
 "route": [{"order_id": "o4", "action": "pickup"}, {"order_id": "o7", "action": "pickup"},
           {"order_id": "o7", "action": "dropoff"}, {"order_id": "o4", "action": "dropoff"}]}
```

- `order_ids` are the orders the courier holds, in the order they were dispatched to it;
  those dispatched in one response, in the order of its `dispatches`.
- `route` is the courier's current route.
- `status` is `idle` with an empty route, `to_pickup` when the first stop is a pickup and
  `to_dropoff` when it is a dropoff. `order_id` is the first stop's order, or `null`.

`GET /couriers` (ops) returns `{"couriers": [...]}`: every courier's record, in `courier_id`
order.

## Shared travel

A courier carrying several orders is paid once for its travel, and the orders share it. This
replaces `10 × blocks` in the courier's part of §9:

```
courier = min(600 + pay, price − restaurant)
```

`pay` is the order's share of the legs it travelled. A leg is the courier's trip from one
stop to the next. Its `blocks` are `|a.x − b.x| + |a.y − b.y|` between the two stops, and
the `n` orders on board when the courier leaves each get `floor(10 × blocks / n)` agorot of
it. An order is on board from the stop where it is picked up until the stop where it is
delivered. A trip with no order on board pays nothing.

## Reading the past

`GET /orders/{order_id}` and `GET /summary` accept an `as_of` query parameter: a time of the
day, an integer from 0 to the highest accepted `at`. Anything else is
`422 validation_failed`, checked after the order id in the path.

With `as_of` the response shows the day as it stood at that time: everything that happened at
or before `as_of`, in the order it happened, and nothing that happened later.

- An order placed after `as_of` is `404 not_found`.
- In an order, `dispatched_at`, `picked_up_at` and `delivered_at` are `null` if what they
  record happened after `as_of`, and so is the `courier_id` of an order not dispatched to a
  courier at that time. `status` is the status those fields give.
- The summary's `at` is `as_of`. `accepted` counts the orders placed by then, `delivered` and
  the three totals the orders delivered by then, and `value` (§9) is worked out as of
  `as_of`: an order counts as delivered if it was delivered by then, and as undelivered past
  its promise if `as_of` is past its `promised_by + 1800` and it was not.

## Upgrades

`POST /_test/import` also accepts a document exported by the stage-1 service. Every courier
in it has capacity 1, and the day continues from where the export left it. A response
completed before the export is repeated exactly as it was.

## Console

`GET /console` serves a page for the operations team. It is served by the service itself and
loads nothing from any other host. Server-side and client-side rendering are both permitted.

The page must expose the `data-testid` attributes below. Additional elements are permitted
and the visual design is the team's choice.

| `data-testid` | Element |
|---|---|
| `console-token` | Text input for the ops token |
| `console-open` | Button. Reads the day with the token in `console-token` and shows it |
| `console-refresh` | Button. Reads the day again, keeping the token and the order filter |
| `console-error` | Visible, with a message, when the token is not the ops token. No part of the day is shown with it. Absent or hidden otherwise |
| `summary-at`, `summary-accepted`, `summary-delivered`, `summary-value` | The four fields of `GET /summary`, each as a plain decimal integer |
| `couriers` | Container. Its children are the courier rows, in `courier_id` order |
| `courier-{courier_id}` | One courier's row |
| `courier-{courier_id}-status` | The courier's `status` |
| `courier-{courier_id}-load` | Orders held and capacity, as `1/2` |
| `courier-{courier_id}-route` | The route: each stop as `pickup o4` or `dropoff o4`, joined by ` > `. Empty for an idle courier |
| `orders-status` | `select` with five options, whose values are the empty string (every order) and the four order statuses. Choosing one shows the matching orders |
| `orders` | Container. Its children are the order rows: the first 50 orders of `GET /orders` with the chosen status, in that order |
| `order-{order_id}` | One order's row |
| `order-{order_id}-status` | The order's `status` |
| `order-{order_id}-courier` | The order's `courier_id`, empty when it has none |

Before `console-open` succeeds the page shows no summary, courier or order.

## A starting policy

The policy of §12, with a courier's queue replaced by a plan of stops:

> Keep a plan for each courier: the stops of its route, followed by a pickup and a dropoff
> for every order planned for it and not yet dispatched. A plan's value is the sum of what
> its orders are worth when the courier makes the stops in turn. A plan is impossible if the
> courier would have more orders on board than its capacity at any point, if it would leave
> for an order's pickup after the order's `promised_by`, or if an order would arrive more
> than 1,800 seconds late.
>
> When an order is placed, try its pickup and its dropoff at every pair of positions in every
> courier's plan, leaving the next stop of a courier on its way where it is. Put the order
> where a plan's value rises most; on a tie, the lowest `courier_id`, then the earliest
> pickup position, then the earliest dropoff position. If no place raises a plan's value
> by at least three quarters of the order's `price`, refuse the order.
>
> Whenever a request is handled, each courier with room is dispatched the planned orders
> whose pickups come first in its plan, and its route becomes the plan's stops for the orders
> it holds.
