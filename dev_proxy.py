"""
Local dev proxy for infra-brain dashboard UI editing.

- Serves local files from src/infra_brain/dashboard/static/ instantly
  (edit index.html → F5 → see the change, no push/deploy needed)
- Proxies all /api/* requests to the live backend on deploy-host-01:8001

Usage:
    python dev_proxy.py [--backend http://deploy-host-01:8001] [--port 8080]
"""

import argparse
import mimetypes
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--backend", default="http://deploy-host-01:8001")
parser.add_argument("--port", type=int, default=8080)
args, _ = parser.parse_known_args()

BACKEND = args.backend.rstrip("/")
STATIC_DIR = Path(__file__).parent / "src" / "infra_brain" / "dashboard" / "static"

app = FastAPI()


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_api(request: Request, path: str):
    url = f"{BACKEND}/api/{path}"
    if request.query_params:
        url += "?" + str(request.query_params)

    async with httpx.AsyncClient(verify=False, timeout=60.0) as client:
        proxy_req = client.build_request(
            method=request.method,
            url=url,
            headers={k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")},
            content=await request.body(),
        )
        resp = await client.send(proxy_req, stream=True)

    return StreamingResponse(
        resp.aiter_bytes(),
        status_code=resp.status_code,
        headers=dict(resp.headers),
        media_type=resp.headers.get("content-type"),
    )


@app.get("/dashboard")
@app.get("/dashboard/")
async def dashboard():
    index = STATIC_DIR / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>index.html not found</h1><p>Expected: " + str(index) + "</p>", status_code=404)


@app.get("/dashboard/static/{path:path}")
async def static_files(path: str):
    file = STATIC_DIR / path
    if file.exists() and file.is_file():
        mime, _ = mimetypes.guess_type(str(file))
        return FileResponse(str(file), media_type=mime or "application/octet-stream")
    # Fall back to remote for anything not found locally
    async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
        resp = await client.get(f"{BACKEND}/dashboard/static/{path}")
    return Response(content=resp.content, status_code=resp.status_code,
                    media_type=resp.headers.get("content-type"))


@app.get("/")
async def root():
    return HTMLResponse('<meta http-equiv="refresh" content="0;url=/dashboard">')


if __name__ == "__main__":
    print(f"Dev proxy -> backend: {BACKEND}")
    print(f"Open: http://localhost:{args.port}/dashboard")
    print(f"Serving local files from: {STATIC_DIR}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
