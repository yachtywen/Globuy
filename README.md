# globuy

`globuy` 是一个从零构建的 AgentLoop 学习项目，当前框架链路为：

`React 调试台 -> FastAPI HTTP/WebSocket -> AgentLoop -> 工具/召回/记忆/压缩/评测`

真实模型通过 LangChain 接入 DeepSeek 的 OpenAI 兼容接口。测试使用注入式假 Agent，
不会消耗真实 API Token。外部搜索和电商数据源尚未配置，相关工具会明确返回
`not_configured`，不会编造商品和实时价格。

## 目录结构

```text
globuy/
├─ app/
│  ├─ agent/             # AgentLoop、fork、模型、工具分发与 system prompt
│  ├─ api/               # FastAPI、ContextVar、连接管理和 AG-UI 风格事件
│  ├─ tools/             # 9 个核心购物工具
│  ├─ recall/            # User / Query / Item 三塔召回与融合
│  ├─ memory/            # PreferenceEntry Store 与 Prompt 注入
│  ├─ compress/          # Cache breakpoint 与上下文压缩
│  ├─ eval/              # Rubric、Judge 与高分轨迹
│  ├─ prompt/            # YAML Prompt 配置
│  └─ utils/             # 安全路径与 Thread Context
├─ frontend/             # React + TypeScript + Vite 调试台
├─ docker/               # 容器配置说明
├─ examples/             # HTTP / WebSocket 调用示例
├─ tests/                # 后端测试
├─ output/               # 本地运行产物（不提交）
├─ uploaded/             # 用户上传文件（不提交）
├─ environment.yml       # Conda 环境定义
├─ pyproject.toml
└─ requirements.txt
```

## 本地启动

要求 Conda 与 Node.js 20.19+。推荐使用项目独立的 `globuy` Conda 环境：

```powershell
conda env create -f environment.yml
conda activate globuy
Copy-Item .env.example .env
uvicorn app.api.server:app --reload
```

环境已经存在时，只需执行 `conda activate globuy`。项目依赖发生变化后，可以运行：

```powershell
python -m pip install -e ".[dev]"
```

后端地址：<http://127.0.0.1:8000>，Swagger：<http://127.0.0.1:8000/docs>。

另开终端启动前端：

```powershell
cd frontend
npm install
npm run dev
```

前端地址：<http://127.0.0.1:5173>。

## 接入真实模型

项目使用 `ChatOpenAI` 接入 OpenAI 或兼容 OpenAI Chat Completions 的服务。修改 `.env`：

```dotenv
GLOBUY_MODEL_PROVIDER=openai-compatible
GLOBUY_LLM_MODEL=你的模型名
GLOBUY_LLM_API_KEY=你的密钥
GLOBUY_LLM_BASE_URL=https://你的兼容接口/v1
```

使用 OpenAI 官方接口时，`GLOBUY_LLM_BASE_URL` 可留空。所有项目环境变量均使用
`GLOBUY_` 前缀，以免与系统或其他项目的同名变量冲突。修改环境变量后需要重启后端。

## 接口

- `GET /healthz`：健康检查
- `POST /api/v1/chat`：一次 HTTP 对话
- `WS /api/v1/ws/{thread_id}`：实时事件对话
- `POST /api/v1/files`：上传会话文件

WebSocket 可发送纯文本，或发送：

```json
{"type":"user_message","content":"帮我比较两款耳机"}
```

服务端依次推送 `RUN_STARTED`、`TEXT_MESSAGE_START`、`TEXT_MESSAGE_CONTENT`、`TEXT_MESSAGE_END` 与 `RUN_FINISHED` 事件。

## 测试

```powershell
pytest
```

示例 01、02 需要先启动后端；其余示例可以直接运行：

```powershell
python examples/01_chat_http.py
python examples/02_chat_ws.py
python examples/03_planner_tool.py
python examples/04_compare_pipeline.py
python examples/05_three_tower_recall.py
python examples/06_long_term_memory.py
python examples/07_context_compression.py
python examples/08_evaluation.py
```

## Docker 基础服务

`docker compose up --build` 会启动 API、Redis 和 Qdrant。首版 Agent 尚未读取 Redis/Qdrant；它们是为后续长期记忆和向量召回预留的基础服务。
"# Globuy" 
