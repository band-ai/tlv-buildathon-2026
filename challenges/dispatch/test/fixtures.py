"""A small town for the checks that do not need a whole day."""
from __future__ import annotations

R1 = {"restaurant_id": "r1", "location": {"x": 100, "y": 100}, "prep_seconds": 600}
R2 = {"restaurant_id": "r2", "location": {"x": 130, "y": 100}, "prep_seconds": 300}
OPS = "ops-token"


def town(couriers: int = 2, customers: int = 2, *, capacity: int | None = None,
         shifts: tuple = (), traffic: list | None = None) -> dict:
    """A reset body: two restaurants 30 blocks apart, couriers lined up west of r1.
    `capacity` goes to every courier, `shifts` are `(start, end)` or None per courier."""
    body = {
        "restaurants": [dict(R1), dict(R2)],
        "couriers": [{"courier_id": f"c{i + 1}", "location": {"x": 90 - 10 * i, "y": 100},
                      "token": f"courier-token-{i + 1}"} for i in range(couriers)],
        "customers": [{"customer_id": f"u{i + 1}", "token": f"customer-token-{i + 1}"}
                      for i in range(customers)],
        "ops_token": OPS,
    }
    for courier, shift in zip(body["couriers"], shifts):
        if shift:
            courier["shift"] = {"start": shift[0], "end": shift[1]}
    if capacity is not None:
        for courier in body["couriers"]:
            courier["capacity"] = capacity
    if traffic is not None:
        body["traffic"] = traffic
    return body


def named(got: dict, want: dict) -> dict:
    """The fields of `got` that `want` names: a response may carry more than a check reads."""
    return {name: got.get(name, "(missing)") for name in want}


def day(orders: list, couriers: int = 2, length: int = 3600, **changes) -> dict:
    """A day in the town for `world.World`: `orders` are (at, restaurant_id, x, y, price).
    `changes` go to `town`."""
    return {"seed": 0, "reset": town(couriers, **changes), "length": length, "orders": [
        {"at": at, "customer": 0, "restaurant_id": place, "dropoff": {"x": x, "y": y},
         "price": price} for at, place, x, y, price in orders]}
