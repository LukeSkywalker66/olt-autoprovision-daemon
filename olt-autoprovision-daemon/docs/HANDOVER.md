# OLT Auto-Provision Daemon — Handover & Debugging Guide

**Documento operativo para el agente NetDevOps a cargo del despliegue, monitoreo y debugging del daemon.**

---

## Tabla de Contenidos

1. [Arranque Rápido](#1-arranque-rápido)
2. [Flujo End-to-End: Lo que ocurre cuando una ONT se conecta](#2-flujo-end-to-end-lo-que-ocurre-cuando-una-ont-se-conecta)
3. [Guía de Lectura de Logs](#3-guía-de-lectura-de-logs)
4. [Escenarios de Falla y Diagnóstico](#4-escenarios-de-falla-y-diagnóstico)
5. [Cómo Simular un Evento Syslog (Testing sin OLT real)](#5-cómo-simular-un-evento-syslog-testing-sin-olt-real)
6. [Verificación del Lado OLT](#6-verificación-del-lado-olt)
7. [Agregar una Nueva OLT al Sistema](#7-agregar-una-nueva-olt-al-sistema)
8. [Señales de Vida (Health Check)](#8-señales-de-vida-health-check)
9. [Métricas Clave desde los Logs](#9-métricas-clave-desde-los-logs)
10. [FAQ — Problemas Comunes](#10-faq--problemas-comunes)

---

## 1. Arranque Rápido

### 1.1 Instalación

```bash
cd /opt/olt-autoprovision-daemon
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 1.2 Configuración

```bash
# Crear archivo de configuración real desde el template
cp config/olts.yaml.example config/olts.yaml
# Editar con credenciales reales
vim config/olts.yaml
```

### 1.3 Ejecución

```bash
# Modo debug (MUY verboso — ideal para troubleshooting inicial)
python main.py --debug

# Modo producción (menos verboso, solo INFO+)
python main.py

# Puerto privilegiado (requiere root o CAP_NET_BIND_SERVICE)
sudo python main.py --port 514

# Con archivo de configuración custom
python main.py --config /etc/olt/production-olts.yaml
```

### 1.4 Instalación como Servicio Systemd

```bash
sudo cp deploy/olt-provision.service /etc/systemd/system/
sudo useradd -r -s /bin/false olt-daemon
sudo chown -R olt-daemon:olt-daemon /opt/olt-autoprovision-daemon
sudo systemctl daemon-reload
sudo systemctl enable --now olt-provision.service

# Ver logs del servicio
sudo journalctl -u olt-provision.service -f

# Ver logs de la aplicación
tail -f /opt/olt-autoprovision-daemon/logs/olt-provision-daemon.log
```

---

## 2. Flujo End-to-End: Lo que ocurre cuando una ONT se conecta

```
PASO 1: ONT nueva se conecta físicamente al puerto PON de la OLT
        ↓
PASO 2: OLT detecta la ONT → ont auto-add-policy la da de alta automáticamente
        (Le asigna un ONT ID y aplica line-profile + service-profile)
        ↓
PASO 3: OLT intenta auto-crear un service-port → FALLA (esperado)
        ↓
PASO 4: OLT emite syslog UDP WARNING:
        "Parameters for automatic service port creation are incorrect"
        con FrameID, SlotID, PortID, ONT ID
        ↓
PASO 5: Listener UDP recibe el datagrama → extrae src_ip del paquete
        ↓
PASO 6: Listener busca la OLT en olts.yaml por src_ip → obtiene credenciales
        ↓
PASO 7: Listener ejecuta parse_syslog_message() → extrae fsp="0/1/0", ont_id="0"
        ↓
PASO 8: Listener verifica DedupCache → ¿ya procesado en los últimos 5 min?
        ├── SÍ → "Dedup SKIP" (INFO) → FIN
        └── NO  → Continúa
        ↓
PASO 9: Listener envía ssh_provision_worker() al ThreadPoolExecutor
        ↓
PASO 10: Worker SSH:
        a) Conecta por SSH a la OLT (Netmiko)
        b) Entra a modo config: enable → config
        c) interface gpon 0/{slot}           ← Entra a la interfaz GPON
        d) ont ipconfig {port} {ont_id} ...   ← Crea WAN DHCP en VLAN gestión
        e) ont tr069-server-config ...        ← Inyecta perfil TR-069
        f) service-port vlan {mgmt} ...       ← Crea el service-port
        g) quit                               ← Sale de GPON
        ↓
PASO 11: ONT ya tiene conectividad IP (DHCP en VLAN gestión) + TR-069.
         El ACS (GenieACS/OpenACS) la descubre y termina de configurar.
```

**Tiempo total estimado desde que la ONT se conecta hasta que tiene IP**: 5-15 segundos (depende de latencia SSH + delay entre comandos).

---

## 3. Guía de Lectura de Logs

### 3.1 Estructura de una línea de log

```
2026-02-09 00:06:31 [INFO    ] olt_daemon.listener: ✓ ONT auto-discovered: OLT=NODO-HORNILLOS (10.11.104.2) FSP=0/1/0 ONT_ID=0
│                    │         │                   │
│                    │         │                   └── Mensaje
│                    │         └── Logger name (módulo de origen)
│                    └── Nivel (DEBUG|INFO|WARNING|ERROR|CRITICAL)
└── Timestamp UTC
```

### 3.2 Logger Names por Módulo

| Logger Name | Archivo | Qué registra |
|-------------|---------|-------------|
| `olt_daemon` | [`main.py`](main.py) | Arranque, shutdown, carga de config |
| `olt_daemon.listener` | [`core/listener.py`](core/listener.py) | Recepción UDP, parseo, dedup, dispatch |
| `olt_daemon.ssh_worker` | [`core/ssh_worker.py`](core/ssh_worker.py) | Conexión SSH, comandos, resultados |
| `netmiko` | (externo) | Detalles de sesión SSH (suprimido a WARNING) |
| `paramiko` | (externo) | Detalles de transporte SSH (suprimido a WARNING) |

### 3.3 Ejemplo: Log de un Provisioning Exitoso (modo `--debug`)

```
2026-02-09 00:06:30 [INFO    ] olt_daemon: Logging initialized — file: logs/olt-provision-daemon.log
2026-02-09 00:06:30 [INFO    ] olt_daemon: ============================================================
2026-02-09 00:06:30 [INFO    ] olt_daemon: OLT Auto-Provision Daemon v1.0.0 starting...
2026-02-09 00:06:30 [INFO    ] olt_daemon: Configuration: /opt/olt-autoprovision-daemon/config/olts.yaml
2026-02-09 00:06:30 [INFO    ] olt_daemon: Listener: 0.0.0.0:5514 (UDP)
2026-02-09 00:06:30 [INFO    ] olt_daemon: Max SSH workers: 50
2026-02-09 00:06:30 [INFO    ] olt_daemon: Loaded OLT: NODO-HORNILLOS (10.11.104.2) — user=smartoltusr port=22 vlan=150 tr069_profile=1
2026-02-09 00:06:30 [INFO    ] olt_daemon: Loaded 1 OLT(s) from config/olts.yaml
2026-02-09 00:06:30 [INFO    ] olt_daemon.listener: Starting SyslogListener on 0.0.0.0:5514 (max_workers=50, registered_olts=1)
2026-02-09 00:06:30 [INFO    ] olt_daemon.listener: Registered OLT IPs: 10.11.104.2
2026-02-09 00:06:30 [INFO    ] olt_daemon.listener: SyslogListener is now listening on UDP :5514

  ... (el daemon espera pasivamente) ...

2026-02-09 00:06:31 [DEBUG   ] olt_daemon.listener: Datagram #1 from 10.11.104.2:514 (342 bytes)
2026-02-09 00:06:31 [INFO    ] olt_daemon.listener: ✓ ONT auto-discovered: OLT=NODO-HORNILLOS (10.11.104.2) FSP=0/1/0 ONT_ID=0
2026-02-09 00:06:31 [DEBUG   ] olt_daemon.listener: Dedup MISS: OLT=10.11.104.2 ONT=0/1/0/0 — recorded in cache (ttl=300s)
2026-02-09 00:06:31 [INFO    ] olt_daemon.ssh_worker: === Starting provisioning: OLT=NODO-HORNILLOS (10.11.104.2) ONT=0/1/0/0 ===
2026-02-09 00:06:31 [INFO    ] olt_daemon.ssh_worker: Connecting to OLT NODO-HORNILLOS (10.11.104.2:22)...
2026-02-09 00:06:32 [INFO    ] olt_daemon.ssh_worker: Connected to NODO-HORNILLOS — prompt: NODO-HORNILLOS>
2026-02-09 00:06:32 [INFO    ] olt_daemon.ssh_worker: Initial prompt for NODO-HORNILLOS: NODO-HORNILLOS>
2026-02-09 00:06:32 [INFO    ] olt_daemon.ssh_worker: Post-enable prompt: NODO-HORNILLOS#
2026-02-09 00:06:32 [INFO    ] olt_daemon.ssh_worker: Post-config prompt: NODO-HORNILLOS(config)#
2026-02-09 00:06:32 [INFO    ] olt_daemon.ssh_worker: Config mode confirmed for NODO-HORNILLOS
2026-02-09 00:06:32 [INFO    ] olt_daemon.ssh_worker: [STEP-1] interface gpon 0/1
2026-02-09 00:06:33 [INFO    ] olt_daemon.ssh_worker: [STEP-2] ont ipconfig 0 0 ip-index 0 dhcp vlan 150 priority 2
2026-02-09 00:06:33 [INFO    ] olt_daemon.ssh_worker: [STEP-3] ont tr069-server-config 0 0 profile-id 1
2026-02-09 00:06:34 [INFO    ] olt_daemon.ssh_worker: [STEP-4] service-port vlan 150 gpon 0/1/0 ont 0 gemport 2 multi-service user-vlan 150 tag-transform translate inbound traffic-table index 7 outbound traffic-table index 7
2026-02-09 00:06:34 [INFO    ] olt_daemon.ssh_worker: [STEP-5] quit
2026-02-09 00:06:34 [INFO    ] olt_daemon.ssh_worker: === Provisioning OK: OLT=NODO-HORNILLOS ONT=0/1/0/0 (5 commands in 2.8s) ===
2026-02-09 00:06:34 [INFO    ] olt_daemon.ssh_worker: Disconnected from NODO-HORNILLOS
2026-02-09 00:06:34 [INFO    ] olt_daemon.listener: ✓ Worker OK: OLT=NODO-HORNILLOS ONT=0/1/0/0 (5 cmds in 2.8s)
```

---

## 4. Escenarios de Falla y Diagnóstico

### 4.1 "El daemon arranca pero nunca detecta ONTs"

**Síntoma**: el daemon corre, no hay errores, pero nunca se ve `✓ ONT auto-discovered`.

**Causas posibles y diagnóstico**:

| Causa | Cómo confirmarla | Solución |
|-------|-----------------|----------|
| La OLT no está enviando syslog al daemon | `tcpdump -i any port 5514 -nn` en el server del daemon. Si no ves paquetes desde la IP de la OLT, no está configurado el syslog export. | Configurar `info-center loghost <IP_DAEMON>` en la OLT. |
| La IP de la OLT en `olts.yaml` no coincide con la IP origen del paquete UDP | Buscar en logs: `WARNING Unknown OLT source IP: X.X.X.X`. Esa X.X.X.X es la IP real que hay que poner en el YAML. | Corregir la clave IP en `olts.yaml`. |
| El parser no matchea el formato del syslog | Activar `--debug` y buscar `Non-matching syslog from`. Si ves muchos de estos para la OLT correcta, el formato del syslog cambió. | Revisar el mensaje crudo en los logs DEBUG y ajustar el regex en [`core/parser.py`](core/parser.py). |
| El puerto UDP está bloqueado por firewall | `iptables -L -n \| grep 5514` o `ufw status`. | Abrir el puerto UDP en el firewall. |
| systemd no permite bind al puerto | `journalctl -u olt-provision.service \| grep -i "permission denied"` | Usar puerto >1024 o agregar `AmbientCapabilities=CAP_NET_BIND_SERVICE`. |

### 4.2 "El daemon detecta la ONT pero falla el SSH"

**Síntoma**: se ve `✓ ONT auto-discovered` pero luego `✗ Worker FAIL`.

**Causas posibles y diagnóstico**:

| Causa | Log característico | Solución |
|-------|-------------------|----------|
| IP/hostname inalcanzable | `ERROR ... NetmikoTimeoutException` | Verificar conectividad: `ping <OLT_IP>` y `telnet <OLT_IP> 22` desde el server del daemon. |
| Credenciales incorrectas | `ERROR ... NetmikoAuthenticationException` | Verificar `ssh_user` y `ssh_pass` en `olts.yaml`. Probar manualmente: `ssh <user>@<OLT_IP> -p <port>`. |
| Puerto SSH incorrecto | `ERROR ... NetmikoTimeoutException` (si está cerrado) o `ERROR ... Connection refused` | Verificar `ssh_port` en `olts.yaml`. |
| OLT no acepta conexiones SSH | `ERROR ... Network error: Connection reset` | La OLT puede tener un ACL que bloquea la IP del daemon. Verificar en la OLT: `display acl`. |
| Timeout durante comandos (OLT lenta) | `WARNING OLT ... busy executing` (múltiples reintentos) | Aumentar `max_retries` en `olts.yaml`. Si persiste, la OLT puede estar haciendo backup; esperar a que termine. |

### 4.3 "El daemon detecta y conecta, pero falla un comando específico"

**Síntoma**: se ven los STEP-1, STEP-2... y de repente `ERROR Command X/Y failed`.

| Causa | Log característico | Solución |
|-------|-------------------|----------|
| La ONT ya fue eliminada o el ONT ID cambió | `Failure: The ONT does not exist` | Race condition raro. La OLT re-asignó el ONT ID. El evento es viejo. |
| El service-port ya existe | `Failure: The service-port already exists` | La ONT ya fue provisionada (posiblemente por una ejecución anterior). El dedup cache debió prevenirlo; si no, verificar TTL. |
| El perfil TR-069 no existe en la OLT | `Failure: The profile does not exist` | Verificar que el `tr069_profile_id` en `olts.yaml` corresponda a un perfil que exista en la OLT: `display ont tr069-server-profile all`. |
| La traffic-table no existe | `Failure: The traffic table does not exist` | Verificar `traffic_table_up` / `traffic_table_down`. Si es numérico: `display traffic-table index X`. Si es nombre: `display traffic-table name X`. |
| La VLAN de gestión no está creada | `Failure: The VLAN does not exist` | Verificar que `management_vlan` exista en la OLT: `display vlan <id>`. |
| El GEM port no es válido | `Failure: Invalid GEM port` | El `gemport` en `olts.yaml` debe ser 1 o 2 (típicamente). Verificar en la OLT: `display ont info <port> <ont_id>`. |

### 4.4 "El daemon provisiona la misma ONT múltiples veces"

**Síntoma**: en logs se ven múltiples `✓ Worker OK` para el mismo FSP/ONT_ID en poco tiempo.

| Causa | Diagnóstico | Solución |
|-------|------------|----------|
| Dedup cache TTL muy corto | El TTL por defecto es 300s (5 min). Si la OLT envía el warning cada 6 minutos, no se detecta como duplicado. | Aumentar `dedup_ttl_seconds` en `olts.yaml` (ej: 600 para 10 min). |
| La OLT envía el warning con diferente formato | Si el parser extrae FSP u ONT_ID distinto cada vez, la clave del cache no matchea. | Revisar los logs DEBUG con los mensajes crudos. |
| Múltiples OLTs con la misma IP | Dos entradas en `olts.yaml` con misma IP — solo la última se carga. | Verificar que no haya IPs duplicadas en el YAML. |

---

## 5. Cómo Simular un Evento Syslog (Testing sin OLT real)

### 5.1 Enviar un syslog falso con netcat (Linux)

```bash
# Asegurate de que el daemon está corriendo en --debug

# Enviar un evento simulado desde la IP de la OLT (requiere que el server
# tenga una interfaz con esa IP, o usar la IP real del server)
echo '<132> 2000-02-09 00:06:30-03:00 0.0.0.0 ! RUNNING WARNING 2000-02-09 00:06:29-03:00
  EVENT NAME :Parameters for automatic service port creation are incorrect
  PARAMETERS :FrameID: 0, SlotID: 1, PortID: 0, ONT ID: 0, Cause:  Parameters for automatic service port creation of an ONT are not configured or incorrectly configured' \
  | nc -u -w1 127.0.0.1 5514
```

### 5.2 Usar el script de prueba incluido

Crear un script `tools/send_test_syslog.py`:

```python
#!/usr/bin/env python3
"""Send a simulated Huawei MA5800 syslog event to the daemon for testing."""
import socket
import sys

TARGET_HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
TARGET_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 5514

# Syslog real capturado con tcpdump desde MA5800-X2
PAYLOAD = (
    "<132> 2000-02-09 00:06:30-03:00 0.0.0.0 ! RUNNING WARNING "
    "2000-02-09 00:06:29-03:00\n"
    "  EVENT NAME :Parameters for automatic service port creation are incorrect\n"
    "  PARAMETERS :FrameID: 0, SlotID: 1, PortID: 0, ONT ID: 0, "
    "Cause:  Parameters for automatic service port creation of an ONT "
    "are not configured or incorrectly configured"
)

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.sendto(PAYLOAD.encode("utf-8"), (TARGET_HOST, TARGET_PORT))
print(f"Sent test syslog to {TARGET_HOST}:{TARGET_PORT}")
sock.close()
```

```bash
python tools/send_test_syslog.py 127.0.0.1 5514
# Luego revisar logs/: deberías ver "✓ ONT auto-discovered" seguido del worker SSH
```

### 5.3 Qué esperar en los logs tras el test

Con `--debug`:

1. `DEBUG Datagram #N from 127.0.0.1:XXXX (XXX bytes)` — el paquete llegó.
2. `WARNING Unknown OLT source IP: 127.0.0.1` — porque 127.0.0.1 no está en el YAML.

Para que el flujo completo funcione en test, agregá temporalmente tu IP local al YAML:

```yaml
olts:
  "127.0.0.1":
    name: "TEST-LOCAL"
    ssh_user: "test"
    ssh_pass: "test"
```

Luego verás el intento de SSH (que fallará porque no hay OLT real), pero confirmará que el pipeline UDP → parser → dispatch funciona.

---

## 6. Verificación del Lado OLT

### 6.1 Confirmar que la OLT envía syslog al daemon

En la OLT Huawei:
```
display info-center
```
Verificar que:
- `info-center enable` está activo.
- Hay una entrada `loghost` apuntando a la IP y puerto del daemon.
- El facility y level son compatibles (el daemon no filtra por facility/severity — procesa todo).

Para configurarlo (si no está):
```
system-view
info-center enable
info-center loghost 192.168.1.100 facility local1
info-center source default channel 2 log level warning
commit
```

### 6.2 Confirmar que `ont auto-add-policy` está activo

```
display ont auto-add-policy
```
Debe mostrar `Policy: Permit` para los puertos GPON relevantes.

Si no está configurado:
```
ont auto-add-policy permit
commit
```

### 6.3 Confirmar que los perfiles existen

Los perfiles que el daemon referencia DEBEN existir en la OLT:

```
display ont tr069-server-profile all          ← Debe mostrar el profile-id del YAML
display traffic-table index 7                 ← O el nombre si usás nombres
display vlan 150                              ← La VLAN de gestión
```

Si un perfil no existe, el comando SSH fallará con `Failure: The profile does not exist`.

---

## 7. Agregar una Nueva OLT al Sistema

### Paso a paso

1. **Obtener la IP origen real del syslog**: ejecutá `tcpdump -i any port 5514 -nn` en el server del daemon mientras conectás una ONT a la nueva OLT. La IP que veas en los paquetes UDP es la que va como clave en el YAML.

2. **Agregar la entrada en `config/olts.yaml`**:
   ```yaml
   olts:
     "10.20.30.40":                    # ← IP origen del syslog
       name: "NODO-NUEVO"
       ssh_user: "smartoltusr"
       ssh_pass: "password_seguro"
       # Opcionales si difieren de defaults:
       ssh_port: 2222
       management_vlan: 200
       tr069_profile_id: 3
   ```

3. **Verificar conectividad SSH** desde el server del daemon:
   ```bash
   ssh smartoltusr@10.20.30.40 -p 2222
   # Deberías llegar al prompt de la OLT
   ```

4. **Verificar que los perfiles existen en la OLT**:
   ```
   ssh smartoltusr@10.20.30.40
   enable
   config
   display ont tr069-server-profile all
   display vlan 200
   display traffic-table index 7
   ```

5. **Reiniciar el daemon**:
   ```bash
   sudo systemctl restart olt-provision.service
   # O si estás corriendo manualmente: Ctrl+C y volver a ejecutar
   ```

6. **Verificar en logs** que la nueva OLT se cargó:
   ```
   grep "Loaded OLT" logs/olt-provision-daemon.log
   # Debe mostrar: Loaded OLT: NODO-NUEVO (10.20.30.40) — user=smartoltusr port=2222 vlan=200 tr069_profile=3
   ```

7. **Probar con una ONT real**: conectar una ONT a un puerto PON de la nueva OLT y verificar en logs que aparece `✓ ONT auto-discovered: OLT=NODO-NUEVO`.

---

## 8. Señales de Vida (Health Check)

### 8.1 ¿El proceso está corriendo?

```bash
systemctl status olt-provision.service        # Si usás systemd
ps aux | grep "python main.py"                # Si lo corrés manualmente
```

### 8.2 ¿El socket UDP está escuchando?

```bash
ss -uln | grep 5514
# Debe mostrar: UNCONN 0 0 0.0.0.0:5514 0.0.0.0:*
```

### 8.3 ¿Está recibiendo paquetes?

Mientras el daemon corre en `--debug`, cada paquete UDP recibido genera:
```
DEBUG ... Datagram #N from X.X.X.X:XXXX (XXX bytes)
```

Si el contador `#N` no avanza en varios minutos, no están llegando syslogs.

### 8.4 ¿El thread pool está sano?

El ThreadPoolExecutor tiene un máximo de 50 workers (configurable con `--max-workers`). Si los 50 están ocupados, nuevas tareas se encolan. En logs, si ves muchos `=== Starting provisioning ===` sin sus correspondientes `=== Provisioning OK ===`, puede haber congestión.

Para monitorear:
```bash
# Contar workers activos (provisionings en vuelo)
grep "Starting provisioning" logs/olt-provision-daemon.log | wc -l
grep "Provisioning OK" logs/olt-provision-daemon.log | wc -l
# La diferencia son los que están en vuelo o fallaron
```

---

## 9. Métricas Clave desde los Logs

Ejecutá estos comandos sobre `logs/olt-provision-daemon.log` para obtener métricas rápidas:

```bash
# Total de ONTs detectadas (eventos matcheados)
grep -c "ONT auto-discovered" logs/olt-provision-daemon.log

# Total de provisionings exitosos
grep -c "Provisioning OK" logs/olt-provision-daemon.log

# Total de fallos
grep -c "Worker FAIL" logs/olt-provision-daemon.log

# Total de eventos duplicados (dedup hits)
grep -c "Dedup SKIP" logs/olt-provision-daemon.log

# OLTs desconocidas (IPs no registradas en YAML)
grep "Unknown OLT source IP" logs/olt-provision-daemon.log | sort -u

# Tiempo promedio de provisioning (extraer elapsed_seconds)
grep "Provisioning OK" logs/olt-provision-daemon.log | grep -oP '\d+\.\d+s' | awk '{sum+=$1; n++} END {print sum/n "s"}'

# Errores de autenticación SSH
grep -c "NetmikoAuthenticationException" logs/olt-provision-daemon.log

# Errores de timeout SSH
grep -c "NetmikoTimeoutException" logs/olt-provision-daemon.log

# OLT ocupada (reintentos)
grep -c "OLT.*busy executing" logs/olt-provision-daemon.log
```

---

## 10. FAQ — Problemas Comunes

### Q: El daemon dice "Unknown OLT source IP" para una IP que SÍ está en el YAML.
**A**: La IP en el YAML debe ser EXACTAMENTE igual a la IP origen del paquete UDP. Si la OLT tiene múltiples IPs (loopback, management, etc.), el syslog sale por la IP de la interfaz de ruteo hacia el daemon. Usá `tcpdump` para ver la IP real.

### Q: El worker SSH falla con timeout pero puedo hacer SSH manualmente.
**A**: Netmiko espera patrones de prompt específicos. Si el prompt de la OLT es muy lento en aparecer (OLT sobrecargada), aumentá `cmd_delay` en el YAML. También verifica que el prompt no tenga caracteres no-ASCII que confundan a Netmiko.

### Q: ¿Puedo correr múltiples instancias del daemon?
**A**: No en el mismo puerto UDP. El socket hace bind exclusivo. Si necesitás alta disponibilidad, usá keepalived para una IP virtual flotante y corré una sola instancia.

### Q: La OLT envía MUCHOS syslogs por minuto. ¿El daemon se satura?
**A**: El listener asyncio puede manejar miles de datagramas por segundo sin bloquearse. El cuello de botella es el ThreadPoolExecutor (50 workers por defecto). Si necesitás más concurrencia, aumentá `--max-workers`. Pero ojo: cada worker consume una conexión SSH a la OLT.

### Q: ¿Qué pasa si la OLT está en backup y responde "System is busy"?
**A**: El `HuaweiSSHClient._send_command()` detecta los patrones de "busy" y reintenta automáticamente (hasta `max_retries` veces, con espera de 200s entre intentos). Esto es el mismo comportamiento probado del código legacy.

### Q: ¿El daemon hace `ont add`?
**A**: NO. La OLT debe tener `ont auto-add-policy permit` para que registre la ONT automáticamente. El daemon solo crea la capa de servicio (WAN DHCP + TR-069 + service-port).

### Q: Se perdió la conexión SSH a mitad del provisioning. ¿La ONT queda a medio configurar?
**A**: Posiblemente. Los comandos VRP no son transaccionales. Si falla el paso 3 (TR-069) después de haber ejecutado el paso 2 (WAN DHCP), la ONT tendrá IP pero no TR-069. El ACS eventualmente la detectará parcialmente. Para reprovisionar, simplemente eliminá el service-port manualmente y esperá a que la OLT re-emita el warning (o forzalo desconectando y reconectando la ONT). El dedup cache expirará en 5 minutos y el daemon reintentará.

---

## Apéndice A: Árbol de Decisión para Debugging

```
¿El daemon arranca?
├── NO → Revisar:
│   ├── ¿Python 3.11+? → python --version
│   ├── ¿netmiko + pyyaml instalados? → pip list | grep -E "netmiko|pyyaml"
│   ├── ¿config/olts.yaml existe? → ls config/olts.yaml
│   ├── ¿Puerto UDP disponible? → ss -uln | grep <port>
│   └── ¿Error en logs? → cat logs/olt-provision-daemon.log
│
└── SÍ → ¿Detecta ONTs?
    ├── NO → Revisar:
    │   ├── ¿Llegan paquetes UDP? → tcpdump -i any port 5514 -nn
    │   ├── ¿IP en YAML coincide con src_ip? → grep "Unknown OLT" logs/*
    │   ├── ¿Parser matchea? → Activar --debug, buscar "Non-matching"
    │   └── ¿OLT configurada para enviar syslog? → display info-center
    │
    └── SÍ → ¿Provisiona OK?
        ├── NO → Revisar:
        │   ├── ¿Error de conexión? → grep "Timeout\|Auth\|Network error" logs/*
        │   ├── ¿Error de comando? → grep "Command.*failed\|Failure:" logs/*
        │   └── ¿Perfil inexistente? → Verificar en OLT: display ont tr069-server-profile all
        │
        └── SÍ → ¡Sistema funcionando correctamente! 🎉
```

---

## Apéndice B: Comandos Útiles en la OLT para Debugging

```bash
# Ver ONTs registradas en un puerto PON
display ont info 0 0         # Puerto 0/1/0 (slot=1, port=0)

# Ver service-ports de una ONT
display service-port port 0/1/0 ont 0

# Ver configuración TR-069 de una ONT
display ont tr069-server-config 0 0

# Ver perfiles TR-069 disponibles
display ont tr069-server-profile all

# Ver tráfico de syslog saliente
display info-center

# Ver buffer de logs local (para comparar con lo que recibe el daemon)
display logbuffer | include "service port"
```

---

*Documento mantenido por el equipo NetDevOps — Última actualización: 2026-07-02*
