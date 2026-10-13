# Android Hardware Diagnostics Toolkit — Android 15, 16 e 17

Este pacote foi pensado para builds `eng`/`userdebug` e coleta evidência de
proximidade, display, NFC e fingerprint sem assumir paths ou tags específicos
de Samsung, Qualcomm, NXP, Goodix, FPC ou outro fornecedor. O objetivo é
localizar a camada que falhou, não mascarar a falha por injeção artificial.

## Limite importante: AOSP x implementação do fabricante

| Camada | O que o AOSP padroniza | O que varia por aparelho |
| --- | --- | --- |
| Proximidade | `TYPE_PROXIMITY`, evento `on-change`, HAL e semântica perto/longe | Driver, hub, I2C/SPI, thresholds e todos os arquivos `/sys` |
| Display | Serviços de power/display e HWC | DDI, painel, DSI/MIPI, driver DRM e logs I2C |
| NFC | Framework, NCI stack/ABI e HAL NFC | NFCC, firmware, transporte I2C/SPI/UART e tags de log do fornecedor |
| Fingerprint | Framework biométrico e HAL AIDL | Sensor, driver, TEE, firmware e logs OEM |

Não existe, no AOSP, um arquivo Linux oficial como
`/sys/class/sensors/proximity_sensor/state`, nem valores oficiais que possam
ser escritos nele. Se esse caminho existir, ele pertence ao driver/hub do
fabricante. Portanto, `0`, `1`, `5`, `near` e `far` **não são valores AOSP
intercambiáveis** para sysfs.

## Instalação e regra de segurança

```bash
cd android_diagnostics_toolkit
python -m pip install -r requirements.txt
adb devices -l
```

Todos os scripts aceitam `--serial`. Quando há mais de um alvo, eles recusam
executar sem esse argumento. `adbutils` seleciona o serial no servidor ADB e
`subprocess` executa cada comando como lista de argumentos, sem `shell=True`.
Cada coleta preserva stdout, stderr, código de retorno e linha de comando.

Por padrão, os scripts:

- não fazem `adb root` nem reiniciam `adbd`;
- não limpam buffers de logcat;
- não mudam `setprop`, configurações ou serviços;
- não fazem `am broadcast`, `service call` ou `cmd` inventado;
- não escrevem em `/sys`.

Os resultados ficam em `./artifacts/`, cada execução em uma pasta nova.
Eles podem conter fingerprint do build, propriedades do aparelho e dados de
depuração; trate-os como material interno.

## Fluxo recomendado

1. Capture uma linha de base do aparelho:

   ```bash
   python collect_snapshot.py --serial SERIAL
   ```

2. Para uma chamada/uso de proximidade, inicie a vigilância e então reproduza
   o cenário real (cobrir e descobrir o sensor, ou usar uma chamada real):

   ```bash
   python monitor_proximity.py --serial SERIAL --duration 90 --interval 0.25
   ```

3. Para flicker, green line ou apagamento ligado à proximidade:

   ```bash
   python display_probe.py --serial SERIAL --duration 90
   ```

4. Para leitura de uma TAG real:

   ```bash
   python nfc_probe.py --serial SERIAL --duration 45
   ```

5. Para um fluxo real de autenticação biométrica:

   ```bash
   python fingerprint_probe.py --serial SERIAL --flow authenticate --duration 45
   ```

O arquivo `logcat.raw.txt` nunca usa o filtro local para descartar linhas. O
arquivo `logcat.filtered.txt` é apenas uma visão prática de tags/termos comuns.
Se o build já tiver tags confirmadas, é possível passar um filter-spec oficial:

```bash
python capture_logcat.py --serial SERIAL --category nfc --duration 60 \
  --filter-spec 'NfcService:D NfcDispatcher:D *:S'
```

Antes disso, verifique as tags verdadeiras no `logcat.raw.txt`; OEMs podem usar
nomes diferentes.

---

## 1. Sensor de proximidade: como o AOSP define o comportamento

O contrato atual do AOSP para `TYPE_PROXIMITY` é:

- modo de relatório **on-change**;
- normalmente versão **wake-up**;
- leitura em centímetros no evento `sensors_event_t.distance`;
- em hardware binário, **far** é `maxRange` e **near** é qualquer valor menor
  que `maxRange`.

Ao ativar um sensor on-change, a HAL deve emitir imediatamente um evento. Esse
é o contrato entre framework e HAL; não é um contrato de sysfs. A pilha é:

