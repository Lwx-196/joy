import json
import subprocess
from pathlib import Path

import pytest

from backend.scripts import comfyui_vlm_judge_runner


REPO_ROOT = Path(__file__).resolve().parents[2]
GEMINI_FLASH_PROVIDER = Path("/Users/a1234/Desktop/飞书Claude/scripts/providers/gemini-flash.js")

pytestmark = pytest.mark.skipif(
    not GEMINI_FLASH_PROVIDER.exists(),
    reason="飞书Claude provider JS not present (CI)",
)


def _run_node(script: str) -> dict:
    result = subprocess.run(
        ["node", "-e", script],
        cwd=REPO_ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


def test_gemini_flash_slot_can_delegate_to_gpt54_flashapi_without_gemini_key() -> None:
    payload = _run_node(
        f"""
        (async () => {{
          process.env.GEMINI_FLASH_PROVIDER = 'gpt54';
          process.env.GEMINI_FLASH_MODEL = 'gpt-5.4';
          process.env.GEMINI_FLASH_BASE_URL = 'https://ai.flashapi.top/v1';
          process.env.VISION_PROVIDER = 'flashapi';
          process.env.VISION_API_BASE = 'https://ai.flashapi.top/v1';
          process.env.VISION_API_KEY = 'unit-vision-key';
          process.env.VISION_API_MODEL = 'gpt-5.4';
          process.env.VISION_API_STREAM = 'false';
          delete process.env.GEMINI_FLASH_API_KEY;

          let captured = null;
          global.fetch = async (url, options) => {{
            captured = {{
              url: String(url),
              authorization: options.headers.Authorization,
              body: JSON.parse(options.body)
            }};
            return {{
              ok: true,
              status: 200,
              json: async () => ({{
                choices: [{{ message: {{ content: '[{{"index":1,"intent":"价格咨询"}}]' }} }}]
              }}),
              text: async () => JSON.stringify({{
                choices: [{{ message: {{ content: '[{{"index":1,"intent":"价格咨询"}}]' }} }}]
              }})
            }};
          }};

          const provider = require({json.dumps(str(GEMINI_FLASH_PROVIDER))});
          const result = await provider.classifyTextBatch(['多少钱']);
          console.log(JSON.stringify({{
            result,
            captured,
            leakedGeminiKey: JSON.stringify(captured).includes('unit-gemini-key')
          }}));
        }})().catch((err) => {{
          console.error(err && err.stack ? err.stack : err);
          process.exit(1);
        }});
        """
    )

    assert payload["captured"]["url"] == "https://ai.flashapi.top/v1/chat/completions"
    assert payload["captured"]["authorization"] == "Bearer unit-vision-key"
    assert payload["captured"]["body"]["model"] == "gpt-5.4"
    assert payload["captured"]["body"]["stream"] is False
    assert payload["result"] == [{"index": 1, "intent": "价格咨询"}]
    assert payload["leakedGeminiKey"] is False


def test_comfyui_vlm_runner_accepts_flashapi_vision_provider_as_openai_responses() -> None:
    config = comfyui_vlm_judge_runner._provider_config(
        env={
            "VISION_PROVIDER": "flashapi",
            "VISION_API_BASE": "https://ai.flashapi.top/v1",
            "VISION_API_KEY": "unit-vision-key",
            "VISION_API_MODEL": "gpt-5.4",
        },
        provider=None,
        model=None,
        endpoint=None,
    )

    assert config["ready"] is True
    assert config["provider"] == "openai_responses"
    assert config["model"] == "gpt-5.4"
    assert config["endpoint"] == "https://ai.flashapi.top/v1/responses"
    assert config["api_key"] == "unit-vision-key"
