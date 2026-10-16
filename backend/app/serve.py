"""Launch the single-worker HOST with configurable, private network binding."""

import uvicorn

from backend.app.config import load_server_settings


def main():
    settings = load_server_settings()
    uvicorn.run("backend.app.main:create_app", factory=True,
                host=settings.host, port=settings.port, workers=1)


if __name__ == "__main__":
    main()
