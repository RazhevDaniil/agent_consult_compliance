import os


JWKS_SERVER = os.getenv(
    'PALM_SECURITY_URL',
    "http://palm-ift-techusers.delta.sbrf.ru/security/im"
)
JWKS_SERVER_JWT = os.getenv(
    'PALM_SECURITY_URL_JWT',
    'http://palm-ift-techusers.delta.sbrf.ru/security/am/protocol/openid-connect/certs'
)
REQUIRED_ROLES = ['PALM_PSS_BUSINESS_SUPPORT_USER_DEPOSITS']
