# Chrome ligero: página vacía + `--disable-extensions`

Técnica para usar un Chrome real (CDP/Playwright) solo como "portador" de la
sesión de un sitio (cookies, `localStorage`, token del WAF) y llamar a su API
HTTPS desde la página, sin cargar la aplicación web. Medido en
chat-deepseek-api (2026-11, Windows 11, Chrome 158, headless).

## Resultado

| Escenario (headless) | Procesos | RAM privada | CPU en reposo (20 s) |
|---|---|---|---|
| App completa + extensiones del sistema | 15 | ~1,1 GB | 2–3 s |
| App completa + `--disable-extensions` | 12 | ~430 MB | 0,4 s |
| Página vacía + `--disable-extensions` | 9 | ~290 MB | ~0 |

Cada llamada a la API tardó ~0,45 s desde la página vacía.

## 1. `--disable-extensions` (lo que más ahorra)

En Windows, programas como Adobe Acrobat o Internet Download Manager instalan
extensiones "del sistema" que Chrome carga en **todos** los perfiles, también
en un `--user-data-dir` nuevo y en headless. En la medición, la de Acrobat
(`efaidnbmnnnibpcajpcglclefindmkaj`, con un iframe `acrobat.adobe.com/.../ajs-worker.html`)
ocupaba un renderer de ~600 MB.

```python
args = [chrome, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check",
        "--disable-extensions"]
```

Para ver qué carga el perfil: `GET http://127.0.0.1:<port>/json/list` (tipos
`background_page`, `service_worker`, `iframe` con `chrome-extension://...`) o la
carpeta `<perfil>/Default/Extensions`.

Ojo: si ya hay un Chrome escuchando en el puerto y el cliente se adjunta a él,
los flags de arranque no aplican.

## 2. Página vacía del mismo origen

Para que `fetch` lleve las cookies del sitio y se pueda leer su `localStorage`,
la página debe tener el **mismo origen**. Navegar a `/favicon.ico` u otra ruta
no sirve si el sitio es una SPA con fallback: responde la app entera en
cualquier ruta (pasó en chat.deepseek.com). Lo que funciona es que Playwright
sirva el documento sin pedirlo a la red:

```python
BLANK = f"{BASE_URL}/__blank"
await page.route(BLANK, lambda r: r.fulfill(
    status=200, content_type="text/html", body="<!doctype html><title>x</title>"))
await page.goto(BLANK)

# Las peticiones sí van a la red real, con las cookies del perfil.
res = await page.evaluate("""async (path) => {
  const raw = localStorage.getItem('userToken');
  let token = raw; try { token = JSON.parse(raw).value || token; } catch (e) {}
  const r = await fetch(path, { headers: { authorization: 'Bearer ' + token } });
  return { status: r.status, body: await r.text() };
}""", "/api/v0/chat_session/fetch_page")
```

Solo se intercepta la URL exacta de `BLANK`; el resto del tráfico no se toca.

## Límites

- Solo sirve para endpoints que no necesitan código de la app. En DeepSeek,
  `chat/completion` exige `x-ds-pow-response` (prueba de trabajo resuelta por el
  JS/wasm de la web), así que el chat sigue necesitando la app completa; el
  historial (`chat_session/fetch_page`, `delete`, `delete_all`) y
  `create_pow_challenge` funcionan con la página vacía.
- El login, el desafío del WAF y cualquier token que la app renueve sola
  requieren cargar la app al menos una vez; la página vacía solo reutiliza lo
  que ya está guardado en el perfil.
- La sesión debe existir antes (perfil ya logueado).

## Cómo medir

Sumar los procesos `chrome.exe` cuyo proceso raíz tenga el `--user-data-dir`
del perfil (filtrar por `Name='chrome.exe'` antes de recorrer el árbol de
`ParentProcessId`, o se cuelan procesos ajenos por reutilización de PID) y
mirar `PrivateMemorySize64`; el working set suma memoria compartida dos veces.
Desglosar por `--type=` (`renderer`, `gpu-process`, `utility`) y
`--extension-process` para ver quién consume.
