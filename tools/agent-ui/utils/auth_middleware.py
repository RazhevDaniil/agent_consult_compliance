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

def get_user() -> Optional[UserObject]:
    global USERS_CACHE
    jwt_token = None
    try:
        jwt_token = request.headers['authorization'].split()[-1]
        if jwt_token is None:
            logger.info(f"there is no jwt_token. Try second.")
            jwt_token = request.headers['Authorization'].split()[-1]
    except (KeyError, IndexError, RuntimeError):
        pass

    if not jwt_token:
        msg = 'JWT token is missing'
        logger.exception(msg)
        return None
    if jwt_token in USERS_CACHE:
        return USERS_CACHE[jwt_token]
    try:
        user: UserObject = get_user_from_jwt(jwt_token)
    except Exception as e:
        logger.exception(e)
        return None
    if not any(role in user.roles for role in REQUIRED_ROLES):
        msg = 'Not allowed for current user roles'
        logger.exception(msg)
        return None
    USERS_CACHE[jwt_token] = user
    return user
