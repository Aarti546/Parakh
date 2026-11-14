import os

from locust import HttpUser, between, task


class ParakhSDKUser(HttpUser):
    wait_time = between(0.01, 0.1)

    def on_start(self) -> None:
        self.sdk_key = os.environ.get("PARAKH_SDK_KEY", "")
        self.headers = {"Authorization": f"Bearer {self.sdk_key}"}
        self.user_id = f"locust-{id(self)}"

    @task(4)
    def fetch_config(self) -> None:
        self.client.get("/v1/config", headers=self.headers, name="/v1/config")

    @task(6)
    def evaluate_flag(self) -> None:
        self.client.post(
            "/v1/evaluate",
            headers=self.headers,
            json={
                "flag_key": os.environ.get("PARAKH_FLAG_KEY", "order-button-color"),
                "user_id": self.user_id,
                "attributes": {"plan": "pro", "country": "IN"},
            },
            name="/v1/evaluate",
        )
