#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');

const projectRoot = path.resolve(__dirname, '../../..');
const gpt54 = require(path.join(projectRoot, 'scripts/providers/gpt54'));

function parseJsonObject(raw) {
  const text = String(raw || '').replace(/```json\s*/gi, '').replace(/```/g, '').trim();
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

function parseArgv(argv) {
  const out = {
    board: '',
    references: [],
    focusTargets: [],
  };
  for (let i = 2; i < argv.length; i += 1) {
    const token = argv[i];
    if (token === '--board' && argv[i + 1]) out.board = argv[++i];
    else if (token === '--reference' && argv[i + 1]) out.references.push(argv[++i]);
    else if (token === '--focus' && argv[i + 1]) out.focusTargets.push(argv[++i]);
  }
  if (!out.board) throw new Error('缺少 --board');
  return out;
}

function buildPrompt(focusTargets, referenceCount) {
  const focusLine = focusTargets.length > 0
    ? focusTargets.map((item, idx) => `${idx + 1}. ${item}`).join('；')
    : '无已确认 focus，本次主要检查术前术后左右关系和标签。';
  return [
    '你是医美案例拼图的最终质检助手。',
    '第1张图是最终案例拼图成品。',
    referenceCount > 0 ? `后续 ${referenceCount} 张参考图仅用于帮助判断 focus 展示和是否存在增强漂移。` : '本次没有额外参考图。',
    `已确认 focus_targets：${focusLine}`,
    '判断规则：',
    '1. left_right_ok：只有在整张图清楚保持左术前右术后时才返回 true。',
    '2. labels_ok：只有在术前/术后标签语义正确、没有左右错置或明显缺失时才返回 true。',
    '3. focus_present：如果有 focus_targets，只有在最终图里能清楚展示目标部位时才返回 true；如果没有 focus_targets，返回 true。',
    '4. enhancement_drift_ok：只有在术后图没有明显漂到非目标区域、没有不合理增强或身份漂移时才返回 true。',
    '5. decision 固定规则：',
    '   - left_right_ok=false 或 labels_ok=false -> reject',
    '   - focus_present=false 或 enhancement_drift_ok=false -> review',
    '   - 其余 -> pass',
    '只输出一行 JSON，不要解释：',
    '{"left_right_ok":true,"labels_ok":true,"focus_present":true,"enhancement_drift_ok":true,"decision":"pass","reason":"拼图左右与标签正确"}',
  ].join('\n');
}

function normalizeDecision(parsed, hasFocus) {
  const leftRightOk = Boolean(parsed.left_right_ok);
  const labelsOk = Boolean(parsed.labels_ok);
  const focusPresent = hasFocus ? Boolean(parsed.focus_present) : true;
  const enhancementDriftOk = Boolean(parsed.enhancement_drift_ok);

  let decision = String(parsed.decision || '').trim().toLowerCase();
  if (!['pass', 'review', 'reject'].includes(decision)) {
    decision = 'pass';
  }
  if (!leftRightOk || !labelsOk) {
    decision = 'reject';
  } else if (!focusPresent || !enhancementDriftOk) {
    decision = 'review';
  }

  return {
    left_right_ok: leftRightOk,
    labels_ok: labelsOk,
    focus_present: focusPresent,
    enhancement_drift_ok: enhancementDriftOk,
    decision,
    reason: String(parsed.reason || '').trim().slice(0, 80) || '未提供原因',
  };
}

async function main() {
  const args = parseArgv(process.argv);
  const allPaths = [args.board, ...args.references];
  for (const filePath of allPaths) {
    if (!fs.existsSync(filePath)) throw new Error(`图片不存在: ${filePath}`);
  }

  const content = [{ type: 'text', text: buildPrompt(args.focusTargets, args.references.length) }];
  for (const imagePath of allPaths) {
    const prepared = gpt54.prepareImage(imagePath);
    content.push({
      type: 'image_url',
      image_url: { url: `data:${prepared.mimeType};base64,${prepared.base64}` },
    });
  }

  const raw = await gpt54.chatComplete([{ role: 'user', content }], 768, { stream: true });
  const parsed = parseJsonObject(raw);
  if (!parsed) {
    throw new Error(`无法解析 final qa JSON: ${String(raw).slice(0, 400)}`);
  }

  const normalized = normalizeDecision(parsed, args.focusTargets.length > 0);
  process.stdout.write(`${JSON.stringify(normalized, null, 2)}\n`);
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error.message}\n`);
  process.exit(1);
});
