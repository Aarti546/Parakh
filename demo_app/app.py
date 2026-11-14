import os
import uuid
from contextlib import asynccontextmanager
from html import escape

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from parakh_sdk import ParakhClient

client = ParakhClient(
    sdk_key=os.getenv("PARAKH_SDK_KEY", ""),
    base_url=os.getenv("PARAKH_BASE_URL", "http://localhost:8000"),
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    client.close()


app = FastAPI(title="Chai Corner", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
def storefront(request: Request) -> str:
    user_id = request.cookies.get("chai_user") or str(uuid.uuid4())
    user_id = escape(user_id, quote=True)
    button_color = client.get_value("order-button-color", user_id, "green")
    color = "#f97316" if button_color == "orange" else "#168a58"
    html = f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Chai Corner</title>
<style>
body{{font:16px system-ui;background:#fff8ed;color:#392718;display:grid;
place-items:center;min-height:90vh}}
main{{text-align:center;background:white;padding:3rem;border-radius:20px;
box-shadow:0 12px 40px #39271818}}
button{{background:{color};color:white;border:0;border-radius:10px;
padding:14px 28px;font-size:18px;cursor:pointer}}</style>
<main><p>☕ CHAI CORNER</p><h1>A little warmth in every cup.</h1>
<p>Freshly brewed chai, delivered to your door.</p>
<form method="post" action="/order"><input type="hidden" name="user_id" value="{user_id}">
<button>Order Now</button></form></main></html>"""
    response = HTMLResponse(html)
    response.set_cookie("chai_user", user_id, httponly=True, samesite="lax")
    return response


@app.post("/order", response_class=HTMLResponse)
async def order(request: Request) -> HTMLResponse:
    form = await request.form()
    user_id = str(form.get("user_id", "anonymous"))
    client.track("order_clicked", user_id, value=1)
    return HTMLResponse(
        "<h1>Thanks! Your chai order is on its way.</h1><a href='/'>Back to shop</a>"
    )
