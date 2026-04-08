"""
认证模块
提供API认证功能
"""
from typing import Optional
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi import status as http_status

from backend.core.config import config

# 认证
_bearer = HTTPBearer(auto_error=False)

def require_auth(creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)):
    """API 鉴权（Bearer Token）"""
    token = config.api_token.strip()
    if not token:
        if not config.debug:
            raise HTTPException(
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="服务端未配置 API Token",
            )
        return {"user": "anonymous"}

    if not creds or creds.credentials != token:
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="无效或缺失 API Token",
        )
    return {"user": "authenticated"}
