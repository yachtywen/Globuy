# Docker 说明

根目录的 `compose.yaml` 定义 API、Redis 与 OpenSearch：

- OpenSearch 是固定的 BaseStore/分类语义应用层，数据卷为 `opensearch-data`。
- Faiss 在宿主机 Conda `globuy` 环境中运行，负责 HNSW/IP 候选召回，不单独启动容器。
- Redis 仅为后续普通缓存预留，不承担向量数据库或长期记忆默认存储。

本地启动基础设施：

```powershell
docker compose up -d opensearch redis
docker compose ps
```

开发 compose 关闭了 OpenSearch Security 插件，只允许本机开发使用，不应直接暴露到公网。
