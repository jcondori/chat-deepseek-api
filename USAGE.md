# Uso de chat-deepseek-api (cliente con navegador)

Cliente no oficial de **chat.deepseek.com** (la web gratuita, no la API de pago `api.deepseek.com`). Maneja un Chrome real, así que la web resuelve el login, el antibots y la prueba de trabajo. Usar `DeepseekBrowserAPI`; el cliente HTTP `DeepseekAPI` ya no funciona (el antibots lo bloquea en el login).

- Repo: `E:\Documents\otherprojects\chat-deepseek-api`
- Python: `E:\Documents\otherprojects\chat-deepseek-api\.venv\Scripts\python.exe` (ya tiene todo instalado)
- Perfil de Chrome con la sesión iniciada: `<repo>\.chrome-profile\` (ignorado por git; no copiarlo ni compartirlo)

## Desde la línea de comandos

```powershell
cd E:\Documents\otherprojects\chat-deepseek-api
.venv\Scripts\python examples\browser_app.py --headless "tu pregunta"
```

- Imprime la respuesta en streaming por stdout y `(chat id: <uuid>)` por stderr.
- `--think`: activa DeepThink; el razonamiento sale con el prefijo `[thinking]` y la respuesta con `[answer]`.
- `--search`: activa la búsqueda web.
- Sin pregunta: abre un chat interactivo (`> `) que mantiene el contexto entre mensajes.
- Sin `--headless`: se ve la ventana de Chrome.
- `--list-chats`: lista los chats guardados (`id  título`, con `*` los fijados).
- `--delete-chat <id> [<id>...]`: borra esos chats.
- `--delete-all-chats`: borra **todos** los chats de la cuenta; pide escribir `yes` (o `--yes` para no preguntar). Es irreversible: no ejecutarlo sin que el usuario lo pida expresamente.
- Para preguntas con tildes o ñ, usar `$env:PYTHONIOENCODING='utf-8'` (PowerShell) o `PYTHONIOENCODING=utf-8` (Git Bash).

## Desde Python

```python
import asyncio
from chat_deepseek_api.browser_api import DeepseekBrowserAPI
from chat_deepseek_api.stream_parser import THINKING

PROFILE = r"E:\Documents\otherprojects\chat-deepseek-api\.chrome-profile"

async def main():
    app = await DeepseekBrowserAPI.create(profile_dir=PROFILE, headless=True)
    try:
        await app.new_chat()                       # chat vacío
        respuesta = ""
        async for chunk in app.chat("¿Cuánto es 17*23?", thinking_enabled=True):
            delta = chunk.choices[0].delta         # delta.type: "thinking" o "text"
            if delta.type != THINKING:
                respuesta += delta.content
        print(respuesta, "| chat:", app.chat_id)

        # Otra pregunta en el mismo chat (conserva el contexto):
        async for chunk in app.chat("¿Y por 2?"):
            print(chunk.choices[0].delta.content, end="")
    finally:
        await app.close()

asyncio.run(main())
```

Ejecutar con el Python del `.venv`, o desde otro entorno con `pip install -e "E:\Documents\otherprojects\chat-deepseek-api[browser]"`.

Siempre pasar `profile_dir=PROFILE`: si se omite, la librería usa `~/.chat-deepseek-api/chrome-profile`, que no tiene sesión.

### Referencia

| Función | Qué hace |
|---|---|
| `await DeepseekBrowserAPI.create(profile_dir=..., headless=True)` | Abre Chrome y comprueba la sesión. También sirve `async with DeepseekBrowserAPI(...) as app:` |
| `await app.new_chat()` | Abre un chat vacío. El id se conoce después del primer mensaje, en `app.chat_id` |
| `await app.open_chat(chat_id)` | Abre un chat existente |
| `app.chat(msg, id=None, thinking_enabled=None, search_enabled=None)` | Generador async de `MessageData`. `id` abre ese chat antes de enviar; `None` sigue el chat abierto. `None` en las opciones deja lo que tenga la interfaz |
| `await app.list_chats(limit=None)` | Lista de chats guardados (dicts con `id`, `title`, `pinned`, `updated_at`), los más recientes primero |
| `await app.delete_chat(id_o_lista)` | Borra uno o varios chats (irreversible) |
| `await app.delete_all_chats()` | Borra todos los chats de la cuenta, incluidos los fijados (irreversible) |
| `await app.close()` | Cierra Chrome (con `keep_open=True` lo deja abierto para que la siguiente ejecución se conecte por el puerto 9333) |

Otros parámetros de `create`: `response_timeout` (s, por defecto 300), `dump_dir` (guarda el stream crudo), `verbose_level`, `keep_open`, `cdp_port`.

## Límites

- **Una pregunta a la vez** por instancia (una pestaña); las llamadas a `chat()` concurrentes esperan en cola.
- Cada ejecución del CLI abre y cierra Chrome (unos segundos). Para muchas preguntas, reutilizar una instancia desde Python.
- `parent_message_id` se ignora: la web siempre continúa desde el último mensaje del chat abierto.
- No usar el mismo perfil desde dos procesos a la vez: Chrome no lo permite.
- Es la web gratuita: puede haber límites de uso, y automatizarla va contra los términos de DeepSeek. Usarlo con moderación.

## Problemas comunes

| Error | Solución |
|---|---|
| `Not logged in and Chrome is headless...` | La sesión venció. El usuario debe ejecutar `.venv\Scripts\python examples\browser_app.py --login`, iniciar sesión en la ventana y cerrarla (requiere a una persona: no hacerlo en automático). |
| `Chrome exited at startup...` | Hay otro Chrome usando `.chrome-profile`: cerrarlo y reintentar. |
| `TimeoutError: No response from DeepSeek` | La web no respondió o cambió. Repetir sin `--headless` para ver la página, y con `--dump <carpeta>` para revisar el stream. |
| Respuesta vacía o texto raro | Probablemente cambió el formato del stream: revisar con `--dump` y ajustar `chat_deepseek_api/stream_parser.py`. |
