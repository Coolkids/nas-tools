"""AES-GCM credentials in the business DB; independent, recoverable master key."""

import base64
import hashlib
import os
import tempfile
from pathlib import Path
from Crypto.Cipher import AES
from sqlalchemy import delete, select
from app.db.models import RECOGNITIONSECRET as SecretRow
from app.media.recognition.repository import RecognitionRepository


OPENAI_SECRET = "recognition.openai.api_key"


class SecretUnavailable(ValueError):
    pass


class SecretManager:
    def __init__(self, repository=None, root=None):
        self.repository = repository or RecognitionRepository()
        if root is None:
            from config import Config
            root = Path(Config().get_config_path())
        self.root = Path(root)

    def _key(self, create=False):
        external = os.environ.get("NASTOOL_RECOGNITION_MASTER_KEY")
        if external:
            try:
                key = base64.b64decode(external, validate=True)
            except Exception:
                raise SecretUnavailable("invalid_recognition_master_key") from None
        else:
            path = self.root / ".recognition-master.key"
            if create:
                self.root.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(prefix=".recognition-key-", dir=self.root)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(os.urandom(32))
                        stream.flush()
                        os.fsync(stream.fileno())
                    try:
                        os.link(temporary, path)
                    except FileExistsError:
                        pass
                finally:
                    os.unlink(temporary)
            try:
                if path.is_symlink() or path.stat().st_mode & 0o077:
                    raise SecretUnavailable("unsafe_recognition_master_key_permissions")
                key = path.read_bytes()
            except FileNotFoundError:
                raise SecretUnavailable("recognition_master_key_missing") from None
        if len(key) != 32:
            raise SecretUnavailable("invalid_recognition_master_key")
        return key

    def metadata(self, name=OPENAI_SECRET):
        with self.repository.transaction(lock=False) as (session, _state):
            row = session.get(SecretRow, name)
            result = {"api_key_set": row is not None, "secret_revision":
                      hashlib.sha256(row.CIPHERTEXT).hexdigest() if row is not None else "unset",
                      "credential_status": "unset" if row is None else "ready", "credential_message": None}
        if row is not None:
            try:
                self.get(name)
            except SecretUnavailable as error:
                result.update(credential_status=str(error), credential_message=
                              "请恢复原识别主密钥；无法恢复时，先清除现有 API Key 再重新配置。数据库备份不包含环境变量主密钥。")
        return result

    def set(self, value, name=OPENAI_SECRET):
        if not isinstance(value, str) or not value.strip() or len(value) > 8192 or any(x in value for x in ("\r", "\n")):
            raise ValueError("invalid_api_key")
        with self.repository.transaction(lock=False) as (session, _state):
            has_credentials = session.execute(select(SecretRow.NAME).limit(1)).first() is not None
        # A lost key must never be silently replaced while ciphertext exists.
        key = self._key(create=not has_credentials)
        cipher = AES.new(key, AES.MODE_GCM, nonce=os.urandom(12))
        cipher.update(name.encode())
        encrypted, tag = cipher.encrypt_and_digest(value.strip().encode())
        with self.repository.transaction() as (session, _state):
            row = session.get(SecretRow, name)
            if row is None:
                row = SecretRow(NAME=name)
                session.add(row)
            row.KEY_ID = hashlib.sha256(key).hexdigest()
            row.CIPHERTEXT = b"v1" + cipher.nonce + tag + encrypted
        return self.metadata(name)

    def get(self, name=OPENAI_SECRET):
        with self.repository.transaction(lock=False) as (session, _state):
            row = session.get(SecretRow, name)
            if row is None:
                return None
            wire, key_id = row.CIPHERTEXT, row.KEY_ID
        key = self._key()
        try:
            if key_id != hashlib.sha256(key).hexdigest() or wire[:2] != b"v1":
                raise ValueError
            cipher = AES.new(key, AES.MODE_GCM, nonce=wire[2:14])
            cipher.update(name.encode())
            return cipher.decrypt_and_verify(wire[30:], wire[14:30]).decode()
        except Exception:
            raise SecretUnavailable("recognition_secret_decryption_failed") from None

    def clear(self, name=OPENAI_SECRET):
        with self.repository.transaction() as (session, _state):
            session.execute(delete(SecretRow).where(SecretRow.NAME == name))
        return self.metadata(name)
