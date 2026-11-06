"""Link the dedicated agent number through Ira's existing sidecar API."""

import re
from urllib.parse import quote

import httpx


async def setup(config, *, phone=None, status_only=False, transport=None):
    env = config.env
    line = env.get("AGENT_WHATSAPP_LINE") or ""
    url = env.get("AGENT_WHATSAPP_SIDECAR_URL") or ""
    secret = env.get("AGENT_WHATSAPP_SECRET") or ""
    if not re.fullmatch(r"line-[A-Za-z0-9_-]+", line) or not url or not secret:
        raise ValueError("Configure the dedicated WhatsApp sidecar URL, secret and line first")
    async with httpx.AsyncClient(
        base_url=url, headers={"Authorization": f"Bearer {secret}"}, timeout=90, transport=transport
    ) as client:
        try:
            current = await client.get(f"/status/{quote(line, safe='')}")
            current.raise_for_status()
            data = current.json()
            public = {k: data[k] for k in ("linked", "connected", "state", "phone", "since") if k in data}
            if status_only:
                return {"line": line, **public}
            phone = phone or env.get("AGENT_WHATSAPP_PHONE") or ""
            phone = re.sub(r"[\s()-]", "", phone)
            if not re.fullmatch(r"\+[1-9]\d{7,14}", phone):
                raise ValueError("Use the agent phone number with its international country code")
            if data.get("linked") or data.get("connected"):
                if data.get("phone") != phone:
                    raise ValueError("This line is linked to a different number; no existing link was changed")
                return {"line": line, **public, "already_linked": True}
            response = await client.post(f"/link/{quote(line, safe='')}", json={"phone": phone})
            if not response.is_success:
                kind = response.json().get("kind")
                allowed = {"already_linked", "bad_phone", "not_on_whatsapp", "timeout", "pair_refused"}
                raise ValueError(
                    f"WhatsApp linking failed: {kind if kind in allowed else 'sidecar refused the request'}"
                )
            code = response.json().get("code")
            if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9-]{8,12}", code):
                raise ValueError("WhatsApp did not return a pairing code")
            return {
                "line": line,
                "phone": phone,
                "code": code,
                "instructions": "On the agent phone: WhatsApp > Linked devices > Link a device > "
                "Link with phone number instead. Enter this code, then check whatsapp-status.",
            }
        except (httpx.HTTPError, KeyError, TypeError):
            raise ValueError(
                "WhatsApp sidecar unavailable. Check the local containers and their private configuration."
            ) from None
