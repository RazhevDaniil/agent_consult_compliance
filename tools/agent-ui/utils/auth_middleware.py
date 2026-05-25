from logging import getLogger
from typing import Optional, Dict
from flask import request

try:
    from utils.authentification import UserObject, get_user_from_jwt
except ModuleNotFoundError:
    from .utils.authentification import UserObject, get_user_from_jwt

try:
    from utils.settings import REQUIRED_ROLES
except ModuleNotFoundError:
    from .utils.settings import REQUIRED_ROLES

logger = getLogger(__name__)

USERS_CACHE: Dict[str, UserObject] = {}


def _request_path() -> str:
    try:
        return request.path
    except RuntimeError:
        return "<no-request-context>"


def get_user() -> Optional[UserObject]:
    global USERS_CACHE
    try:
        authorization = request.headers.get("Authorization") or request.headers.get("authorization")
    except RuntimeError:
        logger.debug("get_user called without request context")
        return None

    jwt_token = authorization.split()[-1] if authorization else None

    if not jwt_token:
        logger.debug("JWT token is missing path=%s", _request_path())
        return None
    if jwt_token in USERS_CACHE:
        return USERS_CACHE[jwt_token]
    try:
        user: UserObject = get_user_from_jwt(jwt_token)
    except Exception as e:
        logger.warning("JWT token validation failed path=%s error=%s", _request_path(), e, exc_info=True)
        return None
    if not any(role in user.roles for role in REQUIRED_ROLES):
        logger.warning(
            "Not allowed for current user roles path=%s login=%s",
            _request_path(),
            getattr(user, "login", None),
        )
        return None
    USERS_CACHE[jwt_token] = user
    return user
