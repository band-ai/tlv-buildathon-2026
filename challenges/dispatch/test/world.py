"""The world a Dispatch service lives in: the rules of the four stages as arithmetic, a seeded
day generator, and a simulator that plays a day against a service and works out what it was
worth.

The simulator is every customer and courier app. It places the day's orders, sends a tick
every 60 seconds, moves each courier as the specs say a courier moves, and reports pickups
and deliveries when they happen. It keeps its own record of the day and never trusts the
service's: `play` returns that record, and a service that breaks a rule raises `WorldError`.

A day uses a stage's features only if it carries them: capacities and routes (stage 2),
traffic, shifts, scheduled orders and late reports (stage 3), cancellations, couriers going
offline, reports sent by mistake and taken back, and second restaurants (stage 4).
`World(day, service, stage=N)` says which stage's fields the service's records are compared
on.
"""
from __future__ import annotations

import heapq
import random
import time
from dataclasses import dataclass, field

import httpx

SECONDS_PER_BLOCK = 20
SLACK = 900            # promised_by = ready_at + travel + SLACK
LATE = 1800            # lateness at which a delivery is worth nothing
TICK = 60
SIZE = 400             # the map is SIZE x SIZE
REQUEST_LIMIT = 1.0    # spec §2: per-request timeout
ACTIONS = ("pickup", "second_pickup", "dropoff")
UNDO_AFTER = 30         # seconds from a report sent by mistake to the undo that takes it back


class WorldError(AssertionError):
    """The service broke a rule of the spec while a day was being played."""


def blocks(a: dict, b: dict) -> int:
    """Grid distance between two locations."""
    return abs(a["x"] - b["x"]) + abs(a["y"] - b["y"])


def rate(traffic: list | None, at: int) -> int:
    """Stage 3: seconds per block for a trip that starts at `at`."""
    if not traffic:
        return SECONDS_PER_BLOCK
    return [entry["seconds_per_block"] for entry in traffic if entry["from"] <= at][-1]


def travel(a: dict, b: dict, at: int = 0, traffic: list | None = None) -> int:
    """Seconds to travel between two locations, leaving at `at`."""
    return rate(traffic, at) * blocks(a, b)


