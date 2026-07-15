import { FormEvent, useEffect, useRef, useState } from "react";

type Message = { role: "user" | "assistant" | "system"; content: string };
type AgentEvent = {
  type: string;
  data: { delta?: string; message?: string };
};

const newThreadId = () => `web-${crypto.randomUUID()}`;

export default function App() {
  const threadId = useRef(newThreadId());
  const socket = useRef<WebSocket | null>(null);
  const [connected, setConnected] = useState(false);
  const [running, setRunning] = useState(false);
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<Message[]>([
    { role: "system", content: "当前是 mock Agent，可先验证完整通信链路。" },
  ]);

  useEffect(() => {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${scheme}://${location.host}/api/v1/ws/${threadId.current}`);
    socket.current = ws;
    ws.onopen = () => setConnected(true);
    ws.onclose = () => setConnected(false);
    ws.onmessage = (message) => {
      const event = JSON.parse(message.data) as AgentEvent;
      if (event.type === "TEXT_MESSAGE_CONTENT" && event.data.delta) {
        setMessages((items) => [...items, { role: "assistant", content: event.data.delta! }]);
      }
      if (event.type === "RUN_FINISHED" || event.type === "RUN_ERROR") {
        setRunning(false);
      }
      if (event.type === "RUN_ERROR" && event.data.message) {
        setMessages((items) => [...items, { role: "system", content: event.data.message! }]);
      }
    };
    return () => ws.close();
  }, []);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const content = input.trim();
    if (!content || socket.current?.readyState !== WebSocket.OPEN) return;
    setMessages((items) => [...items, { role: "user", content }]);
    socket.current.send(JSON.stringify({ type: "user_message", content }));
    setInput("");
    setRunning(true);
  };

  return (
    <main>
      <header>
        <div>
          <p className="eyebrow">AGENTLOOP LEARNING PROJECT</p>
          <h1>globuy</h1>
          <p>购物 Agent 实时调试台</p>
        </div>
        <span className={connected ? "status online" : "status"}>
          {connected ? "WebSocket 已连接" : "连接中"}
        </span>
      </header>

      <section className="conversation">
        {messages.map((message, index) => (
          <article className={message.role} key={`${message.role}-${index}`}>
            <strong>{message.role === "user" ? "你" : message.role === "assistant" ? "globuy" : "系统"}</strong>
            <p>{message.content}</p>
          </article>
        ))}
      </section>

      <form onSubmit={submit}>
        <textarea
          value={input}
          onChange={(event) => setInput(event.target.value)}
          placeholder="例如：帮我制定一份机械键盘选购计划"
          rows={3}
        />
        <button disabled={!connected || running || !input.trim()}>
          {running ? "Agent 运行中…" : "发送"}
        </button>
      </form>
    </main>
  );
}

