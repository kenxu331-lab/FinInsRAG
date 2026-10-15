from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from router import chat_rt
from router import user_rt
from router import history_rt
import os
import redis
from fastapi.responses import JSONResponse
from sqlalchemy import text

from config import settings, validate_runtime_config
from utils.database import engine

# 从环境变量获取 root_path
root_path = os.getenv("ROOT_PATH", "")

app = FastAPI(root_path=root_path)

# 添加 CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许所有源，生产环境中应该设置具体的源
    allow_credentials=True,
    allow_methods=["*"],  # 允许所有方法
    allow_headers=["*"],  # 允许所有头
)

app.include_router(chat_rt.router)
app.include_router(user_rt.router)
app.include_router(history_rt.router)


@app.on_event("startup")
def validate_configuration() -> None:
    validate_runtime_config()


@app.get("/health", include_in_schema=False)
def health():
    """Report dependency readiness without exposing connection details."""

    checks = {"postgres": False, "elasticsearch": False, "redis": False}
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        checks["postgres"] = True
    except Exception:
        pass

    try:
        from service.core.rag.utils.es_conn import ESConnection

        checks["elasticsearch"] = bool(ESConnection().es.ping())
    except Exception:
        pass

    try:
        client = redis.Redis(
            host=os.getenv("REDIS_HOST", "redis"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=int(os.getenv("REDIS_DB", "0")),
        )
        checks["redis"] = bool(client.ping())
    except Exception:
        pass

    healthy = all(checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "degraded", "checks": checks},
    )

if __name__=='__main__':
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

