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
noVNC / websockify (localhost:6080)
    |
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
Agent screenshot -> inspect -> act -> verify loop
```

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
git clone https://github.com/HoussemMouradi/colab_browser-computer-use.git
cd colab_browser-computer-use
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
STATE_DIR=/content/colab_browser-computer-use/.runtime
```

Open `BROWSER_URL` on another computer and enter `VNC_PASSWORD` when prompted.
Both values are temporary. A new tunnel URL and password are generated on each
start.

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
├── screen_shots/             # Generated screenshots, ignored by Git
└── .runtime/                 # Generated profile, logs, PIDs, and VNC data
```

`.runtime/` is created only after startup and is ignored by Git.

## Security notes

- Use this only in an isolated, disposable Colab runtime.
- Chrome runs as root with `--no-sandbox`, and software WebGL uses
  `--enable-unsafe-swiftshader`. These flags reduce browser isolation.
- Do not use the environment for sensitive personal, financial, medical, or
  production accounts.
- Treat the noVNC URL as public and share neither it nor the VNC password.
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

Before publishing the repository, choose and add an open-source license that
matches how you want others to use and contribute to the project.
