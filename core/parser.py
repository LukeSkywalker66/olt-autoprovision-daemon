"""
Syslog Parser — Parametrizable por modelo de OLT.

Implementa el patrón Strategy para soportar múltiples fabricantes/modelos de OLT.
Cada subclase de BaseSyslogParser define su propia expresión regular con Named Capture Groups
para extraer los parámetros relevantes del mensaje syslog.

Evento trigger (Huawei MA5800):
    "Parameters for automatic service port creation are incorrect"
    → Se dispara inmediatamente después del auto-add-policy de la OLT.
    → Contiene FrameID, SlotID, PortID, y ONT ID definitivo.

Uso:
    from core.parser import parse_syslog_message

    result = parse_syslog_message(raw_syslog_str)
    if result:
        print(result["fsp"], result["ont_id"])  # "0/1/0", "0"
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import ClassVar


# =============================================================================
# Base Class — Strategy Pattern
# =============================================================================

class BaseSyslogParser(ABC):
    """
    Parser abstracto para mensajes syslog de OLTs.

    Cada subclase debe definir PATTERN (regex con Named Capture Groups)
    y sobrescribir parse() para retornar un dict con al menos las claves
    'fsp' (str: "FrameID/SlotID/PortID") y 'ont_id' (str).
    """

    PATTERN: ClassVar[str] = ""

    @abstractmethod
    def parse(self, raw_message: str) -> dict[str, str] | None:
        """
        Intenta parsear un mensaje syslog.

        Args:
            raw_message: Mensaje syslog crudo (puede ser multi-línea).

        Returns:
            dict con claves 'fsp' y 'ont_id' si el mensaje es relevante.
            None si no hace match con el patrón de este parser.
        """
        ...


# =============================================================================
# Huawei MA5800 Parser
# =============================================================================

class HuaweiMA5800Parser(BaseSyslogParser):
    """
    Parser para OLTs Huawei MA5800 series (MA5800-X2, MA5800-X7, etc.).

    Trigger event:
        "Parameters for automatic service port creation are incorrect"

    Este evento ocurre inmediatamente después del auto-add-policy de la OLT,
    cuando esta intenta crear un service-port automático y falla porque los
    parámetros no están configurados (comportamiento esperado: el daemon los
    creará de forma controlada vía SSH).

    Formato real del syslog (capturado con tcpdump):
        <132> 2000-02-09 00:06:30-03:00 0.0.0.0 ! RUNNING WARNING 2000-02-09 00:06:29-03:00
          EVENT NAME :Parameters for automatic service port creation are incorrect
          PARAMETERS :FrameID: 0, SlotID: 1, PortID: 0, ONT ID: 0, Cause: ...
    """

    # Regex con Named Capture Groups.
    # - Coincide con "Parameters for automatic service port creation are incorrect"
    #   (insensible a mayúsculas/minúsculas).
    # - Extrae FrameID, SlotID, PortID por separado.
    # - Extrae ONT ID.
    # - Soporta espacios variables alrededor de ':' y ','.
    # - Usa re.DOTALL para que .*? cruce límites de línea.
    PATTERN: ClassVar[str] = (
        r"Parameters\s+for\s+automatic\s+service\s+port\s+creation\s+are\s+incorrect"
        r".*?"
        r"FrameID\s*:\s*(?P<frame>\d+)"
        r"\s*,\s*"
        r"SlotID\s*:\s*(?P<slot>\d+)"
        r"\s*,\s*"
        r"PortID\s*:\s*(?P<port>\d+)"
        r"\s*,\s*"
        r"ONT\s+ID\s*:\s*(?P<ont_id>\d+)"
    )

    def parse(self, raw_message: str) -> dict[str, str] | None:
        """
        Parsea un mensaje syslog de Huawei MA5800.

        Args:
            raw_message: Mensaje syslog crudo (una o múltiples líneas).

        Returns:
            dict {"fsp": "0/1/0", "ont_id": "0"} si el evento es relevante.
            None si el mensaje no contiene el evento de service-port incorrecto.
        """
        # Normalizar: colapsar todos los whitespace (incluyendo saltos de línea)
        # en espacios simples para que el regex funcione sobre mensajes multi-línea.
        flattened = " ".join(raw_message.split())

        match = re.search(self.PATTERN, flattened, re.IGNORECASE | re.DOTALL)
        if not match:
            return None

        frame = match.group("frame")
        slot = match.group("slot")
        port = match.group("port")
        ont_id = match.group("ont_id")

        return {
            "fsp": f"{frame}/{slot}/{port}",
            "ont_id": ont_id,
        }


# =============================================================================
# Parser Registry — Extensible por modelo de OLT
# =============================================================================

PARSER_REGISTRY: dict[str, type[BaseSyslogParser]] = {
    "huawei_ma5800": HuaweiMA5800Parser,
    # Futuros modelos:
    # "huawei_ma5608t": HuaweiMA5608TParser,
    # "zte_c600": ZTEC600Parser,
    # "nokia_7360": Nokia7360Parser,
}


def get_parser(model: str) -> BaseSyslogParser:
    """
    Factoría de parsers. Devuelve una instancia del parser para el modelo de OLT dado.

    Args:
        model: Nombre del modelo registrado (ej: "huawei_ma5800").

    Returns:
        Instancia concreta de BaseSyslogParser.

    Raises:
        ValueError: Si el modelo no está registrado en PARSER_REGISTRY.
    """
    parser_cls = PARSER_REGISTRY.get(model)
    if parser_cls is None:
        raise ValueError(
            f"Unknown parser model: '{model}'. "
            f"Registered models: {list(PARSER_REGISTRY.keys())}"
        )
    return parser_cls()


# =============================================================================
# Public API — Convenience Function
# =============================================================================

def parse_syslog_message(
    raw_syslog_str: str,
    model: str = "huawei_ma5800",
) -> dict[str, str] | None:
    """
    Parsea un mensaje syslog y extrae los parámetros de ONT si el evento es relevante.

    Esta es la función principal que debe usar el listener. Encapsula la factoría
    de parser y el parseo en un solo callable limpio y sin estado.

    Args:
        raw_syslog_str: Mensaje syslog crudo tal como llega por UDP.
        model: Modelo de parser a utilizar (default: "huawei_ma5800").

    Returns:
        dict con claves:
          - 'fsp' (str): Frame/Slot/Port en formato "X/Y/Z" (ej: "0/1/0").
          - 'ont_id' (str): ONT ID asignado por la OLT (ej: "0").
        None si el mensaje no es un evento de service-port incorrecto.

    Example:
        >>> msg = (
        ...     "<132> 2000-02-09 00:06:30-03:00 0.0.0.0 ! RUNNING WARNING ...\\n"
        ...     "  EVENT NAME :Parameters for automatic service port creation are incorrect\\n"
        ...     "  PARAMETERS :FrameID: 0, SlotID: 1, PortID: 0, ONT ID: 0, Cause: ..."
        ... )
        >>> result = parse_syslog_message(msg)
        >>> print(result)
        {'fsp': '0/1/0', 'ont_id': '0'}

        >>> parse_syslog_message("Some unrelated syslog message") is None
        True
    """
    parser = get_parser(model)
    return parser.parse(raw_syslog_str)
