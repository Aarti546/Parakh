import re

from fastapi.testclient import TestClient


def test_register_login_and_me(client: TestClient) -> None:
    registered = client.post(
        "/api/v1/auth/register",
        json={"email": "person@example.com", "password": "correct-horse-42"},
    )
    assert registered.status_code == 201
    assert "password_hash" not in registered.text
    token = registered.json()["access_token"]
    assert (
        client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).json()["email"]
        == "person@example.com"
    )

    logged_in = client.post(
        "/api/v1/auth/login",
        json={"email": "person@example.com", "password": "correct-horse-42"},
    )
    assert logged_in.status_code == 200
    assert (
        client.post(
            "/api/v1/auth/login",
            json={"email": "person@example.com", "password": "wrong-password"},
        ).status_code
        == 401
    )


def test_project_flag_crud_and_environment_toggle(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    project_response = client.post(
        "/api/v1/projects",
        headers=auth_headers,
        json={"name": "Chai Corner", "slug": "chai-corner"},
    )
    assert project_response.status_code == 201
    project_id = project_response.json()["id"]
    environments = client.get(
        f"/api/v1/projects/{project_id}/environments", headers=auth_headers
    ).json()
    assert [environment["key"] for environment in environments] == ["dev", "staging", "prod"]
    dev_id = environments[0]["id"]

    flag_response = client.post(
        f"/api/v1/projects/{project_id}/flags",
        headers=auth_headers,
        json={
            "key": "new_checkout",
            "name": "New checkout",
            "value_type": "boolean",
            "default_value": False,
        },
    )
    assert flag_response.status_code == 201
    flag_id = flag_response.json()["id"]
    setting = client.patch(
        f"/api/v1/environments/{dev_id}/flags/{flag_id}",
        headers=auth_headers,
        json={"enabled": True},
    )
    assert setting.status_code == 200
    assert setting.json()["enabled"] is True
    assert (
        client.get(f"/api/v1/projects/{project_id}/flags", headers=auth_headers).json()[0]["key"]
        == "new_checkout"
    )


def test_validation_duplicate_and_project_isolation(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    project = client.post(
        "/api/v1/projects",
        headers=auth_headers,
        json={"name": "Store", "slug": "store"},
    ).json()
    invalid_flag = client.post(
        f"/api/v1/projects/{project['id']}/flags",
        headers=auth_headers,
        json={
            "key": "bad",
            "name": "Bad default",
            "value_type": "boolean",
            "default_value": "false",
        },
    )
    assert invalid_flag.status_code == 422
    flag_data = {
        "key": "banner",
        "name": "Banner",
        "value_type": "string",
        "default_value": "hello",
    }
    assert (
        client.post(
            f"/api/v1/projects/{project['id']}/flags", headers=auth_headers, json=flag_data
        ).status_code
        == 201
    )
    assert (
        client.post(
            f"/api/v1/projects/{project['id']}/flags", headers=auth_headers, json=flag_data
        ).status_code
        == 409
    )

    another_user = client.post(
        "/api/v1/auth/register",
        json={"email": "other@example.com", "password": "correct-horse-42"},
    ).json()
    response = client.get(
        f"/api/v1/projects/{project['id']}",
        headers={"Authorization": f"Bearer {another_user['access_token']}"},
    )
    assert response.status_code == 404


def test_flag_update_type_validation_and_archive(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    project = client.post(
        "/api/v1/projects",
        headers=auth_headers,
        json={"name": "Archive project", "slug": "archive-project"},
    ).json()
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
    flag_id = flag["id"]

    invalid_update = client.patch(
        f"/api/v1/flags/{flag_id}",
        headers=auth_headers,
        json={"value_type": "string"},
    )
    assert invalid_update.status_code == 422
    valid_update = client.patch(
        f"/api/v1/flags/{flag_id}",
        headers=auth_headers,
        json={"value_type": "string", "default_value": "control"},
    )
    assert valid_update.status_code == 200
    assert valid_update.json()["default_value"] == "control"

    archived = client.post(f"/api/v1/flags/{flag_id}/archive", headers=auth_headers)
    assert archived.status_code == 204
    assert client.get(f"/api/v1/projects/{project['id']}/flags", headers=auth_headers).json() == []
    archived_flags = client.get(
        f"/api/v1/projects/{project['id']}/flags?include_archived=true",
        headers=auth_headers,
    )
    assert archived_flags.status_code == 200
    assert archived_flags.json()[0]["archived_at"] is not None


def test_dashboard_csrf_register_create_project_and_toggle(
    client: TestClient,
) -> None:
    page = client.get("/register")
    assert page.status_code == 200
    csrf_token = client.cookies["parakh_csrf_token"]
    rejected = client.post(
        "/register",
        data={
            "csrf_token": "attacker-controlled-token",
            "email": "web@example.com",
            "password": "correct-horse-42",
        },
    )
    assert rejected.status_code == 403
    registered = client.post(
        "/register",
        data={
            "csrf_token": csrf_token,
            "email": "web@example.com",
            "password": "correct-horse-42",
        },
        follow_redirects=False,
    )
    assert registered.status_code == 303
    assert "parakh_access_token" in client.cookies

    csrf_token = client.cookies["parakh_csrf_token"]
    created = client.post(
        "/projects",
        data={"csrf_token": csrf_token, "name": "Web shop", "slug": "web-shop"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    detail = client.get(created.headers["location"])
    assert "Development" in detail.text
    assert "Feature flags" in detail.text

    assert (
        client.post(
            "/projects/1/flags",
            data={
                "csrf_token": csrf_token,
                "key": "order-button",
                "name": "Order button",
                "value_type": "boolean",
                "default_value": "false",
                "environment_id": "1",
            },
            follow_redirects=False,
        ).status_code
        == 303
    )
    toggled = client.post(
        "/projects/1/flags/1/toggle",
        data={"csrf_token": csrf_token, "environment_id": "1"},
        follow_redirects=False,
    )
    assert toggled.status_code == 303
    assert "Enabled" in client.get(toggled.headers["location"]).text
    settings_response = client.post(
        "/projects/1/flags/1/settings",
        headers={"HX-Request": "true"},
        data={
            "csrf_token": csrf_token,
            "environment_id": "1",
            "enabled": "true",
            "kill_switch": "false",
            "rollout_percentage": "50",
            "targeting_rules": (
                '{"operator":"AND","conditions":['
                '{"attribute":"plan","operator":"equals","value":"pro"}]}'
            ),
            "on_value": "true",
        },
    )
    assert settings_response.status_code == 200
    assert "Saved" in settings_response.text
    settings = client.get(
        "/api/v1/environments/1/flags/1",
        headers={"Authorization": f"Bearer {client.cookies.get('parakh_access_token', '')}"},
    )
    assert settings.status_code == 200
    assert settings.json()["rollout_percentage"] == 50
    assert settings.json()["kill_switch"] is False
    archived = client.post(
        "/projects/1/flags/1/archive",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert archived.status_code == 303
    assert "order-button" not in client.get(archived.headers["location"]).text
    sample_size = client.post(
        "/projects/1/sample-size",
        data={
            "csrf_token": csrf_token,
            "baseline_rate": "0.10",
            "minimum_detectable_effect": "0.02",
            "power": "0.80",
            "alpha": "0.05",
        },
    )
    assert sample_size.status_code == 200
    assert "users per variant" in sample_size.text


def test_register_page_replaces_invalid_stale_csrf_cookie(client: TestClient) -> None:
    client.cookies.set(
        "parakh_csrf_token",
        "signed-with-an-old-secret",
        domain="testserver.local",
        path="/",
    )

    page = client.get("/register")

    assert page.status_code == 200
    token_match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert token_match is not None
    form_token = token_match.group(1)
    assert form_token != "signed-with-an-old-secret"
    assert client.cookies["parakh_csrf_token"] == form_token

    registered = client.post(
        "/register",
        data={
            "csrf_token": form_token,
            "email": "fresh@example.com",
            "password": "correct-horse-42",
        },
        follow_redirects=False,
    )
    assert registered.status_code == 303


def test_health_and_unauthenticated_access(client: TestClient, monkeypatch) -> None:
    from app.services import cache
    from fakeredis import FakeRedis

    monkeypatch.setattr(cache, "redis_client", FakeRedis(decode_responses=True))
    health = client.get("/health")
    assert health.json() == {"status": "ok"}
    assert health.headers["x-request-id"]
    assert client.get("/ready").json() == {"status": "ready"}
    unauthorized = client.get("/api/v1/projects")
    assert unauthorized.status_code == 401
    assert unauthorized.json()["detail"]["code"] == "authentication_required"
    assert client.get("/static/style.css").status_code == 200
