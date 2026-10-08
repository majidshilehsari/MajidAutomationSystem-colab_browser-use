# Colab Browser Computer Use

Run a graphical Chrome browser inside Google Colab, watch it from another
computer through a temporary HTTPS link, and control it from an agent using
screenshots plus mouse and keyboard commands.

This project is intended for experiments, demonstrations, and disposable
agent-browser sessions. It is not a production remote-desktop service.

## What it provides

- A 1366x768 virtual Linux desktop in Colab
- Google Chrome with software WebGL support
- A password-protected noVNC interface exposed through a temporary Cloudflare
  Quick Tunnel
- Mouse, keyboard, clipboard, scrolling, window, and drag controls
- A screenshot directory for visual agent feedback
- An automation sidebar inside the noVNC page: record clicks and keystrokes,
  build a step list, and run it on the server so it keeps going after you close
  your own browser
- Page detection with a memory of every page you detected, plus copy-paste
  prompts for handing that state to an AI assistant
- A supervisor that keeps the desktop services running and relaunches Chrome
  if it closes
- A detailed [agent operating guide](agent.md)

## Architecture

```text
Human browser
    |
    | HTTPS link + temporary VNC password
    v
Cloudflare Quick Tunnel
    |
    v
automation/server.py (localhost:6080)
    |  serves noVNC + the sidebar, answers /automation/api, proxies /websockify
    v
x11vnc (localhost:5901)
    |
    v
Xvfb display :1  --->  Fluxbox  --->  Google Chrome
    ^
    |
browser_control.sh
    ^
    |
    +-- Agent screenshot -> inspect -> act -> verify loop
    +-- automation/engine.py runs saved flows here, in the Colab runtime
```

Chrome is also started with `--remote-debugging-port=9222` on localhost, which
is how the detector reads the page URL, title, visible text, and the interactive
elements with their coordinates.

VNC and noVNC listen only on localhost. The temporary Cloudflare tunnel is the
external transport used by a human observer. An agent running in the same
Colab runtime controls X display `:1` directly and does not need the public URL
or VNC password.

## Requirements

- A Google Colab runtime or a compatible Ubuntu AMD64 environment
- Root access for package installation
- Internet access for installing Chrome, Cloudflare Tunnel, and dependencies
- A terminal or notebook cell that can remain running while the browser is in
  use

The installer currently downloads AMD64 builds and is not designed for ARM
runtimes.

## Quick start in Google Colab

Clone the repository into `/content` and replace `YOUR_USERNAME` with the
GitHub account or organization that hosts it:

```bash
cd /content
git clone https://github.com/HoussemMouradi/colab_browser-use.git
cd colab_browser-use
chmod +x *.sh
./install.sh
```

Start the browser stack:

```bash
./start_colab_browser.sh --wait
```

Keep that command running. It prints values similar to:

```text
BROWSER_URL=https://example.trycloudflare.com/vnc.html?autoconnect=true&resize=scale&path=websockify
VNC_PASSWORD=temporary-password
AUTOMATION_TOKEN=temporary-token
STATE_DIR=/content/colab_browser-computer-use/.runtime
```

Open `BROWSER_URL` on another computer and enter `VNC_PASSWORD` when prompted.
All three values are temporary. A new tunnel URL, VNC password, and automation
token are generated on each start. Share none of them.

## Agent control

The recommended interaction pattern is:

1. Capture a screenshot.
2. Inspect the screenshot and confirm the active window.
3. Perform one small action.
4. Capture another screenshot and verify the result.
5. Repeat until the task is complete.

Example:

```bash
./browser_control.sh screenshot before.png
./browser_control.sh active
./browser_control.sh windows
./browser_control.sh focus 'Google Chrome'
./browser_control.sh click 500 400 left
./browser_control.sh paste 'cute panda'
./browser_control.sh key Return
./browser_control.sh screenshot after.png
```

Screenshots are stored in `./screen_shots/`, even when the supplied filename
contains another path.

Common commands:

```text
./browser_control.sh screenshot [FILE]
./browser_control.sh position
./browser_control.sh move X Y
./browser_control.sh click X Y [left|middle|right|1-5]
./browser_control.sh doubleclick X Y
./browser_control.sh drag X1 Y1 X2 Y2 [left|middle|right]
./browser_control.sh type TEXT
./browser_control.sh paste TEXT
./browser_control.sh key KEY...
./browser_control.sh scroll AMOUNT
./browser_control.sh url URL
./browser_control.sh windows
./browser_control.sh active
./browser_control.sh focus TITLE
```

See [agent.md](agent.md) for command behavior, safety rules, coordinate
guidance, troubleshooting, and a complete agent workflow.

## Automation sidebar

The noVNC page carries a collapsible panel on the right edge, mirroring the
noVNC control bar on the left. Open it with the arrow on the right, then paste
`AUTOMATION_TOKEN` into the AI tab once; the panel remembers it in your browser.

