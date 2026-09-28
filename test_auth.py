"""Smoke test end-to-end del backend de auth de pss_client.

- Verifica la fórmula del checksum contra DeviceLogin17 (API real).
- No usa credenciales del usuario: device_key aleatorio (cuenta desechable)
  para validar el handshake; el login a la cuenta real se hace con el
  device_key propio del usuario.

Uso:
    cd /home/naowh/Work/pss-client && python test_auth.py
"""

import asyncio
import sys
from datetime import datetime, timezone
from uuid import uuid4

sys.path.insert(0, ".")

from pss_client.auth import CHECKSUM_KEY, SAVY_CHECKSUM, PssAuth, create_checksum
from pss_client.transport import PssApiError


async def main() -> None:
    # 1) Test unitario de la fórmula
    dk = str(uuid4())
    now = datetime.now(timezone.utc)
    checksum = create_checksum(dk, now, "DeviceTypeAndroid")
    assert len(checksum) == 32, checksum
    print(f"[unit] create_checksum OK -> {checksum}")

    # 2) Test contra la API real (cuenta nueva desechable — SOLO validación de handshake)
    auth = PssAuth()
    try:
        login = await auth.device_login(device_key=dk, client_datetime=now)
        print(f"[api ] LOGIN OK  user_id={login.user_id} token={login.access_token[:8]}...")
        print(f"[api ] previous_last_login={login.previous_last_login_date}")
    except PssApiError as e:
        print(f"[api ] FALLO: {e}")
        raise
    finally:
        await auth.close()

    print("\nBackend de autenticación verificado.")
    print(f"  CHECKSUM_KEY  = {CHECKSUM_KEY!r}")
    print(f"  SAVY_CHECKSUM = {SAVY_CHECKSUM!r}")
    print("\nPara entrar a TU cuenta: PssAuth().device_login(device_key='<tu device_key del juego>')")


if __name__ == "__main__":
    asyncio.run(main())
