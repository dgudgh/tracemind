import os
import uvicorn

if __name__ == "__main__":
    port = int(os.getenv("BACKEND_PORT", "8800"))
    reload = os.getenv("UVICORN_RELOAD", "false").lower() == "true"

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port,
        reload=reload,
    )
