"""Run after starting the API with uvicorn."""

import json
from urllib.request import Request, urlopen

request = Request(
    "http://127.0.0.1:8000/api/v1/chat",
    data=json.dumps({"message": "帮我制定一份机械键盘选购计划"}).encode(),
    headers={"Content-Type": "application/json"},
)
with urlopen(request) as response:  # noqa: S310 - fixed local development URL
    print(response.read().decode())
