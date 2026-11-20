# Dispatch — Stage 1: one order per courier

This stage defines the initial service and its API.

Work from the requirements in this document. Source code, API documentation and schemas from
existing products in this domain must not be used.

## 1. Scope

Dispatch is the backend of a food-delivery platform. Customers order from restaurants,
couriers carry the orders, and the service decides which courier carries which order. Only
the HTTP API is required.

The service does not run the world. Customer, restaurant and courier apps call it: they place
orders, report what couriers have done, and act on the dispatches the service returns. Which
order goes to which courier, and when, is the service's decision.

The following apply to all operations, including concurrent requests and retries:

1. An order is dispatched to at most one courier, once.
2. A courier carries at most one order at a time.
3. A dispatch names a courier who is idle and an accepted order that has not been dispatched.
4. No response leaves a courier idle while an accepted order is undispatched past its
   `promised_by`.
5. A delivered order's price is split exactly once between restaurant, courier and platform,
   and the three parts sum to the price.

All times are exact integer seconds and all money is an exact integer count of agorot.
Menus, payments, ratings, tips and customer support are out of scope. Restaurants, couriers
and customers all exist before they can be referenced.

## 2. Delivery and deployment

Deliver an HTTP service, a `Dockerfile` and a `RUN.md` with a command that builds and
starts the service without manual setup. Language, framework and storage are unrestricted.

The submitted `Dockerfile` is built, the resulting image is started with `-e PORT=<port>`
and a port mapping, and only its HTTP behaviour is exercised. Runtime networking has no
outbound access.

### Resource limits

| Limit | Value |
|---|---|
| CPU | 2 vCPU |
| Memory | 3 GiB |
| Start to first healthy response | 60 s |
| Concurrent requests | up to 50 in flight |
| Data volume | up to 300 restaurants, 200 couriers and 5,000 orders in a day |
| Per-request timeout | 1 s (10 s for `POST /_test/reset` and `POST /_test/import`) |
| Outbound network | available during `docker build`, **none at run time** |
| Disk | kept across a restart of the container |

State survives a restart of the container, including one that gives the process no chance to
shut down: every response the service has sent stays true after it.

## 3. Time and the map

**Time.** A day starts at second 0. Every request that changes state carries `at`, an integer
number of seconds since the day started, 0 or more. The service takes the time from `at` and never from
its own clock. A request whose `at` is lower than the highest `at` the service has accepted
is refused with `409 stale_time`, changing nothing. The highest accepted `at` rises only with a
`200` or `201` response.

**Map.** A location is `{"x": int, "y": int}` with both in `0..399`. The travel time between
two locations is

```
travel_seconds(a, b) = 20 × (|a.x − b.x| + |a.y − b.y|)
```

A courier travels straight to its task and takes exactly that long.

## 4. Runtime contract

- `GET /health` returns `200` once the service is ready.
- Errors are `{"error": {"code": "<code>", "message": "<text>"}}` with the status given in
  this document. A malformed body, a missing field, a wrong type or a value out of range is
  `422 validation_failed`, and so is an unknown `restaurant_id` in a body. A request body,
  or an object inside it, with a field that is not named for it is `422 validation_failed`. An unknown id in a
  path is `404 not_found`.
- A request is checked in this order, and the first failure is the response: the token, what
  the caller may do, the ids in the path, the body, a repeated key (§10), `stale_time` (§3),
  then the state the request needs.
- `POST /_test/reset` replaces all state with the body below and returns `204`. An invalid
  body is `422 validation_failed` and changes nothing.

```json
{
  "restaurants": [{"restaurant_id": "r1", "location": {"x": 10, "y": 12}, "prep_seconds": 600}],
  "couriers":    [{"courier_id": "c1", "location": {"x": 3, "y": 40}, "token": "..."}],
  "customers":   [{"customer_id": "u1", "token": "..."}],
  "ops_token": "..."
}
```

Ids and tokens are unique non-empty strings. After a reset the highest accepted `at` is 0,
every courier is `idle` at its seeded location, and there are no orders.

