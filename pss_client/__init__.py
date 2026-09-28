"""pss_client — Cliente Python para la API de Pixel Starships.

Backend de autenticación derivado del análisis estático del cliente oficial
(IL2CPP: GameAssembly.so + global-metadata.dat) y de la estructura de pssapi.py.
"""

from .auth import PssAuth, UserLogin

__all__ = ["PssAuth", "UserLogin"]
__version__ = "0.1.0"