The panel has five tabs:

| tab | what it does |
| --- | --- |
| Flow | the step list: add, edit, reorder, enable, duplicate, delete, save, load |
| Record | capture real clicks and keystrokes into steps |
| Pages | detect the current page and browse the pages you already detected |
| AI | copy the guide, copy a full prompt, paste back what the AI returned |
| Log | the live log of the running flow |

### Step types

`click`, `double_click`, `drag`, `move`, `scroll`, `type`, `paste`, `key`,
`wait`, `wait_for_text`, `goto_url`, `focus_window`, `screenshot`. Every step
also accepts `label`, `note`, `enabled`, `delayAfterMs`, `requiresConfirmation`,
and `continueOnError`.

Coordinates are desktop pixels of the fixed 1366x768 display, so a recorded
click stays valid no matter how your noVNC window is scaled.

### Running without your browser

Steps execute on the Colab runtime through `browser_control.sh`, not in your
tab. Once a flow is running you can close the noVNC page, or your whole browser,
and it continues to the end as long as the Colab cell that started the stack
stays alive.

A step marked `requiresConfirmation` pauses the run and waits for a human. If
nobody is watching, it waits until the run is stopped. Use it before anything
that submits, sends, buys, or deletes.

### Detecting a page for an AI

`Pages > Detect page` stores a screenshot, the window list, the page URL and
title, the visible text, and up to 250 interactive elements with a CSS selector
and desktop coordinates. Every detection is remembered under the page's host and
path, so you can come back to it later.

The AI tab turns that into two things you paste into a chat:

1. **Copy general guide** — a standalone document explaining the environment,
   the coordinate system, every step type, and the rules the AI must follow.
   Send this first. It is also `automation/ai_guide.md`.
2. **Copy full prompt** — the guide plus the detected page plus your current
   flow, with instructions to answer in JSON.

Paste the AI's answer into **Import AI output**. The JSON is validated against
the same schema the runner uses, unknown step types are dropped with a warning,
and the result becomes a normal flow you can edit before running.

### API

The panel is a thin client over a JSON API on the same port as noVNC:

```text
GET    /automation/api/info            capabilities and viewport
GET    /automation/api/status          current run, index, log tail
POST   /automation/api/run             validate a flow, then start it
POST   /automation/api/control         pause, resume, stop, confirm
GET    /automation/api/flows           list saved flows
GET    /automation/api/flows/NAME      read one flow
PUT    /automation/api/flows/NAME      save one flow
DELETE /automation/api/flows/NAME      delete one flow
GET    /automation/api/runs/ID/log     replay the log of a finished run
POST   /automation/api/screenshot      capture the desktop now
GET    /automation/api/artifact?path=  read a PNG from the runtime directory
POST   /automation/api/detect          detect the current page
GET    /automation/api/pages           detected page memory
GET    /automation/api/pages/ID        one detection in full
GET    /automation/api/guide           the AI guide as markdown
POST   /automation/api/prompt          assemble the full AI prompt
```

Every route except `info` needs the `X-Automation-Token` header.

### Screenshots and extracted texts

Everything the system captures is indexed under `<data>/archive/`, so both stay
available after a run instead of being loose files on disk:

- `shots.json` — every screenshot, from any route: Detect, a `screenshot` step,
  the automatic one on failure, or the manual button. Each record has the file
  name, a timestamp, where it came from, the step that produced it, and the
  image size.
- `texts.json` — every `capture_text` result plus notes saved by hand.

Two tabs list them. **Screenshots** shows a thumbnail with date and time,
newest first, and a link that opens the image at
`/automation/api/public/shot/<name>` without a token. **Extracted texts** lists
each saved text with its source and length, and has a box for saving your own.

