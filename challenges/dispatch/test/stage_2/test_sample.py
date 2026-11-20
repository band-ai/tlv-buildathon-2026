"""Stage 2 — capacity, routes, the courier record and reading the past. These checks ship
with the kit."""
import pytest

import fixtures as fx
from harness.http import assert_error, assert_status

pytestmark = pytest.mark.stage(2)


@pytest.mark.town(capacity=2)
def test_a_courier_record_shows_what_it_holds(town, dispatched):
    order, courier_id, _at = dispatched(at=100)
    order_id = order["order_id"]
    want = {"capacity": 2, "order_ids": [order_id], "order_id": order_id, "status": "to_pickup",
            "route": [{"order_id": order_id, "action": "pickup"},
                      {"order_id": order_id, "action": "dropoff"}]}
    seen = assert_status(town.ops.get(f"/couriers/{courier_id}"), 200).json()
    assert fx.named(seen, want) == want


def test_a_courier_without_a_capacity_holds_one_order(town):
    listed = assert_status(town.ops.get("/couriers"), 200).json()["couriers"]
    assert [c["courier_id"] for c in listed] == ["c1", "c2"]
    for courier in listed:
        assert courier["capacity"] == 1 and courier["order_ids"] == [] and courier["route"] == []
        assert courier["status"] == "idle" and courier["order_id"] is None


def test_every_answer_that_dispatches_carries_routes(place, tick):
    assert isinstance(assert_status(place(at=100), 201).json()["routes"], list)
    assert isinstance(assert_status(tick(120), 200).json()["routes"], list)


def test_only_ops_lists_the_couriers(town, client):
    assert_error(client().get("/couriers"), 401, "unauthenticated")
    assert_error(town.ada.get("/couriers"), 403, "forbidden")
    assert_error(town.c1.get("/couriers"), 403, "forbidden")


def test_a_small_day_with_room_for_two_adds_up(play):
    """Twelve orders between two couriers who each hold up to two."""
    play(fx.day([(60 * i, "r1" if i % 2 else "r2", 100 + 3 * i, 110, 5800) for i in range(12)],
                capacity=2), 2)


def test_an_order_can_be_read_as_it_stood_earlier(town, place, tick):
    order = assert_status(place(at=100), 201).json()["order"]
    assert_status(tick(160), 200)
    path = f"/orders/{order['order_id']}"
    past = assert_status(town.ops.get(path, params={"as_of": 100}), 200).json()
    assert past["order_id"] == order["order_id"] and past["placed_at"] == 100
    assert_error(town.ops.get(path, params={"as_of": 99}), 404, "not_found")
    assert assert_status(town.ops.get("/summary", params={"as_of": 99}), 200).json()["at"] == 99
