"""Stage 1 sample — the shipped smoke checks.

A sample, not the graded suite: one happy path across each surface so a service can be
wired against it. The spec is the contract and the graded checks are the spec's. Passing
everything here means the wiring is right, not that the stage holds.
"""
import pytest

import fixtures as fx
import world as sim
from harness.http import assert_status

pytestmark = pytest.mark.stage(1)


def test_health(client):
    assert_status(client().get("/health"), 200)


def test_an_accepted_order_carries_its_times(place):
    order = assert_status(place(at=100), 201).json()["order"]
    assert order["status"] in ("placed", "dispatched")
    assert order["placed_at"] == 100 and order["ready_at"] == 700      # r1 prepares in 600 s
    assert order["promised_by"] == 700 + 20 * 10 + 900                 # 10 blocks to the door
    assert order["customer_id"] == "u1" and order["price"] == 8400


def test_one_order_from_door_to_door(town, dispatched, report):
    order, courier_id, at = dispatched(at=100)
    start = next(c for c in town.body["couriers"] if c["courier_id"] == courier_id)["location"]
    pickup = max(at + sim.travel(start, fx.R1["location"]), order["ready_at"])
    picked = assert_status(report(courier_id, "picked_up", order["order_id"], pickup), 200).json()
    assert picked["courier"]["status"] == "to_dropoff"
    assert picked["courier"]["location"] == fx.R1["location"]
    done = assert_status(report(courier_id, "delivered", order["order_id"], pickup + 200),
                         200).json()
    want = {"courier_id": courier_id, "status": "idle", "location": {"x": 110, "y": 100},
            "order_id": None, "delivered_count": 1}
    assert fx.named(done["courier"], want) == want
    seen = assert_status(town.ada.get(f"/orders/{order['order_id']}"), 200).json()
    assert seen["status"] == "delivered" and seen["delivered_at"] == pickup + 200
    assert seen["courier_id"] == courier_id and seen["picked_up_at"] == pickup


def test_a_delivery_is_split_three_ways(town, dispatched, report):
    order, courier_id, at = dispatched(at=100)
    pickup = max(at + 20 * 100, order["ready_at"])      # later than any courier needs
    assert_status(report(courier_id, "picked_up", order["order_id"], pickup), 200)
    assert_status(report(courier_id, "delivered", order["order_id"], pickup + 200), 200)
    ledger = assert_status(town.ops.get("/ledger"), 200).json()
    want = {"order_id": order["order_id"], "courier_id": courier_id, "restaurant_id": "r1",
            "price": 8400, "restaurant": 5880, "courier": 700, "platform": 1820}
    assert ledger["has_more"] is False and len(ledger["entries"]) == 1
    assert fx.named(ledger["entries"][0], want) == want


def test_a_small_day_adds_up(service):
    """Ten orders between two couriers: the service's account of the day matches the day."""
    day = fx.day([(60 * i, "r1" if i % 2 else "r2", 100 + 3 * i, 110, 5800) for i in range(10)])
    played = sim.World(day, service)
    played.play()
    sim.audit(played)
