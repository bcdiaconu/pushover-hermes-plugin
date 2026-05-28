# pushover-hermes-plugin

A [Hermes Agent](https://github.com/benoitbeauchamp/hermes-agent) plugin that adds [Pushover](https://pushover.net) as a notification platform. Outbound-only — sends push notifications, no inbound message handling.

## Requirements

- Python 3.11+
- `aiohttp >= 3.9`
- A [Pushover](https://pushover.net) account with an app token and user key

## Installation

### Installed via PyPI (not yet published)

```bash
pip install pushover-hermes-plugin
```

### Installed from source

```bash
pip install -e .
```

> **Important:** install the plugin into the **same Python environment** that runs your Hermes gateway.
>
> If you use the `uv`-managed tool installation (`~/.local/share/uv/tools/hermes-agent/`), target that env explicitly:
>
> ```bash
> uv pip install . --python ~/.local/share/uv/tools/hermes-agent/bin/python --force-reinstall
> ```
>
> If you use the git/dev installation (`~/.hermes/hermes-agent/venv/`), target that instead:
>
> ```bash
> uv pip install . --python ~/.hermes/hermes-agent/venv/bin/python --force-reinstall
> ```
>
> After installing, restart the gateway:
>
> ```bash
> hermes gateway restart --system   # or: sudo hermes gateway restart --system
> ```

Hermes Agent discovers the plugin automatically via the `hermes_agent.plugins` entry point — no manual registration needed.

## Configuration

### Option 1 — Environment variables (recommended)

```bash
export PUSHOVER_APP_TOKEN=your_app_token   # from pushover.net/apps
export PUSHOVER_USER_KEY=your_user_key     # from pushover.net front page
```

### Option 2 — config.yaml

```yaml
gateway:
  platforms:
    pushover:
      enabled: true
      api_key: <PUSHOVER_APP_TOKEN>
      token: <PUSHOVER_USER_KEY>
      extra:
        device: ""   # optional: restrict to a specific device name
```

> **Note:** environment variables always take precedence over config.yaml values.

### Option 3 — Interactive setup

```bash
hermes gateway setup
```

Select Pushover from the platform list. The wizard prompts for your credentials and writes them to `~/.hermes/.env`.

## Access control

Restrict which Pushover user keys can receive notifications:

```bash
export PUSHOVER_ALLOWED_USERS=user_key_1,user_key_2   # comma-separated
export PUSHOVER_ALLOW_ALL_USERS=true                  # disable restriction
```

## Behaviour

- Messages are truncated to 1024 characters (Pushover API limit)
- Images are sent as a text message containing the URL and caption
- `metadata["title"]` is forwarded as the notification title when present
- Fire-and-forget — no reply handling

## License

[MIT](LICENSE) — provided as-is, no warranty.