```text
Sensor físico / hub / driver Linux (vendor)
        ↓
Sensors HAL (AIDL/HIDL, vendor)
        ↓
SensorService / SensorManager (AOSP)
        ↓
DisplayPowerProximityStateController / DisplayPowerController (AOSP)
        ↓
política de chamada, power e display
```

O `DisplayPowerController` tem um controlador específico para estado de
proximidade e decide o efeito na tela dentro de uma requisição de energia. Logo,
um evento correto não prova sozinho que a tela deverá apagar: a política de
chamada/PowerManager também precisa ter solicitado o comportamento de
screen-off por proximidade.

### O que observar primeiro

`monitor_proximity.py` salva:

- `0000_dumpsys_sensorservice.stdout.txt`: lista/estado que o framework vê;
- trechos de proximidade extraídos desse dump;
- `dumpsys power` antes, durante e depois;
- `sysfs_candidates.json`: nós Linux candidatos e valores iniciais;
- `proximity_samples.csv`: amostras contínuas dos nós encontrados.

Sequência prática:

1. Rode `--discover-only` e abra `sysfs_candidates.json`.
2. Sem mudar o build, cubra e descubra fisicamente o sensor algumas vezes.
3. Compare as transições em `proximity_samples.csv` com o `sensorservice`.
4. Só então mapeie quais valores de **cada aparelho** representam perto/longe.

Interpretação inicial:

| Observação | Direcionamento provável |
| --- | --- |
| Nó bruto muda, mas `sensorservice` não mostra proximidade coerente | HAL, sensor hub, calibração/threshold ou mapeamento do driver para HAL |
| `sensorservice` alterna entre near/far, mas tela não apaga durante chamada | Política de power/telephony/display, e não necessariamente o sensor |
| Nem nó bruto nem framework mudam | Alimentação, IRQ, I2C/SPI, firmware/hub ou driver |
| Nenhum `TYPE_PROXIMITY` aparece no framework | Registro/lista da Sensors HAL, configuração de produto ou driver ausente |

### Sobre arquivos sysfs e escrita

Os locais mais comuns que o script **somente inspeciona** são:

```text
/sys/class/sensors/<vendor_proximity>/
/sys/devices/virtual/sensors/<vendor_proximity>/
/sys/bus/iio/devices/iio:device*/in_proximity_*
```

Eles não são definidos pelo Android. Em especial:

- `enable=1` costuma somente ligar a aquisição; não significa `near`;
- `raw_data` normalmente é uma leitura bruta e pode ser somente leitura;
- `state`, `value` e `in_proximity_raw` podem ter unidade/semântica próprias;
- uma linha `threshold` pode ser calibração, não um injetor de evento.

Há uma escrita protegida apenas para um nó que a descoberta leu e classificou
como valor. Ela serve para um **nó de depuração que o fornecedor já documentou**:

```bash
python monitor_proximity.py --serial SERIAL \
  --write /sys/class/sensors/proximity_sensor/state 0 \
  --confirm-vendor-sysfs-write
```

Antes/depois, stderr e retorno ficam em
`vendor_sysfs_write_transaction.json`. Não use essa opção para tentar escrever
em `enable`, `threshold`, EEPROM/calibração ou em um path ainda não confirmado.
O script não tenta `adb root`; em um `eng` build, faça isso conscientemente em
outra sessão se o SELinux/permissão impedir a leitura.

### Automatização sem telefone físico

Para testar **framework/app**, e não driver, o Android Emulator possui sensor
de proximidade virtual e console documentado. Crie um AVD com *Proximity
Sensor* habilitado e use:

```bash
python emulator_sensor_inject.py --serial emulator-5554 --sensor proximity \
  --value VALOR_EXIBIDO_POR_EMU_HELP_SENSOR --dry-run
```

Após conferir `emu_help_sensor.stdout.txt`, remova `--dry-run`. O script se
recusa a operar em serial físico. O formato do valor vem do próprio console do
emulador; assim ele não inventa uma codificação de proximidade.

---

## 2. NFC: o que é possível rastrear — e o que ADB não consegue fabricar

O caminho típico de uma TAG é:

```text
TAG / campo RF (NFC-A, NFC-B, NFC-F ou NFC-V)
        ⇅
NFCC (controlador NFC)
        ⇅  NCI
HAL NFC AIDL: android.hardware.nfc.INfc/default
        ⇅
libnfc_nci + libnfc_nci_jni + NfcNci.apk / NfcService
        ↓
dispatcher, TagService, NDEF/tecnologias e app selecionado
```

