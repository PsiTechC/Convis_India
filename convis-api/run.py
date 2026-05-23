import os
import uvicorn

if __name__ == "__main__":
    reload_enabled = os.getenv("UVICORN_RELOAD", "1") == "1"
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=reload_enabled,
        reload_dirs=["app"] if reload_enabled else None,
        reload_excludes=["tests/*", "*.md", "*.html", "*.log"] if reload_enabled else None,
    )
