"""
OLT Auto-Provision Daemon — Core Package.

Este paquete contiene los módulos principales del daemon:
- listener: Servidor UDP asíncrono para recepción de syslog.
- parser:   Parser de mensajes syslog parametrizable por modelo de OLT.
- ssh_worker: Worker SSH para aprovisionamiento de ONTs (refactorizado del legacy).
"""

__version__ = "1.0.0"
