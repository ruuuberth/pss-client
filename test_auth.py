"""Smoke test end-to-end del backend de auth de pss_client.

Valida el PASO 1 del flujo de login (DeviceLogin17 con deviceKey desechable)
contra la API real — sin credenciales del usuario. El flujo completo
(email+password) se prueba desde el frontend con las credenciales reales.

Uso:
    cd /home/naowh/Work/pss-client && uv run python test_auth.py
"""

import asyncio
import sys
from datetime import datetime, timezone
from uuid import uuid4

sys.path.insert(0, ".")

from pss_client.auth import CHECKSUM_KEY, SAVY_CHECKSUM, PssAuth, create_checksum
from pss_client.transport import PssApiError


async def main() -> None:
    # 1) Test unitario de la fórmula del checksum
    dk = str(uuid4())
    now = datetime.now(timezone.utc)
    checksum = create_checksum(dk, now, "DeviceTypeAndroid")
    assert len(checksum) == 32, checksum
    print(f"[unit] create_checksum OK -> {checksum}")

    # 2) Test del PASO 1: DeviceLogin17 con deviceKey desechable
    #    (debe devolver un UserLogin de cuenta web temporal)
    async with PssAuth() as auth:
        try:
            node = await auth._device_login_raw(dk)
            user_id = node.attrib.get("UserId")
            token = node.attrib.get("accessToken", "")
            assert user_id and token, dict(node.attrib)
            print(f"[paso1] DeviceLogin17 OK -> UserId={user_id} token={token[:8]}...")
        except PssApiError as e:
            print(f"[paso1] FALLO: {e}")
            raise

    print("\nBackend de autenticación verificado (paso 1 del flujo email+password).")
    print(f"  CHECKSUM_KEY  = {CHECKSUM_KEY!r}")
    print(f"  SAVY_CHECKSUM = {SAVY_CHECKSUM!r}")


if __name__ == "__main__":
    asyncio.run(main())
