"""pss_client — Cliente Python para la API de Pixel Starships.

Backend de autenticación derivado del análisis estático del cliente oficial
(IL2CPP: GameAssembly.so + global-metadata.dat) y del flujo de pss-companion.com.

Único método de login: email + contraseña (email_password_login).
"""

from .auth import PssAuth, UserLogin

__all__ = ["PssAuth", "UserLogin"]
__version__ = "0.2.0"
