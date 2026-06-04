#!/usr/bin/env node
'use strict';

const path = require('path');
const fs = require('fs');
const os = require('os');
const { execFileSync } = require('child_process');

const projectRoot = path.resolve(__dirname, '../../..');
const router = require(path.join(projectRoot, 'scripts/model-router'));
const FACE_ALIGN_PATH = path.join(projectRoot, 'scripts/face_align_compare.py');
const STABILIZATION_MAX_SCORE = 55;
const DIRECTION_MISMATCH_PENALTY = 1000;
const PORTRAIT_MISMATCH_PENALTY = 1000;
const ASPECT_RATIO_HARD_DELTA = 0.14;
const FACE_RATIO_HARD_DELTA = 0.16;

if (require.main === module) {
  console.log = (...args) => console.error(...args);
}

const POSE_PY = `
import importlib.util, json, sys
from PIL import Image, ImageOps
spec = importlib.util.spec_from_file_location("face_align_compare", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
image_path = sys.argv[2]
try:
    pil = Image.open(image_path)
    display = ImageOps.exif_transpose(pil)
    face = mod.detect_face_landmarks(image_path)
    pose = face.get("pose") or {}
    print(json.dumps({
        "ok": True,
        "pose": pose,
        "view": face.get("view") or {},
        "size": face.get("size"),
        "display_size": display.size,
        "eye_distance": face.get("eye_distance"),
        "face_height": face.get("face_height"),
    }, ensure_ascii=False))
except Exception as exc:
    print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
`;

function getPoseMetrics(imagePath) {
  const raw = execFileSync('python3', ['-c', POSE_PY, FACE_ALIGN_PATH, imagePath], {
    encoding: 'utf8',
    cwd: projectRoot,
  });
  const parsed = JSON.parse(raw.trim());
  return parsed.ok ? parsed : null;
}

function rotateImage(sourcePath, degrees, outPath) {
  execFileSync('sips', ['-r', String(degrees), sourcePath, '--out', outPath], {
    encoding: 'utf8',
    stdio: ['ignore', 'ignore', 'pipe'],
  });
}

function buildOrientationCandidates(imagePath) {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'enhance-rotate-'));
  const ext = path.extname(imagePath) || '.jpg';
  const candidates = [{ path: imagePath, rotation: 0, temp: false }];
  for (const degrees of [90, 180, 270]) {
    const outPath = path.join(tmpDir, `rot-${degrees}${ext}`);
    rotateImage(imagePath, degrees, outPath);
    candidates.push({ path: outPath, rotation: degrees, temp: true });
  }
  return { tmpDir, candidates };
}

function poseDistanceScore(candidatePose, referencePose) {
  const yaw = Math.abs((candidatePose.yaw || 0) - (referencePose.yaw || 0));
  const pitch = Math.abs((candidatePose.pitch || 0) - (referencePose.pitch || 0));
  const roll = Math.abs((candidatePose.roll || 0) - (referencePose.roll || 0));
  return yaw + pitch + roll * 0.5;
}

function displaySize(metrics) {
  return metrics?.display_size
    ? { width: Number(metrics.display_size[0]), height: Number(metrics.display_size[1]) }
    : null;
}

function aspectRatio(size) {
  if (!size || !size.width || !size.height) return null;
  return Number(size.width) / Number(size.height);
}

function faceHeightRatio(metrics) {
  const size = displaySize(metrics);
  const faceHeight = Number(metrics?.face_height || 0);
  if (!size || !size.height || !faceHeight) return null;
  return faceHeight / size.height;
}

function inferPoseDirection(metrics) {
  const viewDirection = String(metrics?.view?.direction || '').trim();
  if (['left', 'right', 'center'].includes(viewDirection)) return viewDirection;
  const yaw = Number(metrics?.pose?.yaw || 0);
  if (!Number.isFinite(yaw) || Math.abs(yaw) < 10) return 'center';
  return yaw > 0 ? 'right' : 'left';
}

function hasDirectionMismatch(candidateMetrics, referenceMetrics) {
  const candidateDirection = inferPoseDirection(candidateMetrics);
  const referenceDirection = inferPoseDirection(referenceMetrics);
  if (!['left', 'right'].includes(candidateDirection)) return false;
  if (!['left', 'right'].includes(referenceDirection)) return false;
  return candidateDirection !== referenceDirection;
}