NCI é a interface usada para conversar com o NFCC. Os comandos NCI Android
proprietários usam GID `0xF`; eles são mensagens entre a pilha NFC e o
controlador, **não** comandos públicos de `adb shell`.

No Android 16, o AOSP introduziu o módulo Mainline opcional **NFC Services**.
Ele inclui `framework-nfc.jar`, `NfcNci.apk`, `libnfc_nci_jni.so` e
`libnfc_nci.so`. Por isso, em Android 16/17 não assuma que depurar somente um
APK fixo da partição `system` cobre a implementação instalada: comece por
`dumpsys nfc`, build fingerprint e logcat.

### Estímulo artificial: limite correto

Não há uma API pública de ADB que injete uma descoberta RF/NCI em um **NFCC
físico**. Enviar manualmente `ACTION_TAG_DISCOVERED`, `ACTION_NDEF_DISCOVERED`
ou um broadcast para um app não verifica a antena, o controlador, o HAL, a
pilha NCI nem o dispatcher real. Do mesmo modo, escrever bytes em um device
node desconhecido pode corromper o estado do controlador.

Portanto, `nfc_probe.py` só observa uma TAG física conhecida. Ele registra:

| Evidência | Camada coberta |
| --- | --- |
| `before/after_dumpsys_nfc` | NfcService e estado framework exposto |
| `logcat.raw.txt` | Framework e logs de HAL/OEM que chegam ao logd |
| `logcat.filtered.txt` | Recorte por NFC/NCI/NDEF para correlação rápida |
| horário de aproximação da TAG | Correlação com a entrada NCI/logs |

Para uma automação realmente end-to-end, use um fixture/atuador RF que aproxime
uma TAG real de modo repetível. Para testar somente uma app, use mock da API
NFC no próprio app; deixe claro que esse teste não cobre o aparelho físico.

Direcionamento inicial:

| Sintoma | Próxima camada a isolar |
| --- | --- |
| Serviço `nfc` não aparece em `dumpsys -l` | produto, feature/configuração, módulo NFC ou boot do serviço |
| Serviço existe, mas não há evidência de RF/NCI ao aproximar TAG | antena, alimentação, firmware NFCC, transporte e HAL vendor |
| Há ativação/tecnologia no stack, mas app não recebe a TAG | NfcService/dispatcher, modo leitor, app/intents e permissões |
| Após uma leitura não detecta mais TAG | compare build/QPR; há correção Android 17 QPR3 Beta 1 para um `DeadObjectException` desse tipo |

---

## 3. Fingerprint: touch, captura, match e serviço não são a mesma coisa

Em implementações modernas AIDL, `IFingerprint` fornece propriedades de sensor
e cria uma `ISession` por sensor/usuário. A sessão chama operações como
`authenticate` e comunica resultado por `ISessionCallback`:

```text
Keyguard ou BiometricPrompt
        ↓
BiometricService (serviço "biometric") + FingerprintService ("fingerprint")
        ↓
IFingerprint / ISession (HAL AIDL; pode haver adaptador legado HIDL)
        ↓
driver, firmware, TEE e sensor físico
        ↑
onAcquired → onAuthenticationSucceeded / onAuthenticationFailed / onError
```

`onAcquired` indica estado/qualidade de captura, não um match. Uma tentativa
rejeitada dispara `onAuthenticationFailed` e não encerra por si só a operação;
aceite ou erro encerram o ciclo. Já `detectInteraction` apenas procura uma
imagem semelhante a impressão digital, não autentica e não altera lockout.

`fingerprint_probe.py` não tenta forjar nenhum desses callbacks. Durante a
janela, faça uma autenticação já autorizada por lockscreen ou por um app de
teste que usa `BiometricPrompt`. Depois compare dumps de antes/depois e o
logcat bruto.

| Evidência | Hipótese que ganha força |
| --- | --- |
| `fingerprint` ausente em `dumpsys -l` | serviço/integração de produto não publicado |
| serviço existe, mas dump indica nenhum sensor/autenticador | registro HAL/VINTF, configuração de sensor ou falha no início da HAL |
| sensor é listado, mas não há aquisição ao tocar | alimentação/IRQ/driver, HAL session ou UDFPS/display handshake |
| aquisição ocorre, mas não há resultado terminal | HAL/TEE, scheduler, timeout/cancelamento ou resposta vendor |
| `onAuthenticationFailed` / lockout | sensor alcançou matching; investigar template/TEE/condição de leitura, não "serviço morto" |

