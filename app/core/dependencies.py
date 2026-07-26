"""
FastAPI 依赖注入：将单例对象（TokenManager 等）注入到路由处理函数。
"""

import os
from fastapi import Request
from app.services.token_manager import TokenManager


async def get_token_manager(request: Request) -> TokenManager:
    manager = getattr(request.app.state, "token_manager", None)
    if manager is None:
        # ASGI test transports and lightweight deployments may skip lifespan
        # events.  Lazily load the local file so those routes still expose the
        # configured account pool instead of an unexplained empty dashboard.
        manager = TokenManager(file_path=os.getenv("TOKEN_FILE", "data/accounts.json"))
        await manager.load(refresh=False)
        request.app.state.token_manager = manager
    return manager
