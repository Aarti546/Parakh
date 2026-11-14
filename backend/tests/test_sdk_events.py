from datetime import UTC, datetime

from app.services import cache
from app.services.evaluation import evaluate_flag
from fakeredis import FakeRedis
from fastapi.testclient import TestClient


def test_sdk_key_config_etag_evaluation_and_event_ingestion(
    client: TestClient,
    auth_headers: dict[str, str],
    monkeypatch,
) -> None:
    fake_redis = FakeRedis(decode_responses=True)
    monkeypatch.setattr(cache, "redis_client", fake_redis)
    project = client.post(
        "/api/v1/projects",
        headers=auth_headers,
        json={"name": "SDK test", "slug": "sdk-test"},
    ).json()
    environment = client.get(
        f"/api/v1/projects/{project['id']}/environments", headers=auth_headers
    ).json()[0]
    flag = client.post(
        f"/api/v1/projects/{project['id']}/flags",
        headers=auth_headers,
        json={
            "key": "checkout",
            "name": "Checkout",
            "value_type": "boolean",
            "default_value": False,
        },
    ).json()
    enabled = client.patch(
        f"/api/v1/environments/{environment['id']}/flags/{flag['id']}",
        headers=auth_headers,
        json={"enabled": True, "rollout_percentage": 100},
    )
    assert enabled.status_code == 200
    created_key = client.post(
        f"/api/v1/environments/{environment['id']}/sdk-keys",
        headers=auth_headers,
        json={"name": "test SDK"},
    )
    assert created_key.status_code == 201
    sdk_key = created_key.json()["sdk_key"]
    sdk_headers = {"Authorization": f"Bearer {sdk_key}"}

    config = client.get("/v1/config", headers=sdk_headers)
    assert config.status_code == 200
    assert config.json()["flags"][0]["key"] == "checkout"
    unchanged = client.get(
        "/v1/config",
        headers={**sdk_headers, "If-None-Match": config.headers["etag"]},
    )
    assert unchanged.status_code == 304

    evaluated = client.post(
        "/v1/evaluate",
        headers=sdk_headers,
        json={"flag_key": "checkout", "user_id": "test-user"},
    )
    assert evaluated.status_code == 200
    assert evaluated.json()["value"] is True
    assert (
        client.post(
            "/v1/evaluate/all",
            headers=sdk_headers,
            json={"user_id": "test-user"},
        ).json()["flags"]["checkout"]["reason"]
        == "ROLLOUT"
    )
    experiment = client.post(
        f"/api/v1/projects/{project['id']}/experiments",
        headers=auth_headers,
        json={
            "key": "checkout-color",
            "flag_id": flag["id"],
            "environment_id": environment["id"],
            "primary_metric": "order_clicked",
            "variants": [
                {"key": "control", "value": False, "traffic_percentage": 50},
                {"key": "treatment", "value": True, "traffic_percentage": 50},
            ],
        },
    )
    assert experiment.status_code == 201
    experiment_config = client.get("/v1/config", headers=sdk_headers).json()["flags"][0]
    experiment_result = evaluate_flag(experiment_config, "same-user")
    assert experiment_result["variant"] in {"control", "treatment"}
    assert experiment_result["value"] is (experiment_result["variant"] == "treatment")
    assert client.get(
        f"/api/v1/projects/{project['id']}/audit-log",
        headers=auth_headers,
    ).json()
    assert (
        client.post(
            f"/api/v1/experiments/{experiment.json()['id']}/stop",
            headers=auth_headers,
        ).json()["status"]
        == "completed"
    )

    event_time = datetime.now(UTC).isoformat()
    events = client.post(
        "/v1/events",
        headers=sdk_headers,
        json={
            "events": [
                {
                    "event_id": "9a7ea3d7-ec2e-4910-beb2-3d35dcdd5cd1",
                    "event_name": "order_clicked",
                    "user_id": "test-user",
                    "created_at": event_time,
                }
            ]
        },
    )
    assert events.status_code == 202
    assert events.json() == {"accepted": 1}
    assert fake_redis.xlen(f"parakh:events:{environment['id']}") == 1
    config_response = client.get("/v1/config", headers=sdk_headers)
    config_response2 = client.get(
        "/v1/config",
        headers={**sdk_headers, "If-None-Match": config_response.headers["etag"]},
    )
    assert config_response2.status_code == 304

    assert (
        client.delete(
            f"/api/v1/sdk-keys/{created_key.json()['id']}", headers=auth_headers
        ).status_code
        == 204
    )
    assert client.get("/v1/config", headers=sdk_headers).status_code == 401
