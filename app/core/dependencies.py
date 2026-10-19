"""FastAPI dependency injection for TokenManager singleton."""

import os
from fastapi import Request
from app.services.token_manager import TokenManager, token_manager


async def get_token_manager(request: Request) -> TokenManager:
    manager = getattr(request.app.state, "token_manager", None)
    if manager is None:
        manager = token_manager
        request.app.state.token_manager = manager
    return manager