Para desenvolvimento de AOSP/AVD, existe uma **Virtual Fingerprint HAL** de
debug. A própria fonte AOSP informa que ela é ignorada se uma HAL real estiver
instalada e que a seleção requer build/configuração de debug e reboot. Não use
as propriedades `vendor.fingerprint.virtual.*` deste exemplo como diagnóstico
de um aparelho com HAL real de produção.

---

## Mudanças e cuidados nas gerações recentes

| Versão | Impacto prático para este kit |
| --- | --- |
| Android 15 | As notas QPR documentam teste em páginas de 16 KB. Para HALs/daemons nativos, registre sempre build fingerprint e arquitetura; não existe nova ABI sysfs AOSP para proximidade. |
| Android 16 | NFC Services passou a ser módulo Mainline atualizável opcional. A própria release note também registrou falhas de eventos de toque simulados por ADB/acessibilidade em builds preview; não use `input tap` como prova do caminho físico. |
| Android 17 | A versão inicial foi liberada em 16/06/2026. As notas do QPR3 Beta 1 (02/10/2026) listam correção para NFC parar após a primeira TAG por `DeadObjectException` e para congelamento no desbloqueio por fingerprint. Colete build/QPR exato antes de atribuir a causa ao hardware. |

## Por que os comandos são válidos

Os scripts se limitam a interfaces documentadas do Android:

- `adb -s SERIAL ...` para selecionar explicitamente o destino;
- `adb shell <comando>` para executar comando único no aparelho;
- `adb shell dumpsys -l` e `adb shell dumpsys <serviço>` para diagnóstico de
  serviços;
- `adb logcat --help` consultado no próprio aparelho, pois opções variam por
  versão;
- `adb logcat <tag:priority ...>` para filtros de tag documentados.

`dumpsys` é diagnóstico, não uma API textual estável. Por isso nenhum script
declara sucesso por regex em dump: ele salva a saída completa, seu stderr e o
retorno para que a análise acompanhe a versão do build.

## Fontes oficiais consultadas

- [AOSP — Sensor types / proximity](https://source.android.com/docs/core/interaction/sensors/sensor-types)
- [AOSP — Sensor stack e responsabilidade do driver](https://source.android.com/docs/core/interaction/sensors/sensor-stack)
- [AOSP — Sensors AIDL HAL](https://source.android.com/docs/core/interaction/sensors/sensors-aidl-hal)
- [AOSP — Reporting modes](https://source.android.com/docs/core/interaction/sensors/report-modes)
- [AOSP — NFC Services Mainline (Android 16)](https://source.android.com/docs/core/ota/modular-system/nfc-services)
- [AOSP — Android proprietary NCI commands](https://source.android.com/docs/core/connect/android-proprietary-nci-commands)
- [AOSP source — NFC HAL manifest example](https://android.googlesource.com/platform/hardware/interfaces/+/android16-release/nfc/aidl/default/nfc-service-example.xml)
- [AOSP source — Fingerprint `IFingerprint` AIDL](https://android.googlesource.com/platform/hardware/interfaces/+/refs/heads/main/biometrics/fingerprint/aidl/android/hardware/biometrics/fingerprint/IFingerprint.aidl)
- [AOSP source — Fingerprint `ISessionCallback` AIDL](https://android.googlesource.com/platform/hardware/interfaces/+/refs/heads/main/biometrics/fingerprint/aidl/android/hardware/biometrics/fingerprint/ISessionCallback.aidl)
- [AOSP source — Virtual Fingerprint HAL README](https://android.googlesource.com/platform/hardware/interfaces/+/refs/heads/main/biometrics/fingerprint/aidl/default/README.md)
- [Android Developers — ADB](https://developer.android.com/tools/adb)
- [Android Developers — dumpsys](https://developer.android.com/tools/dumpsys)
- [Android Developers — logcat](https://developer.android.com/tools/logcat)
- [Android Developers — controles de sensor e fingerprint do Emulator](https://developer.android.com/studio/run/emulator-extended-controls)
- [Android Developers — Android 15 release notes](https://developer.android.com/about/versions/15/release-notes)
- [Android Developers — Android 16 release notes](https://developer.android.com/about/versions/16/release-notes)
- [Android Developers — Android 17 QPR3 release notes](https://developer.android.com/about/versions/17/qpr3/release-notes)
