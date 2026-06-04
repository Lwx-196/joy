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
    before: '',
    after: '',
    slot: '',
    focusTargets: [],
  };
  for (let i = 2; i < argv.length; i += 1) {
    const token = argv[i];
    if (token === '--before' && argv[i + 1]) out.before = argv[++i];
    else if (token === '--after' && argv[i + 1]) out.after = argv[++i];
    else if (token === '--slot' && argv[i + 1]) out.slot = argv[++i];
    else if (token === '--focus' && argv[i + 1]) out.focusTargets.push(argv[++i]);
  }
  if (!out.before) throw new Error('缺少 --before');
  if (!out.after) throw new Error('缺少 --after');
  if (!out.slot) throw new Error('缺少 --slot');
  return out;
}

function buildPrompt(slot, focusTargets) {
  const focusLine = focusTargets.length > 0
    ? focusTargets.map((item, idx) => `${idx + 1}. ${item}`).join('；')
    : '无已确认 focus，本次只判断是否同人、同角度可比。';
  return [
    '你是医美案例语义复核助手。',
    `第1张图是术前，第2张图是术后，当前角度槽位是：${slot}。`,
    `已确认 focus_targets：${focusLine}`,
    '判断规则：',
    '1. 只根据图中可见证据判断，不要臆测治疗项目和不存在的效果。',
    '2. same_person_likely 只有在高度像同一人时才返回 true。',
    '3. comparable_view 只有在当前两张图语义上属于同一角度、同一侧向、可做术前术后对比时才返回 true。',
    '4. 如果 focus_targets 不为空，但当前角度看不清该部位或不足以支撑展示，focus_visible 返回 false。',
    '5. 如果术后图看起来存在超出 focus_targets 的额外优化风险，例如无关皮肤瑕疵被处理、无关轮廓明显变化，non_target_drift_risk 返回 true。',
    '6. decision 固定规则：',
    '   - same_person_likely=false -> reject',
    '   - comparable_view=false -> reject',
    '   - 有 focus_targets 且 focus_visible=false -> reject',
    '   - non_target_drift_risk=true -> review',
    '   - 其余 -> pass',
    '只输出一行 JSON，不要解释：',
    '{"same_person_likely":true,"comparable_view":true,"focus_visible":true,"non_target_drift_risk":false,"decision":"pass","reason":"同人同角度可比"}',
  ].join('\n');
}

function normalizeDecision(parsed, hasFocus) {
  const samePerson = Boolean(parsed.same_person_likely);
  const comparableView = Boolean(parsed.comparable_view);
  const focusVisible = hasFocus ? Boolean(parsed.focus_visible) : true;
  const nonTargetDriftRisk = Boolean(parsed.non_target_drift_risk);

  let decision = String(parsed.decision || '').trim().toLowerCase();
  if (!['pass', 'review', 'reject'].includes(decision)) {
    decision = 'pass';
  }
  if (!samePerson || !comparableView || (hasFocus && !focusVisible)) {
    decision = 'reject';
  } else if (nonTargetDriftRisk) {
    decision = 'review';
  }

  return {
    same_person_likely: samePerson,
    comparable_view: comparableView,
    focus_visible: focusVisible,
    non_target_drift_risk: nonTargetDriftRisk,
    decision,
    reason: String(parsed.reason || '').trim().slice(0, 80) || '未提供原因',
  };
}

async function main() {
  const args = parseArgv(process.argv);
  for (const filePath of [args.before, args.after]) {
    if (!fs.existsSync(filePath)) throw new Error(`图片不存在: ${filePath}`);
  }

  const before = gpt54.prepareImage(args.before);
  const after = gpt54.prepareImage(args.after);
  const prompt = buildPrompt(args.slot, args.focusTargets);
  const raw = await gpt54.chatComplete([{
    role: 'user',
    content: [
      { type: 'text', text: prompt },
      { type: 'image_url', image_url: { url: `data:${before.mimeType};base64,${before.base64}` } },
      { type: 'image_url', image_url: { url: `data:${after.mimeType};base64,${after.base64}` } },
    ],
  }], 768, { stream: true });

  const parsed = parseJsonObject(raw);
  if (!parsed) {
    throw new Error(`无法解析 pair review JSON: ${String(raw).slice(0, 400)}`);
  }

  const normalized = normalizeDecision(parsed, args.focusTargets.length > 0);
  process.stdout.write(`${JSON.stringify(normalized, null, 2)}\n`);
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error.message}\n`);
  process.exit(1);
});