function evaluateOrientationCandidate(candidateMetrics, referenceMetrics) {
  if (!candidateMetrics?.pose || !referenceMetrics?.pose) {
    return {
      usable: false,
      totalScore: 9999,
      poseScore: 9999,
      portraitMatch: false,
      directionMismatch: false,
      aspectDelta: null,
      faceRatioDelta: null,
      reason: 'pose_unavailable',
    };
  }

  const candidateSize = displaySize(candidateMetrics);
  const referenceSize = displaySize(referenceMetrics);
  const portraitMatch = !candidateSize || !referenceSize
    ? true
    : (candidateSize.height >= candidateSize.width) === (referenceSize.height >= referenceSize.width);
  const candidateAspect = aspectRatio(candidateSize);
  const referenceAspect = aspectRatio(referenceSize);
  const aspectDelta = candidateAspect && referenceAspect ? Math.abs(candidateAspect - referenceAspect) : 0;
  const candidateFaceRatio = faceHeightRatio(candidateMetrics);
  const referenceFaceRatio = faceHeightRatio(referenceMetrics);
  const faceRatioDelta = candidateFaceRatio && referenceFaceRatio ? Math.abs(candidateFaceRatio - referenceFaceRatio) : 0;
  const directionMismatch = hasDirectionMismatch(candidateMetrics, referenceMetrics);
  const poseScore = poseDistanceScore(candidateMetrics.pose, referenceMetrics.pose);
  const aspectPenalty = aspectDelta > ASPECT_RATIO_HARD_DELTA ? 400 + aspectDelta * 120 : aspectDelta * 80;
  const faceRatioPenalty = faceRatioDelta > FACE_RATIO_HARD_DELTA ? 280 + faceRatioDelta * 160 : faceRatioDelta * 100;
  const totalScore = (
    poseScore
    + (portraitMatch ? 0 : PORTRAIT_MISMATCH_PENALTY)
    + (directionMismatch ? DIRECTION_MISMATCH_PENALTY : 0)
    + aspectPenalty
    + faceRatioPenalty
  );

  return {
    usable: totalScore <= STABILIZATION_MAX_SCORE && !directionMismatch && portraitMatch,
    totalScore,
    poseScore,
    portraitMatch,
    directionMismatch,
    aspectDelta,
    faceRatioDelta,
    candidateDirection: inferPoseDirection(candidateMetrics),
    referenceDirection: inferPoseDirection(referenceMetrics),
    reason: directionMismatch
      ? 'direction_mismatch'
      : !portraitMatch
        ? 'portrait_mismatch'
        : totalScore > STABILIZATION_MAX_SCORE
          ? 'score_exceeded'
          : 'ok',
  };
}

function buildFallbackStabilization(resultImagePath, fallbackImagePath, best, reason) {
  return {
    imagePath: fallbackImagePath || resultImagePath,
    generatedImagePath: resultImagePath,
    rotation: 0,
    score: best?.totalScore ?? null,
    corrected: false,
    fallback: true,
    reason,
    bestCandidate: best
      ? {
          path: best.path,
          rotation: best.rotation,
          score: best.totalScore,
          poseScore: best.poseScore,
          portraitMatch: best.portraitMatch,
          directionMismatch: best.directionMismatch,
          aspectDelta: best.aspectDelta,
          faceRatioDelta: best.faceRatioDelta,
          candidateDirection: best.candidateDirection,
          referenceDirection: best.referenceDirection,
          reason: best.reason,
        }
      : null,
  };
}

function chooseBestOrientation(resultImagePath, poseRefPath, options = {}) {
  const fallbackImagePath = options.fallbackImagePath || resultImagePath;
  const getMetrics = options.getPoseMetrics || getPoseMetrics;
  const buildCandidates = options.buildOrientationCandidates || buildOrientationCandidates;
  const copyFile = options.copyFileSync || fs.copyFileSync;
  if (!poseRefPath) {
    return {
      imagePath: resultImagePath,
      generatedImagePath: resultImagePath,
      rotation: 0,
      score: null,
      corrected: false,
      fallback: false,
      reason: 'no_pose_ref',
    };
  }

  const refPoseMetrics = getMetrics(poseRefPath);
  if (!refPoseMetrics?.pose) {
    return buildFallbackStabilization(resultImagePath, fallbackImagePath, null, 'reference_pose_unavailable');
  }

  const { tmpDir, candidates } = buildCandidates(resultImagePath);
  let best = null;
  try {
    for (const candidate of candidates) {
      const poseMetrics = getMetrics(candidate.path);
      const evaluation = evaluateOrientationCandidate(poseMetrics, refPoseMetrics);
      const entry = {
        path: candidate.path,
        rotation: candidate.rotation,
        ...evaluation,
      };
      if (!best || entry.totalScore < best.totalScore) best = entry;
    }

    if (!best) {
      return buildFallbackStabilization(resultImagePath, fallbackImagePath, null, 'no_candidate');
    }

    if (!best.usable) {
      return buildFallbackStabilization(resultImagePath, fallbackImagePath, best, best.reason || 'score_exceeded');
    }

    if (best.rotation === 0) {
      return {
        imagePath: resultImagePath,
        generatedImagePath: resultImagePath,
        rotation: 0,
        score: best.totalScore,
        corrected: false,
        fallback: false,
        reason: 'ok',
        candidateDirection: best.candidateDirection,
        referenceDirection: best.referenceDirection,
        aspectDelta: best.aspectDelta,
        faceRatioDelta: best.faceRatioDelta,
      };
    }

    const finalExt = path.extname(resultImagePath) || '.jpg';
    const finalPath = path.join(path.dirname(resultImagePath), `${path.basename(resultImagePath, finalExt)}-upright${finalExt}`);
    copyFile(best.path, finalPath);
    return {
      imagePath: finalPath,
      generatedImagePath: resultImagePath,
      rotation: best.rotation,
      score: best.totalScore,
      corrected: true,
      fallback: false,
      reason: 'rotated',
      candidateDirection: best.candidateDirection,
      referenceDirection: best.referenceDirection,
      aspectDelta: best.aspectDelta,
      faceRatioDelta: best.faceRatioDelta,
    };
  } finally {
    for (const candidate of candidates) {
      if (candidate.temp && fs.existsSync(candidate.path)) {
        fs.rmSync(candidate.path, { force: true });
      }
    }
    if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
  }
}

