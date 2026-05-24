import json
from json import JSONDecodeError
from logging import getLogger
from typing import Optional
import requests
import jwt
from jwt.algorithms import RSAAlgorithm

try:
    from utils.settings import JWKS_SERVER, JWKS_SERVER_JWT
except ModuleNotFoundError:
    from .utils.settings import JWKS_SERVER, JWKS_SERVER_JWT

logger = getLogger(__name__)


class JWKSProviderNotAvailableException(Exception):
    """Провайдер недоступен."""


def get_provider_keys(jwks_server: str) -> dict:
    public_keys = {}
    try:
        jwks_keys = requests.get(jwks_server, timeout=5).json()
        logger.info(f"jwks keys is {jwks_keys}")
    except Exception as e:
        logger.error(f"Can't decode json from jwks provider {jwks_server} {e}")
        raise JWKSProviderNotAvailableException(e)

    for jwk in jwks_keys.get('keys', []):
        kid = jwk['kid']
        public_keys[kid] = RSAAlgorithm.from_jwk(json.dumps(jwk))

    return public_keys


def get_public_key(kid: str) -> str:
    public_keys = get_provider_keys(JWKS_SERVER_JWT)
    return public_keys[kid]


def get_user_info_from_security(login: str) -> Optional[str]:
    """
    Делает запрос к сервису security для получения ФИО пользователя по логину (SberPdi).
    """
    if not login:
        logger.info("no login")
        return None

    url = f"{JWKS_SERVER}/api/v1/sudirusers"
    logger.info(f"url for security api is {url}")
    try:
        logger.info(f"login is {login}")
        response = requests.get(url, params={"SberPdi": login}, timeout=5)
        response.raise_for_status()

        data = response.json()
        logger.info(f"data from response: {data}")

        if isinstance(data, list):
            data = data[0]
        elif isinstance(data, dict):
            pass
        else:
            logger.warning(f"Failed to fetch user info for login {login}, user info: {data}")
            return None

        if 'name' in data.keys():
            name_data = data.get("name", {})
            f_name = name_data.get("familyName")
            g_name = name_data.get("givenName")
            m_name = name_data.get("middleName")

            # Собираем ФИО, отфильтровывая None значения
            parts = [p for p in [f_name, g_name, m_name] if p]

            full_name = " ".join(parts)
            return full_name if full_name else None
        else:
            logger.warning(f"No key as name")
            return None


    except Exception as e:
        # Логируем ошибку, но не роняем приложение, возвращаем None
        logger.warning(f"Failed to fetch user info for login {login}: {e}")
        return None


class UserObject:
    def __init__(self, login: str, roles: list[str],
                 # permissions: list[str],
                 sid: Optional[str], ip: Optional[str], full_name: Optional[str] = None):
        self.login = login
        self.roles = roles
        # self.permissions = permissions
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


def get_user_from_jwt(jwt_token: str) -> UserObject:
    jwt_user = {
        # 'permissions': []
    }

    kid = jwt.get_unverified_header(jwt_token)['kid']
    logger.info(f"kid: {kid}")
    public_key = get_public_key(kid)
    logger.info(f"public_key: {public_key}")

    try:
        user_info = jwt.decode(jwt_token, public_key, audience='PALM', algorithms='RS256')
        logger.info(f"user_info is {user_info}")
        jwt_user['login'] = user_info['sub']
        jwt_user['roles'] = user_info['roles']
        # jwt_user['permissions'] = user_info['permissions']
        jwt_user['sid'] = user_info['sid']
        jwt_user['ip'] = user_info.get('ip_address')  # get безопаснее
    except jwt.InvalidSignatureError as e:
        logger.exception(f"Can't decode jwt token {jwt_token} with {public_key} {e}")
        raise

    # Получаем логин, который мы только что извлекли (или взяли из TEST_USER)
    login = jwt_user.get('login')
    full_name = get_user_info_from_security(login)

    # Добавляем full_name в словарь для инициализации UserObject
    jwt_user['full_name'] = full_name

    return UserObject(**jwt_user)