The matching routes, all behind `X-Automation-Token` except the image itself:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/automation/api/shots` | list screenshot records, newest first |
| `DELETE` | `/automation/api/shots/<id>` | drop one record |
| `GET` | `/automation/api/texts` | list saved texts |
| `POST` | `/automation/api/texts` | save a text, `{"text": "..."}` |
| `DELETE` | `/automation/api/texts/<id>` | drop one text |

Indexes are capped (500 shots, 300 texts) and written through a temporary file,
so a runtime that dies mid-write cannot leave a truncated index behind.

### Dedicated panel page

The same controls are also available as a standalone page, away from the noVNC
viewer:

```
https://<tunnel-url>/automation/panel.html
```

It talks to the same API on the same port, so it drives the same Chrome on the
same virtual display, and runs survive closing either tab. The page is full
width, which suits long flows better than the 344 px rail.

Everything works there except click recording, which needs the browser view to
click on; the Record tab says so and links across. A `⧉ Full panel` link in the
sidebar header opens this page, and `🖥 Browser view` goes back.

Like every other route except `GET /info` and `GET /automation/api/public/shot/*`,
it sits behind the tunnel, and the API calls it makes still need
`X-Automation-Token`.

## Stopping the environment

Press `Ctrl+C` in the terminal running the supervisor, or run:

```bash
./stop_colab_browser.sh
```

The stop script only targets processes recorded in this project's runtime
directory.

## Repository layout

```text
colab_browser-computer-use/
├── README.md                 # GitHub project documentation
├── agent.md                  # Detailed instructions for an operating agent
├── install.sh                # Dependency installer
├── start_colab_browser.sh    # Desktop, browser, VNC, and tunnel launcher
├── stop_colab_browser.sh     # Safe workflow shutdown
├── browser_control.sh        # Screenshot and input controls
├── run_tests.sh              # Runs every check in this repository
├── automation/
│   ├── server.py             # noVNC + sidebar + API + VNC proxy on one port
│   ├── engine.py             # Runs flows on the server through browser_control.sh
│   ├── api.py                # JSON routes, independent of the transport
│   ├── detect.py             # Page detection and the detected page memory
│   ├── cdp.py                # Minimal Chrome DevTools Protocol client
│   ├── schema.py             # Flow validation, shared by UI, API, and AI
│   ├── ai_guide.md           # The guide you paste into an AI first
│   └── static/               # Sidebar: automation.js, core.mjs, automation.css
├── tests/                    # Python unittest and node --test suites
├── screen_shots/             # Generated screenshots, ignored by Git
└── .runtime/                 # Profile, logs, PIDs, VNC data, flows, runs, pages
```

`.runtime/automation/` holds the flows you saved, one directory per finished run
with its log and screenshots, and the detected page memory.

`.runtime/` is created only after startup and is ignored by Git.

## Security notes

- Use this only in an isolated, disposable Colab runtime.
- Chrome runs as root with `--no-sandbox`, and software WebGL uses
  `--enable-unsafe-swiftshader`. These flags reduce browser isolation.
- Do not use the environment for sensitive personal, financial, medical, or
  production accounts.
- Treat the noVNC URL as public and share neither it nor the VNC password.
- Treat `AUTOMATION_TOKEN` the same way. It is what authorizes the automation
  API, and that API can drive the browser. The token is never embedded in the
  served page: you paste it into the panel yourself.
- Never commit `.runtime/`, screenshots containing private data, browser
  profiles, cookies, or credentials.
- Let a human enter passwords and complete CAPTCHAs. An agent should not ask
  for passwords in chat or attempt to bypass a CAPTCHA.
- Require human confirmation before purchases, messages, submissions,
  deletions, permission changes, or other consequential actions.

## Limitations

- Colab runtimes can disconnect or be reclaimed at any time.
- Cloudflare Quick Tunnel URLs have no uptime guarantee and change after a
  restart.
- GUI automation is coordinate-based and must be verified with fresh
  screenshots after interface changes.
- Sites may show CAPTCHAs or reject traffic from shared cloud IP addresses.
- Software rendering is slower than a local hardware-accelerated desktop.
- Browser profiles and generated files disappear when the Colab runtime is
  deleted unless they are copied elsewhere.

## Troubleshooting

### Connection error

The temporary tunnel probably expired. Restart the stack and use the newly
printed URL and VNC password.

### Port 5901 or 6080 is already occupied

Another VNC/noVNC stack is running. Stop it before starting this project.

### No X desktop is available

Run `./start_colab_browser.sh --wait` and keep that process alive.

### The top of the browser is cut off

`start_colab_browser.sh` pins the Chrome window to the top-left corner after the
window manager maps it (`fix_chrome_geometry`), leaving room for the fluxbox
toolbar. It is best effort: if your window manager still clips the tab strip,
adjust the `-e 0,0,0,1366,744` line in that function.

### A click lands in the wrong place

Capture a fresh screenshot and confirm the active window. noVNC scaling does
not change the underlying 1366x768 desktop coordinates.

### Logs

Runtime logs are written to:

```text
.runtime/logs/
```

## Contributing

Issues and pull requests are welcome. Useful contributions include improved
browser lifecycle handling, additional input primitives, tests, alternative
tunnel providers, and support for more Linux architectures.

## Tests

```bash
./run_tests.sh
```

This runs the Python unit and integration tests, the JavaScript unit tests for
the sidebar logic, and syntax checks for the shell scripts, the Python package,
and the sidebar script.

The Python integration tests start the real server against a stand-in RFB server
and drive it with a real HTTP client and a real WebSocket client, so the VNC
proxy path is covered too. They need the websockify Python module and the Node
`ws` package (`npm install`); when either is missing those tests report as
skipped instead of failing.

Before publishing the repository, choose and add an open-source license that
matches how you want others to use and contribute to the project.
