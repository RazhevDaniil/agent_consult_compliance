import os
import json
import requests
from typing import Optional
from logging import getLogger

import jwt
from jwt.algorithms import RSAAlgorithm

from ..utils.logger_config import configure_logging

configure_logging()
logger = getLogger(__name__)


JWKS_SERVER_JWT = os.getenv(
    'PALM_SECURITY_URL_JWT',
    'http://palm-ift-techusers.delta.sbrf.ru/security/am/protocol/openid-connect/certs'
)

class JWKSProviderNotAvailableException(Exception):
    """Провайдер недоступен."""


class UserObject:
    def __init__(self, login: str, roles: list[str],
                 sid: Optional[str], ip: Optional[str], full_name: Optional[str] = None):
        self.login = login
        self.roles = roles
        self.sid = sid
        self.ip = ip
        self.full_name = full_name

    @property
    def is_authenticated(self) -> bool:
        return bool(self.login)

    @property
    def is_active(self) -> bool:
        return self.is_authenticated

    def __repr__(self) -> str:
        if self.full_name:
            return f"{self.full_name} ({self.login})"
        return self.login

    def to_dict(self):
        return self.__dict__


def get_provider_keys(jwks_server: str) -> dict:
    public_keys = {}
    try:
        jwks_keys = requests.get(jwks_server, timeout=5).json()
    except Exception as e:
        logger.error(f"Can't decode json from jwks provider {jwks_server}: {e}")
        raise JWKSProviderNotAvailableException(e)

    for jwk in jwks_keys.get('keys', []):
        kid = jwk['kid']
        public_keys[kid] = RSAAlgorithm.from_jwk(json.dumps(jwk))

    return public_keys


def get_public_key(kid: str) -> str:
    public_keys = get_provider_keys(JWKS_SERVER_JWT)
    return public_keys[kid]


def get_user_from_jwt(jwt_token: str) -> UserObject:
    jwt_user = {}
    token = jwt_token.split()[-1]
    kid = jwt.get_unverified_header(token)['kid']
    public_key = get_public_key(kid)
    try:
        user_info = jwt.decode(token, public_key, audience='PALM', algorithms='RS256')
        jwt_user['login'] = user_info['sub']
        jwt_user['roles'] = user_info['roles']
        jwt_user['sid'] = user_info['sid']
        jwt_user['ip'] = user_info.get('ip_address')  # get безопаснее
    except jwt.InvalidSignatureError as e:
        logger.exception(f"Can't decode jwt token: {e}")
        raise
    return UserObject(**jwt_user)
