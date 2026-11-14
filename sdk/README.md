# Parakh Python SDK

Install the SDK from this directory:

```powershell
python -m pip install .
```

```python
from parakh_sdk import ParakhClient

client = ParakhClient(
    sdk_key="psk_from_the_dashboard",
    base_url="http://localhost:8000",
)
try:
    if client.is_enabled("new-checkout", {"id": "user-42", "plan": "pro"}):
        show_new_checkout()
    client.track("order_clicked", "user-42", value=1)
finally:
    client.close()
```

Configuration is polled in a daemon thread and cached in process memory;
individual flag checks do not make HTTP requests. If config refresh fails,
the last known config remains active. Before the first successful refresh
the caller-provided default is returned. Events are buffered and submitted in
batches by a background thread; call `close()` to request a final flush.

