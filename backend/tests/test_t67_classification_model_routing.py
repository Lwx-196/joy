import json
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
GPT54_PROVIDER = Path("/Users/a1234/Desktop/飞书Claude/scripts/providers/gpt54.js")
GEMINI_FLASH_PROVIDER = Path("/Users/a1234/Desktop/飞书Claude/scripts/providers/gemini-flash.js")


def _run_node(script: str) -> dict:
    result = subprocess.run(
        ["node", "-e", script],
        cwd=REPO_ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


def test_gpt54_openai_compatible_uses_tuzi_primary_key_and_model_override() -> None:
    payload = _run_node(
        f"""
        (async () => {{
          process.env.VISION_PROVIDER = 'flashapi';
          process.env.VISION_API_BASE = 'https://api.tu-zi.com/v1';
          process.env.VISION_API_KEY = 'flash-key';
          process.env.GEMINI_TUZI_API_KEY = 'tuzi-key';
          process.env.VISION_API_MODEL = 'gpt-5.4';
          process.env.VISION_API_STREAM = 'false';

          let captured = null;
          global.fetch = async (url, options) => {{
            captured = {{
              url: String(url),
              authorization: options.headers.Authorization,
              body: JSON.parse(options.body),
            }};
            return {{
              ok: true,
              status: 200,
              text: async () => JSON.stringify({{
                choices: [{{ message: {{ content: 'primary-ok' }} }}],
              }}),
            }};
          }};

          const provider = require({json.dumps(str(GPT54_PROVIDER))});
          const result = await provider.chatComplete(
            [{{ role: 'user', content: 'ping' }}],
            32,
            {{ model: 'gpt-5.4-mini', stream: false }},
          );
          console.log(JSON.stringify({{ result, captured }}));
        }})().catch((err) => {{
          console.error(err && err.stack ? err.stack : err);
          process.exit(1);
        }});
        """
    )

    assert payload["result"] == "primary-ok"
    assert payload["captured"]["url"] == "https://api.tu-zi.com/v1/chat/completions"
    assert payload["captured"]["authorization"] == "Bearer tuzi-key"
    assert payload["captured"]["body"]["model"] == "gpt-5.4-mini"
    assert payload["captured"]["body"]["stream"] is False


def test_gpt54_openai_compatible_falls_back_to_flashapi_backup_endpoint() -> None:
    payload = _run_node(
        f"""
        (async () => {{
          process.env.VISION_PROVIDER = 'flashapi';
          process.env.VISION_API_BASE = 'https://api.tu-zi.com/v1';
          process.env.GEMINI_TUZI_API_KEY = 'tuzi-key';
          process.env.VISION_API_KEY = 'flash-key';
          process.env.VISION_BACKUP_BASE_URL = 'https://ai.flashapi.top/v1';
          process.env.VISION_API_MODEL = 'gpt-5.4';
          process.env.VISION_API_STREAM = 'false';

          const calls = [];
          global.fetch = async (url, options) => {{
            calls.push({{
              url: String(url),
              authorization: options.headers.Authorization,
              body: JSON.parse(options.body),
            }});
            if (calls.length === 1) {{
              return {{
                ok: false,
                status: 429,
                text: async () => 'rate limited',
              }};
            }}
            return {{
              ok: true,
              status: 200,
              text: async () => JSON.stringify({{
                choices: [{{ message: {{ content: 'backup-ok' }} }}],
              }}),
            }};
          }};

          const provider = require({json.dumps(str(GPT54_PROVIDER))});
          const result = await provider.chatComplete(
            [{{ role: 'user', content: 'ping' }}],
            32,
            {{ model: 'gpt-5.4', stream: false }},
          );
          console.log(JSON.stringify({{ result, calls }}));
        }})().catch((err) => {{
          console.error(err && err.stack ? err.stack : err);
          process.exit(1);
        }});
        """
    )

    assert payload["result"] == "backup-ok"
    assert [call["url"] for call in payload["calls"]] == [
        "https://api.tu-zi.com/v1/chat/completions",
        "https://ai.flashapi.top/v1/chat/completions",
    ]
    assert [call["authorization"] for call in payload["calls"]] == [
        "Bearer tuzi-key",
        "Bearer flash-key",
    ]


def test_gemini_flash_gpt54_classification_defaults_to_mini_model() -> None:
    payload = _run_node(
        f"""
        (async () => {{
          process.env.GEMINI_FLASH_PROVIDER = 'gpt54';
          delete process.env.GEMINI_FLASH_MODEL;
          process.env.VISION_PROVIDER = 'flashapi';
          process.env.VISION_API_BASE = 'https://api.tu-zi.com/v1';
          process.env.GEMINI_TUZI_API_KEY = 'tuzi-key';
          process.env.VISION_API_KEY = 'flash-key';
          process.env.VISION_API_MODEL = 'gpt-5.4';
          process.env.VISION_API_STREAM = 'false';

          let captured = null;
          global.fetch = async (url, options) => {{
            captured = {{
              url: String(url),
              authorization: options.headers.Authorization,
              body: JSON.parse(options.body),
            }};
            return {{
              ok: true,
              status: 200,
              text: async () => JSON.stringify({{
                choices: [{{ message: {{ content: '[{{"index":1,"intent":"价格咨询"}}]' }} }}],
              }}),
            }};
          }};

          const provider = require({json.dumps(str(GEMINI_FLASH_PROVIDER))});
          const result = await provider.classifyTextBatch(['多少钱']);
          console.log(JSON.stringify({{
            result,
            captured,
            fallbackModel: provider.FALLBACK_MODEL,
          }}));
        }})().catch((err) => {{
          console.error(err && err.stack ? err.stack : err);
          process.exit(1);
        }});
        """
    )

    assert payload["result"] == [{"index": 1, "intent": "价格咨询"}]
    assert payload["captured"]["body"]["model"] == "gpt-5.4-mini"
    assert payload["fallbackModel"] == "gpt-5.4"
