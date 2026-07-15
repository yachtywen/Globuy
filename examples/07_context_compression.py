"""Chapter 7: calculate a breakpoint and compress old messages."""

from langchain_core.messages import HumanMessage

from app.compress import compress_messages

messages = [HumanMessage(content=f"第 {index} 轮：" + "历史购物信息" * 20) for index in range(12)]
result, retained = compress_messages(messages, token_limit=100, keep_recent=4)
print(result.model_dump())
print(result.summary)
print("retained messages:", len(retained))
