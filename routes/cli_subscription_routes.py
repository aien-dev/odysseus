"""Local vendor CLI subscription bridges (Claude, Codex, Grok).

Admin only: the CLI runs on the host with the host owner's own login, and
vendor terms bar routing one person's subscription for other users. Odysseus
never touches the CLIs' token files; login state comes from each CLI's own
status command (see ``src/cli_subscription.py``).
"""

import asyncio
import json
import logging
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request

from core.database import ModelEndpoint, SessionLocal
from core.middleware import require_admin
from src import cli_subscription
from src.auth_helpers import effective_user, get_current_user

logger = logging.getLogger(__name__)


def _owner(request: Request) -> Optional[str]:
    return effective_user(request) or get_current_user(request) or None


def _vendor_or_404(vendor: str) -> cli_subscription.Vendor:
    v = cli_subscription.VENDORS.get(vendor)
    if v is None:
        raise HTTPException(404, "Unknown CLI vendor")
    return v


def _invalidate_models_cache() -> None:
    try:
        from routes.model_routes import _invalidate_models_cache as invalidate

        invalidate()
    except Exception:
        pass


def _provision_endpoint(vendor: str, owner: Optional[str], models: list) -> Dict[str, Any]:
    """Create (or refresh) the owner's single endpoint for this vendor bridge."""
    v = cli_subscription.VENDORS[vendor]
    db = SessionLocal()
    try:
        ep = (
            db.query(ModelEndpoint)
            .filter(ModelEndpoint.base_url == v.base_url, ModelEndpoint.owner == owner)
            .order_by(ModelEndpoint.created_at)
            .first()
        )
        created = ep is None
        if ep is None:
            ep = ModelEndpoint(
                id=uuid.uuid4().hex[:8], name=cli_subscription.provider_label(v.base_url), base_url=v.base_url,
                model_type="llm", endpoint_kind="api", owner=owner,
            )
            db.add(ep)
        ep.api_key = None
        ep.is_enabled = True
        # Plain text model only: no provider-native tool schemas are ever sent.
        ep.supports_tools = False
        ep.model_type = "llm"
        ep.endpoint_kind = "api"
        ep.model_refresh_mode = "manual"
        ep.cached_models = json.dumps(models)
        db.commit()
        result = {"id": ep.id, "name": ep.name, "base_url": ep.base_url, "models": models, "created": created}
    finally:
        db.close()
    _invalidate_models_cache()
    return result


def setup_cli_subscription_routes() -> APIRouter:
    router = APIRouter(prefix="/api/cli-subscription", tags=["cli-subscription"])

    @router.get("/status")
    async def status(request: Request):
        require_admin(request)
        owner = _owner(request)
        db = SessionLocal()
        try:
            connected = {
                ep.base_url: ep.id
                for ep in db.query(ModelEndpoint).filter(ModelEndpoint.owner == owner).all()
                if cli_subscription.is_cli_subscription_base(ep.base_url)
            }
        finally:
            db.close()
        out = list(await asyncio.gather(*(cli_subscription.probe_vendor(slug) for slug in cli_subscription.VENDORS)))
        for info in out:
            info["connected"] = info["base_url"] in connected
            info["endpoint_id"] = connected.get(info["base_url"])
        return {"vendors": out}

    @router.post("/{vendor}/connect")
    async def connect(vendor: str, request: Request):
        require_admin(request)
        v = _vendor_or_404(vendor)
        if v.blocked_reason:
            raise HTTPException(409, f"{v.label} cannot be connected: {v.blocked_reason}")
        info = await cli_subscription.probe_vendor(vendor)
        if not info["installed"]:
            raise HTTPException(409, f"{v.label} CLI is not installed on this host.")
        if not info["logged_in"]:
            raise HTTPException(409, f"{v.label} CLI is not signed in. Sign in with the CLI itself on the host, then retry.")
        models = info["models"]
        if not models:
            raise HTTPException(409, f"{v.label} CLI reported no usable models.")
        return _provision_endpoint(vendor, _owner(request), models)

    @router.delete("/{vendor}")
    def disconnect(vendor: str, request: Request):
        require_admin(request)
        v = _vendor_or_404(vendor)
        owner = _owner(request)
        db = SessionLocal()
        try:
            rows = db.query(ModelEndpoint).filter(ModelEndpoint.base_url == v.base_url, ModelEndpoint.owner == owner).all()
            for row in rows:
                db.delete(row)
            db.commit()
            removed = len(rows)
        finally:
            db.close()
        _invalidate_models_cache()
        return {"removed": removed}

    return router
