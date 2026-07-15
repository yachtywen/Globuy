"""Chapter 3: invoke the local planner tool without starting the API."""

from pprint import pprint

from app.tools.planner import planner

pprint(planner.invoke({"goal": "预算 1000 元购买适合通勤的降噪耳机"}))