## 5. Callers

Every endpoint except `/health` and the `/_test/` endpoints requires
`Authorization: Bearer <token>`. A missing or unknown token is `401 unauthenticated`. A token
that is known but not allowed to make the call is `403 forbidden`.

| Caller | May |
|---|---|
| customer | place orders; read their own orders |
| courier | send their own reports; read their own courier record |
| ops | send ticks; read every order, courier, the summary and the ledger |

Another customer's order is `404 not_found` to a customer, not `403`.

## 6. Orders

```
POST /orders          customer
{"restaurant_id": "r1", "dropoff": {"x": 30, "y": 18}, "price": 8400, "at": 3605}
```

`price` is an integer from 1,000 to 100,000. The service either accepts or refuses.

**Accepted:** `201` with the order and any dispatches (§8):

```json
{
  "order": {
    "order_id": "...", "customer_id": "u1", "restaurant_id": "r1",
    "dropoff": {"x": 30, "y": 18}, "price": 8400,
    "status": "placed", "placed_at": 3605, "ready_at": 4205, "promised_by": 5625,
    "courier_id": null, "dispatched_at": null, "picked_up_at": null, "delivered_at": null
  },
  "dispatches": []
}
```

- `ready_at = placed_at + prep_seconds` of the restaurant.
- `promised_by = ready_at + travel_seconds(restaurant, dropoff) + 900`.
- `status` is `placed`, `dispatched`, `picked_up` or `delivered`. If this response dispatches
  the order itself, the order in it already shows `dispatched`.

**Refused:** `409 no_capacity`. Nothing is created. The service may refuse any order it
judges it cannot deliver; the decision is its own.

`GET /orders/{order_id}` returns the order object. `GET /orders` (ops) returns
`{"orders": [...], "has_more": bool}` in `placed_at` order, then `order_id`, with optional
`status`, `limit` (1 to 200, default 50) and `after` (an `order_id`) query parameters.
An unknown `status`, a `limit` outside its range or an `after` that is not a known order is
`422 validation_failed`. Wherever ids give an order, they compare as strings, character by
character.

## 7. Couriers

A courier is `idle`, `to_pickup` or `to_dropoff`.

```
GET /couriers/{courier_id}
{"courier_id": "c1", "status": "to_pickup", "location": {"x": 3, "y": 40},
 "order_id": "...", "delivered_count": 4}
```

`location` is where the courier was at its last report, or its seeded location.
`order_id` is `null` when idle.

A courier's app reports what the courier has done:

```
POST /couriers/{courier_id}/reports
{"event": "picked_up", "order_id": "...", "at": 4300}
```

| `event` | Allowed when | Effect |
|---|---|---|
| `picked_up` | the courier is `to_pickup` for this order and `at ≥ ready_at` | order `picked_up`, `picked_up_at = at`; courier `to_dropoff`, located at the restaurant |
| `delivered` | the courier is `to_dropoff` for this order | order `delivered`, `delivered_at = at`; courier `idle`, located at the drop-off; `delivered_count` rises by one; the price is split (§9) |

Refusals, each changing nothing, in this order: `order_id` is not this courier's current
order, which includes an unknown id and a courier with no order, `409 wrong_order`; the
courier is not in the state the event needs, `409 wrong_state`; `picked_up` before
`ready_at`, `409 not_ready`.

A successful report returns `200 {"courier": {...}, "dispatches": [...]}`. The courier is
shown as that response's own dispatches leave it.

What the courier apps do with a dispatch: the courier leaves its location at the dispatch's
`at`, reaches the restaurant `travel_seconds` later, waits there until `ready_at` if it is
early, reports `picked_up`, travels to the drop-off and reports `delivered`. An idle courier
stays where it is.

## 8. Dispatches

A dispatch gives one order to one courier: `{"courier_id": "c1", "order_id": "..."}`.

The service issues dispatches only in the `dispatches` array of a `201` or `200` response to
one of three requests: placing an order, a courier report, and a tick.

