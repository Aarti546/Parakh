"""Generate deterministic, realistic-ish experiment traffic via the SDK."""

import os
import random

from parakh_sdk import ParakhClient


def main() -> None:
    randomizer = random.Random(2026)
    client = ParakhClient(
        os.environ["PARAKH_SDK_KEY"],
        os.getenv("PARAKH_BASE_URL", "http://localhost:8000"),
        event_batch_size=100,
        event_flush_interval=0.25,
    )
    try:
        client._poll_once()
        for user_number in range(5_000):
            user_id = f"sim-user-{user_number}"
            variant_value = client.get_value("order-button-color", user_id, "green")
            conversion_rate = 0.12 if variant_value == "green" else 0.15
            if randomizer.random() < conversion_rate:
                client.track(
                    "order_clicked",
                    user_id,
                    value=1,
                    properties={"variant": variant_value},
                )
    finally:
        client.close(timeout=30)


if __name__ == "__main__":
    main()
