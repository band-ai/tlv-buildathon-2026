"""Stage 4 — cancellations, couriers going offline, undone reports and second restaurants.
These checks ship with the kit."""
import pytest

import fixtures as fx
from harness.http import assert_error, assert_status, new_key

pytestmark = pytest.mark.stage(4)


def cancel(who, order_id, at, key=None):
    """Cancel an order as `who`."""
    return who.post(f"/orders/{order_id}/cancel", json={"at": at},
                    idempotency_key=key or new_key())


@pytest.mark.town(shifts=((5000, 20000), (5000, 20000)))
def test_a_placed_order_can_be_cancelled(town, place):
    """Nobody starts work before 5000, so an order for then is still `placed` at 130."""
    order = assert_status(place(at=100, scheduled_for=5000), 201).json()["order"]
    assert order["cancelled_at"] is None and order["second_restaurant_id"] is None
    done = assert_status(cancel(town.ada, order["order_id"], 130), 200).json()
    assert done["order"]["status"] == "cancelled" and done["order"]["cancelled_at"] == 130
    assert isinstance(done["dispatches"], list) and isinstance(done["routes"], list)
    summary = assert_status(town.ops.get("/summary"), 200).json()
    assert (summary["accepted"], summary["cancelled"], summary["value"]) == (1, 1, 0)


def test_a_dispatched_order_cannot_be_cancelled(town, dispatched):
    order, _courier_id, at = dispatched(at=100)
    assert_error(cancel(town.ada, order["order_id"], at + 10), 409, "too_late")


def test_an_order_from_two_restaurants(place):
    order = assert_status(place(at=100, second_restaurant_id="r2"), 201).json()["order"]
    assert order["second_restaurant_id"] == "r2" and order["second_picked_up_at"] is None
    assert (order["ready_at"], order["second_ready_at"]) == (700, 400)   # r2 prepares in 300 s
    # r1 to r2 is 30 blocks, r2 on to the door at (110, 100) another 20.
    assert order["promised_by"] == 700 + 20 * (30 + 20) + 900


def test_a_courier_can_go_offline(town):
    gone = assert_status(town.c1.post("/couriers/c1/reports", json={"event": "offline", "at": 50},
                                      idempotency_key=new_key()), 200).json()
    assert gone["courier"]["offline"] is True and gone["courier"]["status"] == "idle"
    assert assert_status(town.ops.get("/couriers/c2"), 200).json()["offline"] is False


def test_a_small_day_that_does_not_go_to_plan(play):
    day = fx.day([(60 * i, "r1" if i % 2 else "r2", 100 + 3 * i, 110, 5800) for i in range(14)],
                 couriers=3, capacity=2)
    day["orders"][2]["cancel_at"] = 150
    day["orders"][5]["second_restaurant_id"] = "r2"
    day["offline"] = {"c2": 500}
    play(day, 4)


def test_a_courier_with_no_report_has_nothing_to_undo(town):
    answer = town.c1.post("/couriers/c1/reports",
                          json={"event": "undo", "order_id": "o1", "at": 50},
                          idempotency_key=new_key())
    assert_error(answer, 409, "wrong_state")
