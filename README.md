# FinInsRAG

面向金融、保险文档的可解释知识问答平台。

## 启动后端

```cmd
cd backend
docker compose up -d --build
docker logs -f swxy_api
```

NLTK 的最小英文 Punkt/WordNet 资源会在 Docker 镜像构建阶段安装，无需手动下载或目录挂载。

启动前请在 `backend/.env` 中配置数据库、Elasticsearch、JWT、DeepSeek 和 DashScope 参数。真实密钥不会提交到 GitHub。

## 启动前端

```cmd
cd frontend
npm install
npm run dev
```

访问：<http://localhost:5181/>

后端 API 文档：<http://localhost:8001/docs>

## 项目结构

```text
backend/   FastAPI、文档解析、Elasticsearch 检索、DeepSeek 生成
frontend/  React + Vite 用户界面
docs/      项目学习与开发文档
```
