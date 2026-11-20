"""Stage 3 — traffic, shifts, scheduled orders and delivery estimates. These checks ship with
the kit."""
import pytest

import fixtures as fx
from harness.http import assert_error, assert_status

pytestmark = pytest.mark.stage(3)

RUSH = [{"from": 0, "seconds_per_block": 20}, {"from": 600, "seconds_per_block": 30},
        {"from": 3600, "seconds_per_block": 20}]


@pytest.mark.town(traffic=RUSH)
def test_the_promise_uses_the_rate_at_ready_at(place):
    order = assert_status(place(at=100), 201).json()["order"]      # ready at 700, in the rush
    assert order["ready_at"] == 700 and order["promised_by"] == 700 + 30 * 10 + 900
    assert order["scheduled_for"] is None


def test_a_scheduled_order_is_ready_when_it_was_asked_for(place):
    order = assert_status(place(at=100, scheduled_for=5000), 201).json()["order"]
    assert order["scheduled_for"] == 5000 and order["ready_at"] == 5000
    assert order["promised_by"] == 5000 + 20 * 10 + 900


def test_an_order_cannot_be_scheduled_before_it_can_be_made(place):
    assert_error(place(at=100, scheduled_for=699), 422, "validation_failed")   # r1 needs 600 s
    assert_status(place(at=100, scheduled_for=700), 201)


@pytest.mark.town(shifts=((600, 7200), None))
def test_the_courier_record_shows_the_shift(town):
    listed = assert_status(town.ops.get("/couriers"), 200).json()["couriers"]
    assert [c["shift"] for c in listed] == [{"start": 600, "end": 7200}, None]


def test_a_small_day_with_traffic_and_shifts_adds_up(play):
    day = fx.day([(60 * i, "r1" if i % 2 else "r2", 100 + 3 * i, 110, 5800) for i in range(12)],
                 capacity=2, traffic=RUSH, shifts=((0, 1800), (600, 9000)))
    day["orders"][3]["scheduled_for"] = 2400
    play(day, 3)


@pytest.mark.town(shifts=((5000, 20000), (5000, 20000)))
def test_an_order_not_yet_dispatched_has_no_eta(town, place):
    order = assert_status(place(at=100), 201).json()["order"]       # nobody works before 5000
    assert order["eta"] is None
    assert assert_status(town.ops.get(f"/orders/{order['order_id']}"), 200).json()["eta"] is None