def split(price: int, pay: int, second: bool = False) -> dict:
    """A delivered order's price split between restaurant, courier and platform, `pay`
    being its share of the legs it was on board for (stage 2); with `second`, between two
    restaurants (stage 4)."""
    restaurants = price * 70 // 100
    courier = min(600 + pay, price - restaurants)
    parts = {"restaurant": restaurants, "courier": courier,
             "platform": price - restaurants - courier}
    if second:
        parts.update(second_restaurant=restaurants // 2,
                     restaurant=restaurants - restaurants // 2)
    return parts


def value(price: int, promised_by: int, delivered_at: int | None) -> int:
    """Spec §9: what an accepted order was worth, `delivered_at` None if it never arrived."""
    if delivered_at is None or delivered_at - promised_by > LATE:
        return -(price // 2)
    late = delivered_at - promised_by
    return price if late <= 0 else price * (LATE - late) // LATE


def on_shift(shift: dict | None, at: int) -> bool:
    """Stage 3: whether a courier with `shift` works at `at`."""
    return shift is None or shift["start"] <= at < shift["end"]


# ---- days ------------------------------------------------------------------

def make_day(seed: int, *, orders: int = 3000, couriers: int = 120, restaurants: int = 120,  # pylint: disable=too-many-locals,too-many-arguments
             customers: int = 50, length: int = 50_400, stage: int = 1) -> dict:
    """A seeded day: the reset body, the orders customers will place, and its length in
    seconds. Restaurants cluster in districts, customers order from nearby, and demand peaks
    at lunch and dinner. `stage` adds that stage's features and every earlier one's."""
    rng = random.Random(seed)

    def near(center: dict, spread: float) -> dict:
        return {axis: min(SIZE - 1, max(0, round(rng.gauss(center[axis], spread))))
                for axis in ("x", "y")}

    districts = [{"x": rng.randrange(140, 260), "y": rng.randrange(140, 260)}
                 for _ in range(8)]
    places = [{"restaurant_id": f"r{i:03}", "location": near(rng.choice(districts), 15),
               "prep_seconds": rng.randrange(300, 1201, 60)} for i in range(restaurants)]
    popularity = [1 / (rank + 3) for rank in range(restaurants)]
    reset = {
        "restaurants": places,
        "couriers": [{"courier_id": f"c{i:03}", "location": near(rng.choice(districts), 25),
                      "token": f"courier-{seed}-{i}"} for i in range(couriers)],
        "customers": [{"customer_id": f"u{i:03}", "token": f"customer-{seed}-{i}"}
                      for i in range(customers)],
        "ops_token": f"ops-{seed}",
    }

    def moment() -> int:
        kind = rng.random()
        if kind < 0.35:
            at = rng.gauss(length * 0.18, 2700)     # lunch
        elif kind < 0.80:
            at = rng.gauss(length * 0.68, 4200)     # dinner
        else:
            at = rng.uniform(0, length)
        return int(min(length - 1, max(1, at)))

    placed = []
    for at in sorted(moment() for _ in range(orders)):
        place = rng.choices(places, weights=popularity)[0]
        placed.append({"at": at, "customer": rng.randrange(customers),
                       "restaurant_id": place["restaurant_id"],
                       "dropoff": near(place["location"], 25),
                       "price": rng.choice((3200, 4500, 5800, 7400, 8400, 9900, 12500, 18000))})
    day = {"seed": seed, "reset": reset, "orders": placed, "length": length}
    if stage > 1:
        _later_stages(day, stage, random.Random(f"{seed}-later"))
    return day


def _later_stages(day: dict, stage: int, rng: random.Random) -> None:
    """Add the features of stages 2 to `stage` to a stage-1 day, in place."""
    reset, length = day["reset"], day["length"]
    places = {r["restaurant_id"]: r for r in reset["restaurants"]}
    for courier in reset["couriers"]:
        courier["capacity"] = rng.choice((1, 2, 2, 3))
    if stage >= 3:
        def minute(share: float) -> int:
            return int(length * share) // 60 * 60
        reset["traffic"] = [{"from": minute(share), "seconds_per_block": seconds} for
                            share, seconds in ((0, 20), (0.12, 28), (0.26, 20), (0.58, 32),
                                               (0.80, 22))]
        for courier in reset["couriers"]:
            kind = rng.random()
            if kind < 0.3:
                courier["shift"] = {"start": 0, "end": minute(0.55)}
            elif kind < 0.6:
                courier["shift"] = {"start": minute(0.40), "end": length + 7200}
        for order in day["orders"]:
            if rng.random() < 0.15:     # wanted at about the time it would have been placed
                prep = places[order["restaurant_id"]]["prep_seconds"]
                order["scheduled_for"] = order["at"] + prep + 60 * rng.randrange(30)
                order["at"] = rng.randrange(max(1, order["at"] - 14_400), order["at"] + 1)
        day["orders"].sort(key=lambda o: o["at"])
        day["late"] = {"share": 0.05, "longest": 600}
    if stage >= 4:
        for order in day["orders"]:
            if rng.random() < 0.06:
                order["cancel_at"] = order["at"] + rng.randrange(20, 240)
            if rng.random() < 0.12:
                own = places[order["restaurant_id"]]
                here = own["location"]
                close = [r for r in sorted(places.values(),
                                           key=lambda r: blocks(r["location"], here))  # pylint: disable=cell-var-from-loop
                         if r is not own][:5]
                order["second_restaurant_id"] = rng.choice(close)["restaurant_id"]
        gone = rng.sample(reset["couriers"], len(reset["couriers"]) * 12 // 100)
        day["offline"] = {c["courier_id"]: rng.randrange(int(length * 0.15), int(length * 0.85))
                          for c in gone}
        day["mistakes"] = {"share": 0.02}


# ---- services --------------------------------------------------------------

class Http:
    """A service reached over HTTP. Every call returns `(status, body)`; a response slower
    than the spec's limit is a `WorldError`."""

    def __init__(self, base_url: str, limit: float = REQUEST_LIMIT):
        self.client = httpx.Client(base_url=base_url.rstrip("/"), timeout=limit)
        self.limit = limit
        self.calls = 0
        self.slowest = 0.0

    def close(self) -> None:
        """Close the connection pool."""
        self.client.close()

    def reconnect(self) -> None:
        """Drop connections to a process that has been restarted."""
        self.client.close()
        self.client = httpx.Client(base_url=self.client.base_url, timeout=self.limit)

    def reset(self, body: dict) -> None:
        """POST /_test/reset."""
        resp = self.client.post("/_test/reset", json=body, timeout=10)
        if resp.status_code != 204:
            raise WorldError(f"reset answered {resp.status_code}: {resp.text[:200]}")

    def get(self, path: str, token: str, **params) -> dict:
        """GET `path` as `token`; the JSON of a 200 response."""
        resp = self.client.get(path, params=params, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code != 200:
            raise WorldError(f"GET {path} answered {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def post(self, path: str, token: str, body: dict, key: str) -> tuple[int, dict]:
        """POST `path` as `token` with an idempotency key."""
        started = time.monotonic()
        try:
            resp = self.client.post(path, json=body, headers={
                "Authorization": f"Bearer {token}", "Idempotency-Key": key})
        except httpx.TimeoutException:
            raise WorldError(f"POST {path} at {body.get('at')}: no response within "
                             f"{self.limit:g} s") from None
        self.calls += 1
        self.slowest = max(self.slowest, time.monotonic() - started)
        try:
            return resp.status_code, resp.json()
        except ValueError:
            raise WorldError(f"POST {path} answered {resp.status_code} without JSON: "
                             f"{resp.text[:200]}") from None


# ---- the simulator ---------------------------------------------------------

@dataclass
class Result:
    """What the simulator saw happen in a day."""
    orders: dict = field(default_factory=dict)      # service order_id -> the order's true record
    refused: int = 0
    calls: int = 0
    at: int = 0

    @property
    def delivered(self) -> list:
        """Delivered orders, in delivery order."""
        done = [o for o in self.orders.values() if o["delivered_at"] is not None]
        return sorted(done, key=lambda o: o["sequence"])

    @property
    def value(self) -> int:
        """The day's value: every accepted order counted, undelivered ones as lost and
        cancelled ones as nothing."""
        return sum(value(o["price"], o["promised_by"], o["delivered_at"])
                   for o in self.orders.values() if o.get("cancelled_at") is None)


# The order fields a service's record is compared on, and how many of them each stage has.
ORDER_FIELDS = ("customer_id", "restaurant_id", "dropoff", "price", "placed_at", "ready_at",
                "promised_by", "courier_id", "dispatched_at", "picked_up_at", "delivered_at",
                "scheduled_for", "cancelled_at", "second_restaurant_id", "second_ready_at",
                "second_picked_up_at")
FIELD_COUNT = {1: 11, 2: 11, 3: 12, 4: 16}


def stops_of(order_id: str, order: dict) -> list:
    """The stops a newly dispatched order adds to a route, as `(order_id, action)`."""
    middle = [(order_id, "second_pickup")] if order.get("second_restaurant_id") else []
    return [(order_id, "pickup"), *middle, (order_id, "dropoff")]


def status_of(order: dict) -> str:
    """An order's status, from its true record."""
    if order.get("cancelled_at") is not None:
        return "cancelled"
    if order["delivered_at"] is not None:
        return "delivered"
    picked = order["picked_up_at"] is not None and (
        not order.get("second_restaurant_id") or order["second_picked_up_at"] is not None)
    return "picked_up" if picked else "placed" if order["courier_id"] is None else "dispatched"


class World:  # pylint: disable=too-many-instance-attributes
    """One day being played. `step()` handles the next event; `play()` runs to the end.

    A test drives `step()` itself when it wants to do something between events, such as
    restarting the service or moving its state to another one."""

    # At one instant: reports, couriers going offline, cancellations, orders, the tick, the
    # reports whose signal returned with that tick, then reports sent by mistake and undos.
    REPORT, OFFLINE, CANCEL, ORDER, TICK_EVENT, LATE_REPORT, MISTAKE, UNDO = range(8)

    def __init__(self, day: dict, service, stage: int = 1):
        self.day, self.service, self.stage = day, service, stage
        self.result = Result()
        self.traffic = day["reset"].get("traffic")
        self.places = {r["restaurant_id"]: r for r in day["reset"]["restaurants"]}
        self.couriers = {c["courier_id"]: {
            "token": c["token"], "location": c["location"], "delivered_count": 0,
            "capacity": c.get("capacity", 1), "shift": c.get("shift"), "offline": False,
            "held": [], "route": [], "moving": False, "leaving": False, "left_at": 0,
            "trip": 0,          # bumped on every departure: a report of an older trip is void
            "mistaken": None,   # what a report sent by mistake changed, until its undo
            "made": []}     # the stops made, as (position in day["orders"], action)
            for c in day["reset"]["couriers"]}
        self.ops = day["reset"]["ops_token"]
        self.last = None        # the last request sent, for replaying it
        self.last_answer = None
        self.accepted_at = 0    # the highest `at` the service has accepted
        self.ids = {}           # position in day["orders"] -> the service's order_id
        self._pending = None
        self._events: list = []
        self._count = 0
        self._deliveries = 0
        self._mistakes: set = set()
        for index, order in enumerate(day["orders"]):
            self._push(order["at"], self.ORDER, index)
            if "cancel_at" in order:
                self._push(order["cancel_at"], self.CANCEL, index)
        for courier_id, at in day.get("offline", {}).items():
            self._push(at, self.OFFLINE, courier_id)
        self._push(TICK, self.TICK_EVENT, None)
        service.reset(day["reset"])

    def _push(self, at: int, rank: int, data) -> None:
        self._count += 1
        heapq.heappush(self._events, (at, rank, self._count, data))

    # -- what is still going on ---------------------------------------------

    def waiting(self) -> list:
        """Accepted orders not dispatched and not cancelled."""
        return [o for o in self.result.orders.values()
                if o["courier_id"] is None and o.get("cancelled_at") is None]

    def busy(self) -> bool:
        """Whether any courier still has a stop to make."""
        return any(c["route"] for c in self.couriers.values())

    def finished(self) -> bool:
        """The day is over and nothing more can happen that changes its value."""
        at = self.result.at
        return (at >= self.day["length"] and not self.busy()
                and all(at > o["promised_by"] + LATE for o in self.waiting()))

    # -- one event -----------------------------------------------------------

    def step(self) -> bool:
        """Handle the next event. False once the day has finished."""
        if self.finished():
            return False
        at, rank, sequence, data = heapq.heappop(self._events)
        self.result.at = at
        key = f"day{self.day['seed']}-{sequence}"
        if rank == self.ORDER:
            self._place(at, data, key)
        elif rank == self.REPORT:
            trip, data = data
            if trip == self.couriers[data[0]]["trip"]:
                self._report(at, data, key)
        elif rank == self.LATE_REPORT:
            made, data = data
            self._report(made, data, key, sent=at)
        elif rank == self.MISTAKE:
            self._mistake(at, data, key)
        elif rank == self.UNDO:
            self._undo(at, data, key)
        elif rank == self.CANCEL:
            self._cancel(at, data, key)
        elif rank == self.OFFLINE:
            self._offline(at, data, key)
        else:
            self._send("/ticks", self.ops, {"at": at}, key, 200)
            self._push(at + TICK, self.TICK_EVENT, None)
        self._leave(at)
        self.result.calls += 1
        self._check_idle(self.accepted_at)    # the time as the service knows it
        return True

    def play(self) -> Result:
        """Run the day to its end."""
        while self.step():
            pass
        return self.result

    def _send(self, path, token, body, key, *allowed):
        self.last = (path, token, body, key)
        status, answer = self.service.post(path, token, body, key)
        if status not in allowed:
            raise WorldError(f"POST {path} {body} answered {status} {answer}")
        self.last_answer = (status, answer)
        if status < 300:
            # Spec §3: only these move the service's time, and a late report does not.
            self.accepted_at = max(self.accepted_at, body["at"])
            self._apply(self.accepted_at, answer)
        return status, answer

    def _place(self, at, index, key):
        placed = self.day["orders"][index]
        place = self.places[placed["restaurant_id"]]
        body = {"restaurant_id": placed["restaurant_id"], "dropoff": placed["dropoff"],
                "price": placed["price"], "at": at,
                **{k: placed[k] for k in ("scheduled_for", "second_restaurant_id") if k in placed}}
        customer = self.day["reset"]["customers"][placed["customer"]]
        ready = placed.get("scheduled_for", at + place["prep_seconds"])
        record = {"restaurant_id": placed["restaurant_id"], "dropoff": placed["dropoff"],
                  "price": placed["price"], "placed_at": at, "ready_at": ready,
                  "customer_id": customer["customer_id"],
                  "promised_by": ready + travel(place["location"], placed["dropoff"], ready,
                                                self.traffic) + SLACK,
                  "courier_id": None, "dispatched_at": None, "picked_up_at": None,
                  "delivered_at": None, "sequence": None,
                  "scheduled_for": placed.get("scheduled_for"), "cancelled_at": None,
                  "second_restaurant_id": None, "second_ready_at": None,
                  "second_picked_up_at": None, "index": index, "pay": 0,
                  "history": []}  # (at, courier_id or None): who held it from when
        if "second_restaurant_id" in placed:
            second = self.places[placed["second_restaurant_id"]]
            also = placed.get("scheduled_for", at + second["prep_seconds"])
            both = max(ready, also)
            record.update(
                second_restaurant_id=placed["second_restaurant_id"], second_ready_at=also,
                promised_by=both + travel(place["location"], second["location"], both,
                                          self.traffic)
                + travel(second["location"], placed["dropoff"], both, self.traffic) + SLACK)
        # The order has to be known before its own response's dispatches are applied.
        self._pending = (index, record)
        status, answer = self._send("/orders", customer["token"], body, key, 201, 409)
        self._pending = None
        if status == 409:
            if answer.get("error", {}).get("code") != "no_capacity":
                raise WorldError(f"POST /orders {body} answered 409 {answer}")
            self.result.refused += 1

    def _late(self, at, data) -> int | None:
        """Stage 3: when the report of a stop made at `at` is sent, if the courier's app has
        no signal then: the first tick at least a few minutes later. None if it has signal."""
        late = self.day.get("late")
        if not late:
            return None
        courier_id, (order_id, action) = data
        index = self.result.orders[order_id]["index"]
        rng = random.Random(f"{self.day['seed']}-{courier_id}-{index}-{action}")
        if rng.random() >= late["share"]:
            return None
        return -(-(at + rng.randrange(120, late["longest"])) // TICK) * TICK

    def _report(self, at, data, key, sent=None):
        """The courier made a stop at `at`. Without signal the courier waits there and the
        report is sent at `sent`, right after that tick."""
        if sent is None:
            sent = self._late(at, data)
            if sent is not None:
                self._push(sent, self.LATE_REPORT, (at, data))
                return
        courier_id, (order_id, action) = data
        courier, order = self.couriers[courier_id], self.result.orders[order_id]
        self._share_leg(courier, order, action)
        courier["route"].pop(0)
        courier["made"].append((order["index"], action))
        courier["moving"] = False
        if action == "dropoff":
            self._deliveries += 1
            order["delivered_at"], order["sequence"] = at, self._deliveries
            courier["held"].remove(order_id)
            courier.update(location=order["dropoff"],
                           delivered_count=courier["delivered_count"] + 1)
            event = "delivered"
        elif action == "pickup":
            order["picked_up_at"] = at
            courier["location"] = self.places[order["restaurant_id"]]["location"]
            event = "picked_up"
        else:
            order["second_picked_up_at"] = at
            courier["location"] = self.places[order["second_restaurant_id"]]["location"]
            event = "second_picked_up"
        self._send(f"/couriers/{courier_id}/reports", courier["token"],
                   {"event": event, "order_id": order_id, "at": at}, key, 200)
        if courier["leaving"] and not courier["mistaken"]:
            self._offline(at if sent is None else sent, courier_id, key + "-offline")

    def _share_leg(self, courier, order, action) -> None:
        """Stage 2: the leg to this stop is paid once, split between the orders on board."""
        there = order["dropoff"] if action == "dropoff" else self.places[
            order["restaurant_id" if action == "pickup" else "second_restaurant_id"]]["location"]
        aboard = [self.result.orders[i] for i in courier["held"]]
        aboard = [o for o in aboard
                  if o["picked_up_at"] is not None or o["second_picked_up_at"] is not None]
        for carried in aboard:
            carried["pay"] += 10 * blocks(courier["location"], there) // len(aboard)

    def _offline(self, at, courier_id, key):
        """Stage 4: the courier stops taking work, now if it is at a stop or idle, else as
        soon as it has reported the stop it is on its way to."""
        courier = self.couriers[courier_id]
        if courier["moving"] or courier["mistaken"]:
            courier["leaving"] = True
            return
        courier.update(offline=True, leaving=False)
        for order_id in list(courier["held"]):
            order = self.result.orders[order_id]
            if order["picked_up_at"] is None and order.get("second_picked_up_at") is None:
                order.update(courier_id=None, dispatched_at=None)
                order["history"].append((at, None))
                courier["held"].remove(order_id)
                courier["route"] = [stop for stop in courier["route"] if stop[0] != order_id]
        self._send(f"/couriers/{courier_id}/reports", courier["token"],
                   {"event": "offline", "at": at}, key, 200)

    def _cancel(self, at, index, key):
        """Stage 4: the customer tries to cancel; it works while the order is `placed`."""
        order_id = self.ids.get(index)
        if order_id is None:
            return
        order = self.result.orders[order_id]
        customer = self.day["reset"]["customers"][self.day["orders"][index]["customer"]]
        early = order["courier_id"] is None
        if early:
            order["cancelled_at"] = at
        status, answer = self._send(f"/orders/{order_id}/cancel", customer["token"], {"at": at},
                                    key, 200 if early else 409)
        if status == 409 and answer.get("error", {}).get("code") != "too_late":
            raise WorldError(f"cancelling {order_id} at {at} answered 409 {answer}")

    def _stop(self, stop) -> tuple:
        """Where a stop is and the earliest time it can be made."""
        order_id, action = stop
        order = self.result.orders[order_id]
        if action == "dropoff":
            return order["dropoff"], 0
        if action == "pickup":
            return self.places[order["restaurant_id"]]["location"], order["ready_at"]
        return self.places[order["second_restaurant_id"]]["location"], order["second_ready_at"]

    def _leave(self, at):
        """Every courier with a route who is not on its way leaves for its first stop. A
        courier whose last report was a mistake is still on its way to that stop: the
        service thinks it has set out for the next, and it stays put until the undo."""
        for courier_id, courier in self.couriers.items():
            if courier["route"] and not courier["moving"]:
                courier.update(moving=True, left_at=at)
                if not courier["mistaken"]:
                    self._depart(at, courier_id)

    def _depart(self, at, courier_id) -> None:
        """Schedule the report of the courier's next stop, and perhaps a mistaken one first."""
        courier = self.couriers[courier_id]
        courier["trip"] += 1
        stop = courier["route"][0]
        where, ready = self._stop(stop)
        made = max(at + travel(courier["location"], where, at, self.traffic), ready)
        self._push(made, self.REPORT, (courier["trip"], (courier_id, stop)))
        mistakes = self.day.get("mistakes")
        if not mistakes:
            return
        once = (courier_id, self.result.orders[stop[0]]["index"], stop[1])
        rng = random.Random(f"{self.day['seed']}-{once}-mistake")
        early = max((at + made) // 2, ready)
        if once not in self._mistakes and rng.random() < mistakes["share"] \
                and early + UNDO_AFTER < made:
            self._mistakes.add(once)    # one mistake per stop
            self._push(early, self.MISTAKE, (courier["trip"], courier_id))

    def _mistake(self, at, data, key):
        """Stage 4: the courier's app reports the stop it is on its way to before it is made;
        the undo follows `UNDO_AFTER` seconds later."""
        trip, courier_id = data
        courier = self.couriers[courier_id]
        if trip != courier["trip"] or courier["mistaken"]:
            return
        stop = courier["route"][0]
        orders = self.result.orders
        courier["mistaken"] = {
            "stop": stop, "location": courier["location"], "held": list(courier["held"]),
            "delivered_count": courier["delivered_count"],
            "pays": {i: orders[i]["pay"] for i in courier["held"]}}
        courier["trip"] += 1            # the real report of this trip is void now
        self._report(at, (courier_id, stop), key, sent=at)
        self._push(at + UNDO_AFTER, self.UNDO, courier_id)

    def _undo(self, at, courier_id, key):
        """Stage 4: take back the mistaken report; the courier sets out for the stop again."""
        courier = self.couriers[courier_id]
        taken, orders = courier["mistaken"], self.result.orders
        order_id, action = taken["stop"]
        order = orders[order_id]
        released = [i for i in courier["held"] if i not in taken["held"]]
        for other in released:
            orders[other].update(courier_id=None, dispatched_at=None)
            orders[other]["history"].append((at, None))
        if action == "dropoff":
            order.update(delivered_at=None, sequence=None)
        else:
            order["picked_up_at" if action == "pickup" else "second_picked_up_at"] = None
        for other, pay in taken["pays"].items():
            orders[other]["pay"] = pay
        courier["made"].pop()
        courier.update(mistaken=None, held=taken["held"], location=taken["location"],
                       delivered_count=taken["delivered_count"], moving=True, left_at=at,
                       route=[taken["stop"]] + [s for s in courier["route"]
                                                if s[0] not in released])
        self._send(f"/couriers/{courier_id}/reports", courier["token"],
                   {"event": "undo", "order_id": order_id, "at": at}, key, 200)
        self._depart(at, courier_id)

    # -- the service's decisions --------------------------------------------

    def _apply(self, at, answer):  # pylint: disable=too-many-locals,too-many-branches
        """Check a response's dispatches and routes against the rules, and act on them."""
        pending, self._pending = self._pending, None
        if pending is not None and "order" in answer:
            index, record = pending
            order_id = answer["order"].get("order_id")
            if not isinstance(order_id, str) or not order_id or order_id in self.result.orders:
                raise WorldError(f"order_id {order_id!r} is missing or already used")
            for name in ("placed_at", "ready_at", "promised_by"):
                if answer["order"].get(name) != record[name]:
                    raise WorldError(f"order {order_id}: {name} is {answer['order'].get(name)}, "
                                     f"the spec gives {record[name]}")
            self.result.orders[order_id] = record
            self.ids[index] = order_id
        dispatches, routes = answer.get("dispatches"), answer.get("routes", [])
        if not isinstance(dispatches, list) or not isinstance(routes, list):
            raise WorldError(f"`dispatches` and `routes` must be lists, got {answer!r}")
        for item in dispatches:
            courier = self.couriers.get(item.get("courier_id"))
            order = self.result.orders.get(item.get("order_id"))
            if courier is None or order is None:
                raise WorldError(f"dispatch {item} at {at} names an unknown courier or order")
            if len(courier["held"]) >= courier["capacity"]:
                raise WorldError(f"dispatch {item} at {at}: the courier has no room")
            if courier["offline"] or not on_shift(courier["shift"], at):
                raise WorldError(f"dispatch {item} at {at}: the courier is offline or off shift")
            if order["courier_id"] is not None or order.get("cancelled_at") is not None:
                raise WorldError(f"dispatch {item} at {at}: the order is already dispatched "
                                 "or cancelled")
            order.update(courier_id=item["courier_id"], dispatched_at=at)
            order["history"].append((at, item["courier_id"]))
            courier["held"].append(item["order_id"])
            courier["route"] += stops_of(item["order_id"], order)
        named = set()
        for item in routes:
            courier = self.couriers.get(item.get("courier_id"))
            if courier is None or item["courier_id"] in named:
                raise WorldError(f"route {item} at {at} names an unknown courier, or one twice")
            named.add(item["courier_id"])
            try:
                stops = [(stop["order_id"], stop["action"]) for stop in item["stops"]]
            except (KeyError, TypeError):
                raise WorldError(f"route {item} at {at} is malformed") from None
            if sorted(stops) != sorted(courier["route"]):
                raise WorldError(f"route {item} at {at}: the courier's remaining stops are "
                                 f"{courier['route']}")
            if courier["moving"] and stops[0] != courier["route"][0]:
                raise WorldError(f"route {item} at {at} changes the stop the courier is on "
                                 "its way to")
            for order_id, action in stops:
                if action != "dropoff" and stops.index((order_id, action)) > \
                        stops.index((order_id, "dropoff")):
                    raise WorldError(f"route {item} at {at} drops {order_id} off before "
                                     "picking it up")
            courier["route"] = stops

    def _check_idle(self, at):
        """Invariant 4: no courier who could work is idle while an accepted order is
        undispatched past its promise."""
        overdue = [o for o in self.waiting() if o["promised_by"] < at]
        if overdue and any(not c["held"] and not c["offline"] and on_shift(c["shift"], at)
                           for c in self.couriers.values()):
            late = next(i for i, o in self.result.orders.items() if o is overdue[0])
            raise WorldError(f"at {at} a courier is idle while order {late} is undispatched "
                             "past its promised_by")

    # -- for checks that interrupt a day ------------------------------------

    def replay_last(self) -> None:
        """Send the last request again with its key; the answer must be the first one."""
        path, token, body, key = self.last
        again = self.service.post(path, token, body, key)
        if again != self.last_answer:
            raise WorldError(f"POST {path} {body} repeated with its key answered "
                             f"{again}, first {self.last_answer}")


def eta(world: World, order_id: str) -> int | None:
    """Stage 3: when the order's courier makes its dropoff if it follows its route from when
    and where it set out, never earlier than the highest accepted `at`."""
    order = world.result.orders[order_id]
    if status_of(order) not in ("dispatched", "picked_up"):
        return None
    courier = world.couriers[order["courier_id"]]
    when, here = courier["left_at"], courier["location"]
    for stop in courier["route"]:
        where, ready = world._stop(stop)  # pylint: disable=protected-access
        when = max(when + travel(here, where, when, world.traffic), ready, world.accepted_at)
        here = where
        if stop == (order_id, "dropoff"):
            return when
    raise WorldError(f"order {order_id} is held but its dropoff is on no route")


def past_order(order: dict, as_of: int) -> dict:
    """Stage 2: an order's true record as it stood at `as_of`."""
    then = [entry for entry in order["history"] if entry[0] <= as_of]
    courier_id = then[-1][1] if then else None
    past = {**order, "courier_id": courier_id,
            "dispatched_at": then[-1][0] if courier_id else None}
    for name in ("picked_up_at", "delivered_at", "second_picked_up_at", "cancelled_at"):
        if past.get(name) is not None and past[name] > as_of:
            past[name] = None
    return past


def past_summary(world: World, as_of: int) -> dict:
    """Stage 2: the summary as it stood at `as_of`."""
    orders = [o for o in world.result.orders.values() if o["placed_at"] <= as_of]
    live = [o for o in orders if o["cancelled_at"] is None or o["cancelled_at"] > as_of]
    done = [o for o in orders if o["delivered_at"] is not None and o["delivered_at"] <= as_of]
    delivered = {id(o) for o in done}
    entries = [split(o["price"], o["pay"], bool(o.get("second_restaurant_id"))) for o in done]
    summary = {"at": as_of, "accepted": len(orders), "delivered": len(done),
               "value": sum(value(o["price"], o["promised_by"],
                                  o["delivered_at"] if id(o) in delivered else None)
                            for o in live
                            if id(o) in delivered or as_of > o["promised_by"] + LATE),
               "restaurant_total": sum(e["restaurant"] + e.get("second_restaurant", 0)
                                       for e in entries),
               "courier_total": sum(e["courier"] for e in entries),
               "platform_total": sum(e["platform"] for e in entries)}
    if world.stage >= 4:
        summary["cancelled"] = len(orders) - len(live)
    return summary


def audit(world: World) -> None:  # pylint: disable=too-many-locals
    """Compare everything the service reports about the day so far with what happened, on
    the fields the stage's spec names. Raises `WorldError` at the first difference."""
    service, ops, result, stage = world.service, world.ops, world.result, world.stage

    def same(what, got, want):
        if isinstance(want, dict) and isinstance(got, dict):
            got = {name: got.get(name, "(missing)") for name in want}
        if got != want:
            raise WorldError(f"{what}: the service says {got!r}, the day says {want!r}")

    def pages(path, name):
        rows, after = [], None
        while True:
            params = {"limit": 200, **({"after": after} if after else {})}
            body = service.get(path, ops, **params)
            rows += body[name]
            if not body["has_more"]:
                return rows
            after = rows[-1]["order_id"]

    listed = pages("/orders", "orders")
    same("order count", len(listed), len(result.orders))
    same("order sequence", [o["order_id"] for o in listed],
         sorted(result.orders, key=lambda i: (result.orders[i]["placed_at"], i)))
    def record(order_id, truth, estimate):
        want = {**{name: truth[name] for name in ORDER_FIELDS[:FIELD_COUNT[stage]]},
                "order_id": order_id, "status": status_of(truth)}
        if stage >= 3:
            want["eta"] = estimate
        return want

    for order in listed:
        order_id = order["order_id"]
        same(f"order {order_id}", order,
             record(order_id, result.orders[order_id], eta(world, order_id)))
    for courier_id, truth in world.couriers.items():
        route = [{"order_id": order_id, "action": action} for order_id, action in truth["route"]]
        want = {"courier_id": courier_id, "location": truth["location"],
                "status": "idle" if not route else
                "to_dropoff" if route[0]["action"] == "dropoff" else "to_pickup",
                "order_id": route[0]["order_id"] if route else None,
                "delivered_count": truth["delivered_count"]}
        if stage >= 2:
            want.update(capacity=truth["capacity"], order_ids=truth["held"], route=route)
        if stage >= 3:
            want["shift"] = truth["shift"]
        if stage >= 4:
            want["offline"] = truth["offline"]
        same(f"courier {courier_id}", service.get(f"/couriers/{courier_id}", ops), want)
    entries = []
    ids = {id(order): order_id for order_id, order in result.orders.items()}
    for order in result.delivered:
        also = order.get("second_restaurant_id")
        entry = {"order_id": ids[id(order)], "courier_id": order["courier_id"],
                 "restaurant_id": order["restaurant_id"], "price": order["price"],
                 **split(order["price"], order["pay"], bool(also))}
        if stage >= 4:
            entry.update(second_restaurant_id=also,
                         second_restaurant=entry.get("second_restaurant", 0))
        entries.append(entry)
    ledger = pages("/ledger", "entries")
    same("ledger length", len(ledger), len(entries))
    for got, want in zip(ledger, entries):
        same(f"ledger entry {want['order_id']}", got, want)
    live = [o for o in result.orders.values() if o.get("cancelled_at") is None]
    settled = [value(o["price"], o["promised_by"], o["delivered_at"]) for o in live
               if o["delivered_at"] is not None or world.accepted_at > o["promised_by"] + LATE]
    want = {"at": world.accepted_at, "accepted": len(result.orders), "delivered": len(entries),
            "value": sum(settled),
            "restaurant_total": sum(e["restaurant"] + e.get("second_restaurant", 0)
                                    for e in entries),
            "courier_total": sum(e["courier"] for e in entries),
            "platform_total": sum(e["platform"] for e in entries)}
    if stage >= 4:
        want["cancelled"] = len(result.orders) - len(live)
    same("summary", service.get("/summary", ops), want)
    if stage < 2:
        return
    for as_of in sorted({world.accepted_at * share // 4 for share in (1, 2, 3)}):
        same(f"summary as of {as_of}", service.get("/summary", ops, as_of=as_of),
             past_summary(world, as_of))
        then = [order_id for order_id, o in sorted(result.orders.items())
                if o["placed_at"] <= as_of]
        for order_id in then[::max(1, len(then) // 20)]:
            same(f"order {order_id} as of {as_of}",
                 service.get(f"/orders/{order_id}", ops, as_of=as_of),
                 record(order_id, past_order(result.orders[order_id], as_of), None))


def play(day: dict, service, stage: int = 1) -> Result:
    """Play `day` against `service` from reset to the end."""
    return World(day, service, stage).play()
