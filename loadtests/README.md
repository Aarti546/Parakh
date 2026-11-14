# Load tests

Install `loadtests/requirements.txt`, create an SDK key in the dashboard, and
set `PARAKH_SDK_KEY` and (optionally) `PARAKH_FLAG_KEY` in the shell. Start the
stack, then run:

```powershell
locust -f loadtests/locustfile.py --host http://localhost:8000
```

In the Locust UI, start with 20 users and ramp up gradually. Record the
configuration, request rate, error rate, p50, and p95 in your results before
and after an optimization. No latency result is claimed here because Docker
was unavailable in the authoring environment and no load test was run.

