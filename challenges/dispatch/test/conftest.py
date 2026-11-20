"""Track-level pytest configuration for `dispatch`."""
from __future__ import annotations

import pathlib
import sys
import time
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import fixtures as fx  # noqa: E402
import world as sim  # noqa: E402
from harness import control  # noqa: E402
from harness.http import Api, assert_status, new_key  # noqa: E402

pytest_plugins = ["harness.plugin"]


@pytest.fixture
def client(base_url):
    """Factory for API clients held to the spec's 1 s limit, closed at teardown."""
    made = []

    def _client(token=None):
        made.append(Api(base_url, token=token, timeout=sim.REQUEST_LIMIT))
        return made[-1]

    yield _client
    for one in made:
        one.close()


@pytest.fixture
def service(base_url):
    """The service as `world.World` plays against it."""
    http = sim.Http(base_url)
    yield http
    http.close()


def pytest_configure(config):
    """Register this track's marker."""
    config.addinivalue_line("markers", "town(**changes): how the `town` fixture differs from "
                            "`fixtures.town()`")


class Refused(Exception):
    """The service refused an order a check placed, as §6 allows it to."""


@pytest.hookimpl(wrapper=True)
def pytest_pyfunc_call(pyfuncitem):  # pylint: disable=unused-argument
    """A check whose order was refused has nothing left to check: it passes. Not a skip,
    which the harness never counts as passed."""
    try:
        return (yield)
    except Refused as refused:
        print(refused)
        return True


class NoTown:  # pylint: disable=too-few-public-methods
    """Stands in for a town whose reset was refused: the check fails when it first uses the
    town, as a failed check and not as a broken fixture."""

    def __init__(self, why: str):
        self.__dict__["why"] = why

    def __getattr__(self, name):
        raise AssertionError(self.__dict__["why"])


@pytest.fixture
def previous(request):
    """The preceding stage's service, as `world.World` plays against it."""
    url = request.config.getoption("--previous-base-url")
    if not url:
        pytest.fail("upgrade checks need the preceding stage: use harness run --repo --stage N, "
                    "or supply --previous-base-url")
    http = sim.Http(url)
    yield http
    http.close()


@pytest.fixture
def town(request, reset, client):
    """The small town, reset, with a client per caller. A `town` marker changes the town."""
    marker = request.node.get_closest_marker("town")
    body = fx.town(**(marker.kwargs if marker else {}))
    refused = reset(body, raw=True)
    if refused.status_code != 204:
        return NoTown(f"POST /_test/reset answered {refused.status_code} {refused.text[:200]} "
                      f"to {body}")
    return SimpleNamespace(
        body=body, ops=client(fx.OPS),
        ada=client("customer-token-1"), bob=client("customer-token-2"),
        c1=client("courier-token-1"), c2=client("courier-token-2"))


@pytest.fixture
def place(town):
    """Place an order as a customer (Ada by default). Returns the response, unless the
    service refuses the order and the check needed it: then the check ends there and passes
    (see `Refused`). `refusable=True` returns a refusal like any other response."""
    def _place(who=None, *, at, restaurant_id="r1", x=110, y=100, price=8400, key=None,
               refusable=False, **extra):
        body = {"restaurant_id": restaurant_id, "dropoff": {"x": x, "y": y}, "price": price,
                "at": at, **extra}
        answer = (who or town.ada).post("/orders", json=body,
                                        idempotency_key=new_key() if key is None else key)
        if not refusable and answer.status_code == 409 and "no_capacity" in answer.text:
            raise Refused(f"the service refused the order, as §6 allows: {body}")
        return answer
    return _place


@pytest.fixture
def tick(town):
    """Send a tick as ops. Returns the response."""
    return lambda at, key=None: town.ops.post(
        "/ticks", json={"at": at}, idempotency_key=new_key() if key is None else key)


@pytest.fixture
def report(town):
    """Send a courier report. Returns the response."""
    def _report(courier_id, event, order_id, at, *, who=None, key=None):
        sender = who or getattr(town, courier_id)
        named = {} if order_id is None else {"order_id": order_id}
        return sender.post(f"/couriers/{courier_id}/reports",
                           json={"event": event, **named, "at": at},
                           idempotency_key=new_key() if key is None else key)
    return _report


@pytest.fixture
def dispatched(place, tick):
    """Place one order at `at` and tick until the service dispatches it. Returns
    `(order, courier_id, at)`: the order as first returned, and who got it when.

    The service chooses the moment, but invariant 4 bounds it: with a courier idle, the
    order cannot stay undispatched past its `promised_by`."""
    def _dispatched(at=100, **order):
        body = assert_status(place(at=at, **order), 201).json()
        found, now = body["dispatches"], at
        while not found:
            now = (now // sim.TICK + 1) * sim.TICK
            assert now <= body["order"]["promised_by"] + sim.TICK, \
                "a courier is idle and the order is still undispatched after its promised_by"
            found = assert_status(tick(now), 200).json()["dispatches"]
        assert len(found) == 1 and found[0]["order_id"] == body["order"]["order_id"], found
        return body["order"], found[0]["courier_id"], now
    return _dispatched


@pytest.fixture
def carried(dispatched, report):
    """One order dispatched and picked up as soon as it is ready. Returns
    `(order, courier_id, picked_at)`."""
    def _carried(at=100, **order):
        placed, courier_id, now = dispatched(at=at, **order)
        picked = max(now, placed["ready_at"])
        assert_status(report(courier_id, "picked_up", placed["order_id"], picked), 200)
        return placed, courier_id, picked
    return _carried

@pytest.fixture
def restart(request, base_url):
    """Kill the service's container and wait until it answers again."""
    def _restart():
        folder = request.config.getoption("--control-dir")
        if not folder:
            pytest.fail("this check restarts the service: run it through `python -m harness`")
        control.request(folder)
        deadline = time.monotonic() + 60      # spec §2: start to first healthy response
        while True:
            try:
                if httpx.get(f"{base_url}/health", timeout=5).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            assert time.monotonic() < deadline, "no /health 60 s after a restart"
            time.sleep(0.25)
    return _restart


@pytest.fixture
def play(service):
    """Play a day against the service at a stage, comparing its records with the day every
    `every` events and at the end. Returns the finished `world.World`."""
    def _play(day, stage, every=0):
        played, steps = sim.World(day, service, stage), 0
        while played.step():
            steps += 1
            if every and steps % every == 0:
                sim.audit(played)
        sim.audit(played)
        return played
    return _play


@pytest.fixture
def upgraded(previous, service):
    """Play the first `stop` events of a day against the preceding stage's service, then
    move its export into this one. Returns the `world.World`, now playing against this
    service at `stage`."""
    def _upgraded(day, stage, stop):
        played = sim.World(day, previous, stage - 1)
        for _ in range(stop):
            played.step()
        document = assert_status(previous.client.get("/_test/export", timeout=10), 200).json()
        assert_status(service.client.post("/_test/import", json=document, timeout=10), 204)
        played.service, played.stage = service, stage
        return played
    return _upgraded


@pytest.fixture
def console(page, tid):
    """Open the console with a token and wait for it to show the day or an error."""
    def _console(token):
        page.goto("/console")
        page.fill(tid("console-token"), token)
        page.click(tid("console-open"))
        either = f"{tid('summary-at')}:visible, {tid('console-error')}:visible"
        page.locator(either).first.wait_for()
        return page
    return _console
