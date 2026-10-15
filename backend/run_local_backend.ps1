$ErrorActionPreference = "Stop"

$BackendDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppDir = Join-Path $BackendDir "app"
$NltkDataDir = Join-Path $AppDir "resources\nltk_data"

Set-Location $AppDir

$env:PYTHONDONTWRITEBYTECODE = "1"
$env:ROOT_PATH = ""
$env:POSTGRES_PASSWORD = if ($env:POSTGRES_PASSWORD) { $env:POSTGRES_PASSWORD } else { "changeme" }
$env:ELASTIC_PASSWORD = if ($env:ELASTIC_PASSWORD) { $env:ELASTIC_PASSWORD } else { "changeme" }
$env:JWT_SECRET_KEY = if ($env:JWT_SECRET_KEY) { $env:JWT_SECRET_KEY } else { "change_me" }
$env:DATABASE_URL = if ($env:DATABASE_URL) { $env:DATABASE_URL } else { "postgresql://postgres:$($env:POSTGRES_PASSWORD)@localhost:5433/gsk" }
$env:ES_HOST = if ($env:ES_HOST) { $env:ES_HOST } else { "http://localhost:9201" }
$env:REDIS_HOST = if ($env:REDIS_HOST) { $env:REDIS_HOST } else { "localhost" }
$env:REDIS_PORT = if ($env:REDIS_PORT) { $env:REDIS_PORT } else { "6380" }
$env:REDIS_DB = if ($env:REDIS_DB) { $env:REDIS_DB } else { "0" }
$env:NLTK_DATA = $NltkDataDir
$env:LOCAL_BACKEND_PORT = if ($env:LOCAL_BACKEND_PORT) { $env:LOCAL_BACKEND_PORT } else { "8002" }

python run_migration.py
python -m uvicorn app_main:app --host 0.0.0.0 --port $env:LOCAL_BACKEND_PORT --reload
