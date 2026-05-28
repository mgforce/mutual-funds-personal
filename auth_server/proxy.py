"""
Reverse-proxy authenticated requests through to the Streamlit dashboard.

The proxy is intentionally dumb: forward everything (HTTP + WebSocket) to
upstream Streamlit, but inject an ``X-Session-Token`` header so the script
can identify who the user is. Streamlit itself never sees the auth cookie.

Only authenticated requests reach this — the FastAPI router in ``main.py``
gates the catch-all route on ``session_from_request``.
"""
from __future__ import annotations

import asyncio
import os

import httpx
from fastapi import Request, Response, WebSocket

STREAMLIT_URL = os.environ.get("STREAMLIT_URL", "http://127.0.0.1:8501")
SESSION_HEADER = "X-Session-Payload"

# httpx client tuned for proxying: long timeouts, no automatic redirects
# (we want Streamlit's redirects to reach the browser as-is).
_HTTP_CLIENT = httpx.AsyncClient(
    base_url=STREAMLIT_URL,
    timeout=httpx.Timeout(300.0, connect=10.0),
    follow_redirects=False,
)


# Hop-by-hop headers per RFC 7230 — must not be forwarded through a proxy.
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}


def _drop_hop_by_hop(headers: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


async def proxy_http(request: Request, payload: str) -> Response:
    """Forward a normal HTTP request to Streamlit and stream the response back."""
    upstream_headers = _drop_hop_by_hop(dict(request.headers))
    upstream_headers[SESSION_HEADER] = payload
    upstream_headers.pop("host", None)  # let httpx set the correct host

    body = await request.body()
    upstream = await _HTTP_CLIENT.request(
        method=request.method,
        url=request.url.path + (f"?{request.url.query}" if request.url.query else ""),
        headers=upstream_headers,
        content=body,
    )

    response_headers = _drop_hop_by_hop(dict(upstream.headers))
    # Strip any Set-Cookie from Streamlit — we don't want it leaking through.
    response_headers.pop("set-cookie", None)
    # httpx auto-decompresses gzip/br/deflate responses, so the body we got
    # is already plain bytes. If we forward Content-Encoding from upstream
    # the browser tries to re-decompress and fails with
    # ERR_CONTENT_DECODING_FAILED. Same for Content-Length — the byte count
    # changed during decompression. Drop both and let Starlette set Content-
    # Length correctly from our actual payload.
    response_headers.pop("content-encoding", None)
    response_headers.pop("content-length", None)
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )


async def proxy_websocket(websocket: WebSocket, payload: str) -> None:
    """Forward Streamlit's WebSocket traffic (_stcore/stream) bidirectionally.
    Streamlit needs WS for live reruns — without this, the dashboard hangs
    on a 'Connecting…' spinner.

    Subprotocols are negotiated end-to-end: we ask upstream for whichever
    the client wanted, then accept() the client with the protocol upstream
    confirmed. Streamlit's frontend won't talk to a WS that doesn't echo
    the same subprotocol, so getting this wrong = blank dashboard.
    """
    import websockets  # local: only needed when WS endpoint is hit

    upstream_url = (
        STREAMLIT_URL.replace("http://", "ws://").replace("https://", "wss://")
        + websocket.url.path
        + (f"?{websocket.url.query}" if websocket.url.query else "")
    )
    requested_subprotocols = websocket.scope.get("subprotocols") or []

    # Forward client headers (notably Cookie) so st.context.cookies and
    # st.context.headers see the same values the dashboard would see for an
    # HTTP request. Skip hop-by-hop and websocket-control headers, which the
    # underlying websockets library sets itself.
    _WS_RESERVED = {"host", "upgrade", "connection", "origin"}
    forwarded_headers: list[tuple[str, str]] = []
    for k, v in websocket.headers.items():
        lk = k.lower()
        if lk in _HOP_BY_HOP or lk in _WS_RESERVED or lk.startswith("sec-websocket"):
            continue
        forwarded_headers.append((k, v))
    forwarded_headers.append((SESSION_HEADER, payload))

    try:
        upstream = await websockets.connect(
            upstream_url,
            additional_headers=forwarded_headers,
            subprotocols=requested_subprotocols or None,
        )
    except Exception as e:
        print(f"[proxy] upstream WS connect failed: {type(e).__name__}: {e}")
        await websocket.close(code=1011)
        return

    await websocket.accept(subprotocol=upstream.subprotocol)
    try:
        await asyncio.gather(
            _ws_pipe_client_to_upstream(websocket, upstream),
            _ws_pipe_upstream_to_client(upstream, websocket),
        )
    finally:
        await upstream.close()


async def _ws_pipe_client_to_upstream(client: WebSocket, upstream) -> None:
    try:
        while True:
            msg = await client.receive()
            if msg["type"] == "websocket.disconnect":
                await upstream.close()
                return
            if "bytes" in msg and msg["bytes"] is not None:
                await upstream.send(msg["bytes"])
            elif "text" in msg and msg["text"] is not None:
                await upstream.send(msg["text"])
    except Exception:
        await upstream.close()


async def _ws_pipe_upstream_to_client(upstream, client: WebSocket) -> None:
    try:
        async for msg in upstream:
            if isinstance(msg, bytes):
                await client.send_bytes(msg)
            else:
                await client.send_text(msg)
    except Exception:
        try:
            await client.close()
        except Exception:
            pass
