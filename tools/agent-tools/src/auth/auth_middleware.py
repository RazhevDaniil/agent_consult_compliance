import logging
from typing import Optional, Dict

from .authentification import UserObject, get_user_from_jwt

from ..utils.logger_config import configure_logging


configure_logging()
logger = logging.getLogger(__name__)

USERS_CACHE: Dict[str, UserObject] = {}

REQUIRED_ROLES = ['PALM_PSS_BUSINESS_SUPPORT_USER_DEPOSITS']


def get_roles_from_security(jwt_token) -> Optional[list[str]]:
    if not jwt_token:
        return None

    if jwt_token in USERS_CACHE:
        return USERS_CACHE[jwt_token].roles

    user = get_user_from_jwt(jwt_token)
    if REQUIRED_ROLES and not any(role in user.roles for role in REQUIRED_ROLES):
        return None

    USERS_CACHE[jwt_token] = user
    return user.roles
