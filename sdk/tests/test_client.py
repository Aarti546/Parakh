import logging

import httpx
import pytest
from parakh_sdk.client import ParakhClient


def _config() -> dict:
    return {
        "flags": [
            {
                "key": "banner",
                "default_value": False,
                "enabled": True,
                "kill_switch": False,
                "rollout_percentage": 100,
                "experiment": None,
            },
            {
                "key": "color",
                "default_value": "gray",
                "enabled": True,
                "kill_switch": False,
                "rollout_percentage": 100,
                "experiment": {
                    "variants": [
                        {"key": "control", "value": "green", "traffic_percentage": 50},
                        {"key": "treatment", "value": "orange", "traffic_percentage": 50},
                    ]
                },
            },
        ]
    }


def _response(config: dict | None = None, status_code: int = 200) -> httpx.Response:
    if status_code == 304:
        return httpx.Response(304, request=httpx.Request("GET", "http://localhost/v1/config"))
    return httpx.Response(
        status_code,
        json=config or _config(),
        headers={"ETag": '"config-v1"'},
        request=httpx.Request("GET", "http://localhost/v1/config"),
    )


def test_local_checks_and_config_poll_with_etag(monkeypatch) -> None:
    requests: list[dict] = []

    def fake_get(url, *, headers, timeout):
        requests.append(headers)
        return _response()

    monkeypatch.setattr("parakh_sdk.client.httpx.get", fake_get)
    client = ParakhClient("psk_test", "http://localhost:8000/", poll_interval=60)
    client._poll_once()
    assert client.is_enabled("banner", "user-1") is True
    assert client.get_value("missing", "user-1", "safe") == "safe"
    client._poll_once()
    assert requests[-1]["If-None-Match"] == '"config-v1"'
    assert client._etag == "config-v1"
    client.close()


def test_network_failure_uses_caller_default(monkeypatch) -> None:
    def unavailable(*_args, **_kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr("parakh_sdk.client.httpx.get", unavailable)
    client = ParakhClient("psk_test", "http://localhost:8000", poll_interval=60)
    assert client.get_value("banner", "user-1", "fallback") == "fallback"
    client.close()


def test_experiment_exposure_is_tracked_once(monkeypatch) -> None:
    posted: list[dict] = []
    monkeypatch.setattr("parakh_sdk.client.httpx.get", lambda *_args, **_kwargs: _response())
    monkeypatch.setattr(
        "parakh_sdk.client.httpx.post",
        lambda url, *, json, **_kwargs: posted.append({"events": list(json["events"])})
        or httpx.Response(202, request=httpx.Request("POST", url)),
    )
    client = ParakhClient(
        "psk_test",
        "http://localhost:8000",
        poll_interval=60,
        event_batch_size=1,
        event_flush_interval=60,
    )
    client._poll_once()
    first = client.get_value("color", {"id": "user-1", "plan": "pro"})
    assert first in {"green", "orange"}
    assert client.get_value("color", {"id": "user-1", "plan": "pro"}) == first
    client.close()
    events = [event for request in posted for event in request["events"]]
    assert len(events) == 1
    assert events[0]["event_name"] == "$exposure"
    assert events[0]["flag_key"] == "color"
    assert events[0]["variant"] in {"control", "treatment"}


def test_track_batches_events_and_flushes_on_close(monkeypatch) -> None:
    posted: list[dict] = []
    monkeypatch.setattr("parakh_sdk.client.httpx.get", lambda *_args, **_kwargs: _response())

    def fake_post(_url, *, json, **_kwargs):
        posted.append({"events": list(json["events"])})
        return httpx.Response(
            202,
            request=httpx.Request("POST", "http://localhost/v1/events"),
        )

    monkeypatch.setattr("parakh_sdk.client.httpx.post", fake_post)
    client = ParakhClient(
        "psk_test",
        "http://localhost:8000",
        poll_interval=60,
        event_batch_size=10,
        event_flush_interval=60,
    )
    client.track("order_clicked", "user-1", value=1, properties={"page": "shop"})
    client.close()
    assert len(posted) == 1
    assert posted[0]["events"][0]["event_name"] == "order_clicked"
    assert posted[0]["events"][0]["properties"] == {"page": "shop"}


def test_invalid_user_and_queue_overflow_are_explicit(monkeypatch, caplog) -> None:
    monkeypatch.setattr(
        "parakh_sdk.client.httpx.get",
        lambda *_args, **_kwargs: httpx.Response(
            503, request=httpx.Request("GET", "http://localhost/v1/config")
        ),
    )
    client = ParakhClient("psk_test", "http://localhost:8000", poll_interval=60)
    with pytest.raises(ValueError):
        client.get_value("banner", {"plan": "pro"}, False)
    client.close()

    import queue

    client._events = queue.Queue(maxsize=1)
    with caplog.at_level(logging.ERROR):
        client.track("one", "user-1")
        client.track("two", "user-1")
    assert "dropping event two" in caplog.text


def test_rejects_non_positive_intervals() -> None:
    with pytest.raises(ValueError):
        ParakhClient("psk_test", "http://localhost:8000", poll_interval=0)
