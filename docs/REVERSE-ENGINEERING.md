# Ingeniería inversa de la API de Pixel Starships

Todo lo documentado aquí fue **verificado contra la API real** o **desensamblado del cliente oficial** (GameAssembly.so, IL2CPP). La fuente primaria son los issues #1-#9 de este repo y los commits que implementaron cada hallazgo; cada sección cita el suyo. Este documento los consolida para que no haya que excavar el historial.

Herramienta de trabajo: el [cliente Python `pssapi`](https://github.com/rgaudin/pssapi) (cassettes y raw models) sirvió de referencia continua para atributos XML y endpoints.

## 1. La API

- Base: `https://api.pixelstarships.com`, un REST de servicios (`UserService`, `ShipService`, `DesignService`, `TaskService`, `RoomService`, `GalaxyService`, `ResearchService`…).
- **Las respuestas son XML** cuya raíz es el nombre del servicio (`<UserService>`, `<ShipService>`…).
- Los errores viajan como **atributo** `errorMessage` en el XML — **HTTP es 200 incluso en fallos**. El transporte debe parsear el XML y buscar el atributo (`pss_client/transport.py`).
- Caso especial: una respuesta puede traer `errorCode` + `errorMessage` Y a la vez un `<User>` hijo con datos — es una respuesta estructurada, no un error plano (p. ej. el 400 `linked_account` del DeviceLogin17).
- Fechas en formato ISO `%Y-%m-%dT%H:%M:%S`, en **UTC** (verificado: `UpdateDate` del Ship coincide con la hora de las peticiones — issue #4).

## 2. Checksums

Los tres checksums fueron extraídos del desensamblado y **verificados empíricamente**: con la fórmula correcta la API cambia de `An error occurred.` (checksum inválido) a un error de negocio o éxito (checksum validado).

| Endpoint | Fórmula | Origen (RVA) |
|---|---|---|
| `UserService/DeviceLogin17` | `md5(deviceKey + clientDateTime + deviceType + "5343" + "Savvy!s0d@")` | `UserManager.DownloadUserLogin` 0x36F6370 → `StringExtensions.SavysodaEncryptString` 0x3822490 |
| `UserService/UserEmailPasswordAuthorize4` | `md5(deviceKey + email + clientDateTime + accessToken1 + "5343Savvy!s0d@")` | mismo patrón; reconstruido del JS de pss-companion y validado contra la API |
| `UserService/HeartBeat4` | **INT**: `ord(at[0]) + ord(at[1]) + ord(at[3]) + second × minute` (chars UTF-16 del accessToken) | `CheckHeartbeatInvoke` 0x3727460 → `CheckSumPasswordWithString` 0x36F54D0 + `TimeCheckSumForDate` 0x36F5530 |
| Acciones genéricas (`CollectResources3`, `Refill2`, `CollectDailyReward2`) | `md5(clientDateTime + "5343" + "Savvy!s0d@")` | patrón `SavysodaEncryptString`; verificado contra `GetEngagement` en pssapi |

Constantes (extraídas de los slots GOT del binario: `Configuration.get_ChecksumKey` en 0x77D9368, `Configuration.get_SavyChecksum` en 0x77EFF50):

- `CHECKSUM_KEY = "5343"`
- `SAVY_CHECKSUM = "Savvy!s0d@"`

Implementación: `pss_client/auth.py` (`create_checksum`) y `pss_client/game.py` (`create_action_checksum`, `send_heartbeat`).

**Nota HeartBeat4**: el checksum NO es md5, es un entero. La verificación empírica del fix (commit `fe8a0ad`): la API pasó de `An error occurred.` a `"You have been idle for too long."` con `errorCode=1` — checksum validado; el error restante era solo inactividad del token de prueba.

## 3. Login email+password (flujo companion de 3 pasos)

Reconstruido del JS de pss-companion.com y verificado contra la API real (responde `Email or password is incorrect.` con credenciales erróneas — el checksum llega validado). Implementación: `PssAuth.email_password_login` (`pss_client/auth.py`).

**Paso 0 — refreshToken persistido** (lo que hace el juego real con su sesión de dispositivo): si hay un `refreshToken` guardado en `~/.pss-client/tokens.json`, se canjea directo por un accessToken y no se pasa por email/password. Si falla, se cae al flujo completo.

**Paso 1 — `DeviceLogin17` con deviceKey nuevo** → crea una **cuenta web temporal** y devuelve su accessToken.

- deviceKey **determinista** por email: `uuid5(NAMESPACE_URL, "pss-client:{email}")` — mismo email, mismo "dispositivo" (commit `1897ec4`).
- `UserDeviceInfo` emulando el cliente que pss-companion.com usa (la API lo exige desde el endurecimiento de 2026-09): `OsVersion=AD12`, `OSBuild=0`, `DeviceName=AD123`, `Locale=en`, `ClientBuild=17180`, `ClientVersion=0.999.43`.
- **Si el deviceKey ya quedó vinculado** a una cuenta real (login previo), la API responde `errorCode=400` con el email en `errorMessage` y un `<User>` hijo → hay que **rotar el deviceKey** (`pss-client:{email}#1`) y reintentar una vez (commit `f1fa5b7`).

**Paso 2 — `UserEmailPasswordAuthorize4`** con el accessToken del paso 1 → devuelve el **refreshToken + UserId de la cuenta REAL** (no trae accessToken directo). Params: `clientDateTime, checksum, deviceKey, email, password, accessToken(paso1), languageKey=en, isWeb=True`. El refreshToken se persiste **aquí**, antes del paso 3, para que el login directo sobreviva cualquier caída posterior.

**Paso 3 — `DeviceLogin17` con el refreshToken** → accessToken owner (el token con el que se llama al resto de la API).

Errores de login observados:

- `Email or password is incorrect.` — credenciales malas.
- `An error occurred.` — checksum inválido o cuenta solo-Steam (el flujo email+password no aplica).
- `errorCode 445` — "too many accounts": el paso 1 creaba una cuenta temporal **nueva en cada login**; el deviceKey determinista lo evita (commit `1897ec4`).
- `errorCode 400` + email en `errorMessage` — deviceKey ya vinculado (`linked_account`); rotar (commit `f1fa5b7`).

## 4. Ciclo de vida de tokens y sesiones

- **accessToken: caduca a ~52 min** (medido 2026-10-04, issue #3: 51 heartbeats OK consecutivos y de golpe `Failed to authorize access token.` en TODOS los endpoints). El gateway lo renueva **proactivamente a los 45 min** de emisión real (el `saved_at` de `tokens.json`, no la hora de arranque del loop — tras un reinicio el token restaurado ya puede ser viejo) y **reactivamente** si una petición lo encuentra muerto (commit `f4ce0fd`).
- **refreshToken: larga duración**, canjeable por accesTokens nuevos con el mismo `DeviceLogin17` del paso 3. Su vida exacta (¿días? ¿semanas?) está pendiente de observación (issue #9).
- **`~/.pss-client/tokens.json`**: `{email: {device_key, refresh_token, user_id, saved_at, access_token}}` — restaura sesiones al arrancar el server sin re-login.
- **session_token del navegador: estable por cuenta** — `sess_{user_id}_{uuid5(NAMESPACE_URL, "pss-client:session:{email}")}`. Antes derivaba del accessToken (`access_token[:16]`) y como este rota cada ~1h, un reinicio del server tras una renovación dejaba la sesión del navegador huérfana → 401 → re-login (issue #5, commit `d9aebeb`).
- Bordes abiertos (issue #9): refresh revocado por login en otro dispositivo, restore ciego de tokens viejos, falta de circuit-breaker tras fallos repetidos de renovación.

## 5. Heartbeat — mantener la cuenta Online

El juego envía un heartbeat periódico por **WebSocket** (`wss://api.pixelstarships.com/heartbeatSocket?accessToken=`, payload `HeartbeatRequestApi {ClientDateTime, Checksum, PingPongInfo.Number}`) con **fallback REST** a `POST UserService/HeartBeat4`. Este cliente usa el REST con el checksum INT de la tabla anterior (commit `89f58b0`).

- El loop del gateway lo envía cada 60 s por sesión activa (se inicia en el login y se reactiva al arrancar para sesiones restauradas).
- **Efecto**: mantener la cuenta marcada Online en el servidor (es lo que hace el heartbeat del juego). Verificado que la API acepta el heartbeat propio: 51 ciclos consecutivos con `success="true"` en el log del proyecto (issue #3). La transición `Offline`→`Defending` **no** la controla el heartbeat — la controla el matchmaking del servidor (`defending_engagement_group`); la nave puede seguir `Defending` 30+ min sin heartbeats (issue #1).
- Enum `ShipStatus`: `Online=0, Offline=1, Attacking=2, Defending=3`.

## 6. Fórmulas del juego desensambladas

### 6.1 Stats por nivel — `PSCharacter.GetStatForLevel` (RVA 0x3853B30)

Interpolación del valor de un stat entre el nivel base y el máximo, según el `ProgressionType` del diseño:

```
x   = (L - 1) / (MaxLevel - 1)
Linear : pct = x
EaseIn : pct = x²
EaseOut: pct = 1 - (1 - x)²
stat = pct × Final + (1 - pct) × Basic
```

Distribución real del catálogo: Linear 489, EaseIn 48, EaseOut 17. Implementado en `designs.stat_for_level` con el join del catálogo (`character_stats` con `Hp`, `FinalHp`, `MaxCharacterLevel`, `ProgressionType` del `CharacterDesign`) — commit `d9aebeb`, issue #2.

### 6.2 Escudo máximo de la nave — `PSShip.get_MaxShield` (RVA 0x38CABC0)

```
MaxShield = Σ Capacity(roomDesign de cada sala con RoomType == Shield) × 100.0
```

Itera las rooms; para cada una con `get_RoomType() == 6` (`public const RoomType Shield = 6`), suma el `RoomDesign.<Capacity>k__BackingField` (offset 0xAC, verificado en dump.cs) y multiplica por `100.0` (constante en 0x136CD08).

`MainSceneUIManager.UpdateStatusPanel` (RVA 0x3591E30) es el único método que alimenta el `shieldPanel` (0xD0) del hangar: si `MaxShield > 0` muestra la barra (`InitBar(2, ceil(MaxShield/100.0))`); si es ≤ 0 la **oculta**. El juego **no muestra "escudo: 0"** fuera de batalla — muestra la capacidad máxima (issue #8, comentario con verificación real: las 4 salas Shield suman (2+3+0+12)×100 = **1.700**).

### 6.3 HP máximo de personaje — base interpolada + bonus de gym (issue #7, PENDIENTE de aplicar)

- El XML de cada `<Character>` de `GetShipByUserId` **sí expone el entrenamiento**: `HpImprovement, AttackImprovement, PilotImprovement, RepairImprovement, ScienceImprovement, EngineImprovement, WeaponImprovement, StaminaImprovement, AbilityImprovement, BoostLevel, Fatigue, TrainingData, TrainingDesignId`.
- `PSCharacter.get_TrainingUsed` (RVA 0x3851540) = suma de los 9 campos `*Improvement` — el medidor de capacidad del gym (`TrainingCapacity` del diseño).
- `PSCharacter.CharacterStats.HPAtLvl` (RVA 0x3871140): llama a `GetStatForLevel` y aplica `get_HP() = (HPAtLvl(level) + tempHP) × modHP` con `tempHP=modHP=1.0` fuera de batalla (constantes en 0x13677b0). `get_MaxHp` = `currentStats.get_HP() × 100.0` (la escala del factor ×100 viene de los Improvement).
- Enum `TrainingStat` (dump.cs L29827): `Unknown=0, Hp=1, Attack=2, Science=3, Repair=4, Stamina=5…` — `HpImprovement` es la mejora entrenada del stat Hp.
- Fórmula objetivo: `max_hp = (stat_for_level(level, Hp, FinalHp, MaxCharLevel, ProgressionType) + hp_improvement_bonus) × 1.0`, con la escala exacta del bonus pendiente de calibrar contra el juego (issue #7).

## 7. Hallazgos de mecánicas (verificados)

- **El escudo de la nave solo existe EN BATALLA** (issues #1 y #4, verificado por tres vías): `Shield="0"` en las 26 respuestas de `GetShipByUserId` del log, también en `InspectShipViaShipId`; `get_Shield`/`HasActiveShieldDefense` son de `ShipBattleEntity` y el panel de escudo (`playerShieldPanel`) vive en `BattleSceneUIManager` (HUD de combate); pssapi mapea un único campo `Shield`. `ProduceShield`/`RegenerateShields` son código de batalla que computa el escudo en vivo desde las salas.
- **`ImmunityDate` = inmunidad PvP**: hasta cuándo la nave no puede ser atacada — el juego la llama *"immunity shields"* ("You can use immunity shields to prevent your ship from being attacked while you are away"). Es el único valor de "protección" real fuera de batalla; viaja en el XML del Ship y debe interpretarse en **UTC**. Permanece con `active=False` tras vencer (issue #4).
- **`Hp` del casco es el daño pendiente de reparar** (real, no un bug): no se regenera con el tiempo; se repara con el botón del juego (issue #1).
- **El catálogo de diseños** (`DesignService`): flujo de 3 pasos — `SettingService/GetLatestVersion4` da las versiones actuales de cada catálogo; `ListAllStaticDesigns` (~13.5 MB) trae habitaciones/personajes/naves; `ListAllDynamicDesigns` trae misiones e items. Las respuestas son **base64(gzip(XML))** aunque el Content-Type diga `application/xml`, y **con versiones en 0 la API devuelve el catálogo VACÍO** — hay que pedir primero las versiones del Setting (commit `d7dc189`). Conteos verificados: 554 `CharacterDesign`, 798 `RoomDesign`, 228 `TaskDesign`, 2588 `ItemDesign` (issue #2).
- **Lección de parseo XML** (commit `7a7fc7f`): los atributos usan capitalización exacta (`BattleCharacterHp`, no `BattleCharacterHP`); verificar contra los raw models de pssapi antes de asumir. El nombre del personaje viaja como `CharacterName` (no `Name`).

## 8. Cómo reproducir la extracción

Todo lo anterior salió del binario IL2CPP del cliente de Steam (Linux), `GameAssembly.so`:

1. **IL2CPP dump**: [Il2CppDumper](https://github.com/Perfare/Il2CppDumper) sobre `GameAssembly.so` + `global-metadata.dat` → genera:
   - `dump.cs` — todas las clases/métodos/campos descompilados a declaraciones C#, con offsets de campos y RVAs de métodos.
   - `script.json` — mapa símbolo → RVA/offset para el desensamblado.
   - `stringliteral.json` — todos los string literals del binario (urls de endpoints, mensajes de error, claves).
   - `DummyDll/` + `il2cpp.h` — para navegación en un IDE.
2. **Localizar el símbolo**: buscar en `dump.cs` (p. ej. `get_MaxShield`, `GetStatForLevel`, `TrainingStat`); el RVA del método está a la derecha de la declaración o en `script.json`.
3. **Desensamblar**: `objdump -d --start-address=<RVA> --stop-address=<RVA+N> GameAssembly.so` y leer el ensamblador ARM. Las constantes y strings se resuelven con los offsets GOT (los getters de `Configuration` cargan las claves de checksum desde slots fijos) y `stringliteral.json`.
4. **Verificar empíricamente contra la API**: una fórmula de checksum queda validada cuando la API deja de responder `An error occurred.` y responde un error/éxito de negocio (p. ej. `You have been idle for too long.` con `errorCode=1`).

Artefactos del dump (local del autor): `~/Work/il2cpp_output/` con `dump.cs`, `script.json`, `stringliteral.json`, `il2cpp.h` y `DummyDll/`.

## 9. Referencia rápida de hallazgos → issues

| Hallazgo | Issue / commit |
|---|---|
| Escudo=0 explicado (solo existe en batalla) | #1 (cerrado), commits `bd3430b`, `d9901c6` |
| Stats derivados del catálogo + `GetStatForLevel` | #2 (cerrado), commit `d9aebeb` |
| Caducidad del accessToken (~52 min) + renovación | #3 (cerrado), commits `d9901c6`, `f4ce0fd` |
| `ImmunityDate` = inmunidad PvP en el dashboard | #4, commit `d9901c6` |
| `session_token` estable (uuid5 del email) | #5, commit `d9aebeb` |
| `get_MaxShield` = Σ Capacity×100 y `UpdateStatusPanel` | #8 (comentario de hallazgo) |
| `HpImprovement` / entrenamiento del gym expuesto en el XML | #7 (abierto) |
| Robustez de sesiones (circuit-breaker, vida del refresh) | #9 (abierto) |
| Checksum del HeartBeat4 desensamblado | commit `fe8a0ad` |
| deviceKey determinista (445) / rotación (400 linked_account) | commits `1897ec4`, `f1fa5b7` |
| Catálogo base64-gzip + versiones del Setting | commits `d7dc189`, `f4e27d3` |
