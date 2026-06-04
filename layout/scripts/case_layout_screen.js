#!/usr/bin/env node
'use strict';

const path = require('path');
const projectRoot = path.resolve(__dirname, '../../..');
const gpt54 = require(path.join(projectRoot, 'scripts/providers/gpt54'));

const PROMPT = `你是医美案例素材整理助手。请根据图片内容，只输出一行 JSON，不要输出其他文字。

字段要求：
- phase_guess: "术前" | "术后" | "不确定"
- view_guess: "正面" | "45侧" | "侧面" | "背面" | "局部" | "其他"
- subject: "面部" | "颈部" | "身体" | "手部" | "其他"
- quality: "good" | "fair" | "poor"
- usable: true | false
- confidence: "high" | "medium" | "low"
- direction_guess: "left" | "right" | "center" | "unknown"
- reason: 20字以内中文短句

判断规则：
- 只根据图中可见信息判断，不要臆测不存在的治疗效果
- 如果无法确认术前/术后，必须返回 "不确定"
- 如果画面严重模糊、主体不完整、无法做案例对比，usable 返回 false
- "局部" 用于特写、近距离局部区域，不适合作为标准正面/侧面角度
- 对面部图要尽量判断左右朝向；正面返回 center，判断不出时返回 unknown
- 对身体/颈部图，如果明显是背对镜头，应返回 "背面"

输出示例：
{"phase_guess":"术前","view_guess":"正面","subject":"面部","quality":"good","usable":true,"confidence":"high","direction_guess":"center","reason":"正面面部清晰"}`
;

function extractJsonObject(raw) {
  if (!raw) return null;
  const text = String(raw).trim();
  try {
    return JSON.parse(text);
  } catch {}

  const match = text.match(/\{[\s\S]*\}/);
  if (!match) return null;
  try {
    return JSON.parse(match[0]);
  } catch {
    return null;
  }
}

function normalizeConfidence(raw, parsed) {
  const token = String(raw || '').trim().toLowerCase();
  if (token === 'high' || token === 'medium' || token === 'low') return token;
  if (parsed?.usable && parsed?.subject === '面部' && parsed?.view_guess && parsed?.view_guess !== '其他') {
    return 'medium';
  }
  return 'low';
}

async function analyzeOne(imagePath) {
  try {
    const raw = await gpt54.analyzeImage(imagePath, PROMPT);
    const parsed = extractJsonObject(raw);
    if (!parsed) {
      return {
        image_path: imagePath,
        error: `无法解析判读 JSON: ${String(raw).slice(0, 200)}`,
      };
    }
    return {
      image_path: imagePath,
      phase_guess: parsed.phase_guess || '不确定',
      view_guess: parsed.view_guess || '其他',
      subject: parsed.subject || '其他',
      quality: parsed.quality || 'poor',
      usable: Boolean(parsed.usable),
      confidence: normalizeConfidence(parsed.confidence, parsed),
      direction_guess: ['left', 'right', 'center', 'unknown'].includes(parsed.direction_guess)
        ? parsed.direction_guess
        : 'unknown',
      reason: parsed.reason || '',
      raw: raw,
    };
  } catch (error) {
    return {
      image_path: imagePath,
      error: error.message,
      confidence: 'low',
    };
  }
}

async function main() {
  const paths = process.argv.slice(2);
  if (paths.length === 0) {
    throw new Error('需要至少 1 个图片路径');
  }

  const results = [];
  for (const imagePath of paths) {
    results.push(await analyzeOne(imagePath));
  }
  process.stdout.write(`${JSON.stringify(results, null, 2)}\n`);
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error.message}\n`);
  process.exit(1);
});