function parseArgv(argv) {
  const out = {
    image: '',
    poseRefs: [],
    prompt: '',
    quality: '4k',
    model: '',
    usePlanner: true,
  };
  for (let i = 2; i < argv.length; i += 1) {
    const token = argv[i];
    if (token === '--image' && argv[i + 1]) out.image = argv[++i];
    else if (token === '--pose-ref' && argv[i + 1]) out.poseRefs.push(argv[++i]);
    else if (token === '--prompt' && argv[i + 1]) out.prompt = argv[++i];
    else if (token === '--quality' && argv[i + 1]) out.quality = argv[++i];
    else if (token === '--model' && argv[i + 1]) out.model = argv[++i];
    else if (token === '--no-planner') out.usePlanner = false;
    else if (token === '--help' || token === '-h') {
      process.stdout.write([
        'Usage: node case_layout_enhance.js --image <path> --prompt <text> [options]',
        '',
        'Options:',
        '  --image <path>       待编辑图片路径',
        '  --pose-ref <path>    姿态参考图（可多次指定）',
        '  --prompt <text>      编辑描述',
        '  --quality <level>    输出质量: 4k|2k|draft (default: 4k)',
        '  --model <name>       覆盖生图模型',
        '  --no-planner         跳过 edit-planner 改写，完整 prompt 直达 Tuzi 模型',
        '  --help, -h           显示帮助信息',
      ].join('\n') + '\n');
      process.exit(0);
    }
  }
  if (!out.image) throw new Error('缺少 --image');
  if (!out.prompt) throw new Error('缺少 --prompt');
  return out;
}

async function main() {
  const args = parseArgv(process.argv);
  const prompt = args.poseRefs.length
    ? [
        '第一张图是要编辑的术后原图。',
        ...args.poseRefs.map((_, idx) => `第${idx + 2}张图是姿态/构图参考图，只用于对齐姿态，不得替换人物身份。`),
        args.prompt,
      ].join('\n')
    : args.prompt;
  const result = await router.editPipeline({
    imagePaths: [args.image, ...args.poseRefs],
    userDirection: prompt,
    quality: args.quality,
    model: args.model || undefined,
    usePlanner: args.usePlanner,
  });
  let stabilized = null;
  let stabilizedPath = result.imagePath || null;
  if (result.success && result.imagePath && args.poseRefs.length) {
    try {
      stabilized = chooseBestOrientation(result.imagePath, args.image, {
        fallbackImagePath: args.image,
      });
      stabilizedPath = stabilized.imagePath;
    } catch (err) {
      stabilized = buildFallbackStabilization(
        result.imagePath,
        args.image,
        null,
        `stabilization_exception:${err.message}`,
      );
      stabilizedPath = args.image;
    }
  }

  process.stdout.write(`${JSON.stringify({
    success: Boolean(result.success && stabilizedPath),
    imagePath: stabilizedPath,
    generatedImagePath: result.imagePath || null,
    stage: result.stage || null,
    quality: args.quality,
    poseRefCount: args.poseRefs.length,
    plannerUsed: Boolean(result.plannerUsed),
    plannedTasks: result.plannedTasks || '',
    editPrompt: result.editPrompt || '',
    degradations: result.degradations || [],
    elapsedSeconds: result.elapsedSeconds || {},
    pending: result.pending || null,
    stabilization: stabilized,
    model: result.usedModel || args.model || null,
    requestedModel: args.model || null,
  }, null, 2)}\n`);
}

if (require.main === module) {
  main().catch((err) => {
    process.stderr.write(`${err.stack || err.message}\n`);
    process.exit(1);
  });
}

module.exports = {
  STABILIZATION_MAX_SCORE,
  inferPoseDirection,
  hasDirectionMismatch,
  evaluateOrientationCandidate,
  buildFallbackStabilization,
  chooseBestOrientation,
  parseArgv,
  main,
};
