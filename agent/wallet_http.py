"""Only wallet capability links are exposed publicly. The agent API stays private."""

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict

from agent.arkiv import ArkivError
from agent.wallet import SEPOLIA


class WalletInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    address: str = ""
    signature: str = ""
    tx_hash: str = ""
    stage: str = ""
    error_code: int = 0
    chain_id: int = SEPOLIA


def create_wallet_app(runtime):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    web = Path(__file__).parent / "wallet_web"
    mobile = runtime.wallets.mobile

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        if request.method == "POST" and request.headers.get("origin") not in {None, mobile.origin}:
            return JSONResponse({"detail": "Open the original wallet link from WhatsApp"}, status_code=403)
        response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
                "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            }
        )
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(ArkivError)
    async def unavailable(request, exc):
        return JSONResponse(
            {"detail": "Task storage is unavailable. Return to WhatsApp; do not send again."}, status_code=503
        )

    @app.get("/connect/{token}", response_class=HTMLResponse)
    @app.get("/payment/{token}", response_class=HTMLResponse)
    @app.get("/arkiv/{token}", response_class=HTMLResponse)
    async def page(token: str):
        mobile.info(token)
        return (web / "index.html").read_text()

    @app.get("/assets/mobile-wallet.js")
    async def javascript():
        return Response((web / "mobile-wallet.js").read_text(), media_type="text/javascript")

    @app.get("/health")
    async def health():
        return {
            "ok": True,
            "arkiv_ready": runtime.wallets.store.available if hasattr(runtime.wallets.store, "available") else True,
        }

    @app.get("/api/link/{token}")
    async def info(token: str):
        return mobile.info(token)

    @app.post("/api/link/{token}/challenge")
    async def challenge(token: str, body: WalletInput):
        return mobile.challenge(token, body.address, body.chain_id)

    @app.post("/api/link/{token}/progress")
    async def progress(token: str, body: WalletInput):
        return mobile.progress(token, body.stage, body.error_code)

    @app.post("/api/link/{token}/connect")
    async def connect(token: str, body: WalletInput):
        return mobile.connect(token, body.signature)

    @app.post("/api/link/{token}/claim")
    async def claim(token: str):
        return await mobile.claim(token)

    @app.post("/api/link/{token}/result")
    async def result(token: str, body: WalletInput):
        return await mobile.attach(token, body.tx_hash)

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def private(path: str):
        raise HTTPException(404)

    return app
