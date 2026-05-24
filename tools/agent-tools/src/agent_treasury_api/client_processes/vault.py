import hmac
import hashlib
from typing import List, Optional

from .clients_finorg_subjects import SUBJECTS, FINORGS


class INNVault:
    """
    Хранит HMAC-хэши ИНН. Проверяет входящий ИНН за O(n).
    """

    def __init__(
            self,
            hashed_inns: List[str],
            # secret_key: Optional[bytes] = None
    ):
        """
        Args:
            hashed_inns: список HMAC-хэшей ИНН (hex-строки)
        """
        self._hashes = set(hashed_inns)
        self._key = b"5834325f748c4eb9d38f347cb30b57d4decc1c154de325fb05410bbd34d2adb4" # secret_key or self._load_key()

    @staticmethod
    def _load_key() -> bytes:
        key = b"5834325f748c4eb9d38f347cb30b57d4decc1c154de325fb05410bbd34d2adb4" # settings.inn_secret_key
        if not key:
            raise EnvironmentError(
                "Переменная окружения DEAL_INN_SECRET_KEY не задана. "
                "Сгенерируйте ключ: python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        return key

    @staticmethod
    def _compute_hmac(inn: str, key: bytes) -> str:
        """HMAC-SHA256 от нормализованного ИНН."""
        inn_normalized = inn.strip()
        return hmac.new(key, inn_normalized.encode(), hashlib.sha256).hexdigest()

    def check(self, inn: str) -> bool:
        """Проверяет, есть ли ИНН в списке."""
        h = self._compute_hmac(inn, self._key)
        return h in self._hashes

    @classmethod
    def generate_hashes(cls, inn_list: List[str], secret_key: Optional[bytes] = None) -> List[str]:
        """
        Генерирует HMAC-хэши для списка ИНН.
        Результат можно вставить в код как константу.
        """
        key = secret_key or cls._load_key()
        return [cls._compute_hmac(inn, key) for inn in inn_list]


vault_finorgs = INNVault(FINORGS)
vault_subjects = INNVault(SUBJECTS)