```
POST /ticks           ops
{"at": 3660}
→ 200 {"dispatches": [...]}
```

Ticks arrive every 60 seconds of the day. They are the service's chance to act when nothing
else is happening. A tick is valid at any `at`.

A dispatch takes effect at the request's `at`: the order becomes `dispatched` with
`courier_id` and `dispatched_at = at`, and the courier becomes `to_pickup` with that
`order_id`. A courier who reports `delivered` is idle from that moment and may be dispatched
in the same response. One response never names a courier or an order twice.

## 9. Money and the day's value

When an order is delivered its price is split:

```
restaurant = floor(price × 70 / 100)
courier    = min(600 + 10 × blocks, price − restaurant)
platform   = price − restaurant − courier
```

where `blocks = |restaurant.x − dropoff.x| + |restaurant.y − dropoff.y|`.

`GET /ledger` (ops) returns `{"entries": [...], "has_more": bool}`, one entry per delivered
order, in the order the `delivered` reports were accepted, with the same `limit` and `after`
(an `order_id`) parameters:

```json
{"order_id": "...", "courier_id": "c1", "restaurant_id": "r1",
 "price": 8400, "restaurant": 5880, "courier": 860, "platform": 1660}
```

An order's **value** depends on when it arrives. With `late = delivered_at − promised_by`:

| Outcome | Value |
|---|---|
| delivered, `late ≤ 0` | `price` |
| delivered, `0 < late ≤ 1800` | `floor(price × (1800 − late) / 1800)` |
| delivered, `late > 1800` | `−floor(price / 2)` |
| accepted and still undelivered when the latest `at` is past `promised_by + 1800` | `−floor(price / 2)` |
| refused | 0 |

An undelivered order's value does not change if it is delivered after that point.

`GET /summary` (ops) returns the day so far, as of the highest accepted `at`:

```json
{"at": 86400, "accepted": 2950, "delivered": 2901, "value": 21877340,
 "restaurant_total": 17012880, "courier_total": 2466350, "platform_total": 4824770}
```

`value` is the sum of the values above over accepted orders that have one. The three totals
are sums over the ledger.

## 10. Retries

`POST /orders`, `POST /couriers/{id}/reports` and `POST /ticks` require an `Idempotency-Key`
header of 1 to 200 characters; a missing or invalid one is `422 validation_failed`.

Keys are scoped to the caller. Repeating a request with the same key and the same body
returns the first response again, status and body, and changes nothing: no second order, no
second dispatch, no second ledger entry. That includes a first response that was a refusal.
The same key with a different body or on a different path is `409 idempotency_key_reuse`. A repeat is recognised
before the `stale_time` check, so a retry of an earlier request still returns its response.

A repeat is recognised after a restart of the container (§2).

## 11. State transfer

`GET /_test/export` returns a JSON document from which `POST /_test/import` restores the
service: the restaurants, couriers, customers and tokens of the reset, every order and ledger
entry, the highest accepted `at`, and the completed responses of §10. A successful import
returns `204`. After an import, the service behaves as the exporting one would have. The
format of the document is the service's own. An import of a document the service did not
produce is `422 validation_failed` and changes nothing.

## 12. A starting policy

The following policy satisfies this document. It is a starting point, not a requirement.

> Keep a queue of orders for each courier. A courier's queue has a value: starting from when
> and where the courier will next be free, the courier carries the queued orders in turn,
> and each is worth what §9 gives for the time it would arrive. A queue is impossible if
> any order in it would be dispatched after its `promised_by` or arrive more than 1,800
> seconds late.
>
> When an order is placed, try it at every position of every courier's queue and put it
> where a queue's value rises most; on a tie, the lowest `courier_id`, then the earliest
> position. If no position raises a queue's value by at least three quarters of the
> order's `price`, refuse the order.
>
> Whenever a request is handled, each idle courier with a queue is dispatched the first
> order in it.

How well a day went is its `value` (§9). A service that dispatches better than this policy
ends the same day with a higher one.
