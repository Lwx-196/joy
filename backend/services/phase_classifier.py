"""Hybrid phase classifier: EfficientNet fast pre-screen + VLM fallback.

High-confidence EfficientNet predictions are trusted directly (milliseconds).
Low-confidence ones fall back to VLM (ollama qwen2.5vl) for accuracy.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import torch
from PIL import Image
from torchvision import models, transforms

if TYPE_CHECKING:
    from .vlm_provider import VLMProvider
    from .vlm_source_classifier import ClassificationResult

logger = logging.getLogger(__name__)

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

CONFIDENCE_THRESHOLD = float(os.environ.get("PHASE_CLASSIFIER_THRESHOLD", "0.85"))
MODEL_PATH_DEFAULT = Path(__file__).resolve().parents[3] / "phase_classifier_finetune.pt"
LABEL_NAMES = ["before", "after"]


@dataclass(frozen=True)
class PhaseResult:
    phase: str
    confidence: float
    source: str  # "efficientnet" or "vlm_fallback"


class EfficientNetPhaseClassifier:
    """Singleton wrapper for EfficientNet-B0 phase classifier."""

    _instance: EfficientNetPhaseClassifier | None = None

    def __init__(self, model_path: Path | None = None):
        self.model_path = model_path or Path(os.environ.get("PHASE_CLASSIFIER_MODEL", str(MODEL_PATH_DEFAULT)))
        self.device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
        self._model = None
        self._transform = None
        self._img_size = 224
        self._arch = "b0"

    @classmethod
    def get_instance(cls, model_path: Path | None = None) -> EfficientNetPhaseClassifier:
        if cls._instance is None:
            cls._instance = cls(model_path)
        return cls._instance

    def _ensure_loaded(self):
        if self._model is not None:
            return
        if not self.model_path.exists():
            raise FileNotFoundError(f"Phase classifier model not found: {self.model_path}")

        checkpoint = torch.load(self.model_path, map_location=self.device, weights_only=True)
        self._img_size = checkpoint.get("img_size", 224)
        self._arch = checkpoint.get("arch", "b0")

        if self._arch == "b2":
            model = models.efficientnet_b2()
        else:
            model = models.efficientnet_b0()
        model.classifier = torch.nn.Sequential(
            torch.nn.Dropout(p=0.4 if self._arch == "b2" else 0.3),
            torch.nn.Linear(model.classifier[1].in_features, 1),
        )
        model.load_state_dict(checkpoint["model_state_dict"])
        model.to(self.device)
        model.eval()
        self._model = model

        resize_to = int(self._img_size * 256 / 224)
        self._transform = transforms.Compose([
            transforms.Resize(resize_to),
            transforms.CenterCrop(self._img_size),
            transforms.ToTensor(),
            transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
        ])

        logger.info(
            "Loaded phase classifier: arch=%s img_size=%d val_f1=%.3f from %s",
            self._arch, self._img_size,
            checkpoint.get("val_f1", 0), self.model_path,
        )

    @torch.no_grad()
    def classify(self, image_path: Path) -> PhaseResult:
        self._ensure_loaded()
        try:
            img = Image.open(image_path).convert("RGB")
        except Exception as exc:
            logger.warning("Cannot open image %s: %s", image_path, exc)
            return PhaseResult(phase="unknown", confidence=0.0, source="efficientnet")

        tensor = self._transform(img).unsqueeze(0).to(self.device)
        logit = self._model(tensor).squeeze()
        prob = torch.sigmoid(logit).item()

        if prob > 0.5:
            phase, confidence = "after", prob
        else:
            phase, confidence = "before", 1 - prob

        return PhaseResult(phase=phase, confidence=round(confidence, 4), source="efficientnet")

    @torch.no_grad()
    def classify_batch(self, image_paths: list[Path]) -> list[PhaseResult]:
        return [self.classify(p) for p in image_paths]


def classify_phase_hybrid(
    image_path: Path,
    vlm_provider: VLMProvider | None = None,
    *,
    threshold: float = CONFIDENCE_THRESHOLD,
    model_path: Path | None = None,
) -> PhaseResult:
    """Classify phase with EfficientNet, fallback to VLM if low confidence."""
    classifier = EfficientNetPhaseClassifier.get_instance(model_path)
    result = classifier.classify(image_path)

    if result.confidence >= threshold:
        logger.debug("EfficientNet high-conf: %s %.2f%% %s", result.phase, result.confidence * 100, image_path.name)
        return result

    if vlm_provider is None:
        logger.debug("EfficientNet low-conf but no VLM provider: %s %.2f%% %s", result.phase, result.confidence * 100, image_path.name)
        return result

    logger.info("EfficientNet low-conf (%.2f%%), falling back to VLM: %s", result.confidence * 100, image_path.name)
    try:
        from .vlm_source_classifier import classify_image
        vlm_result = classify_image(image_path, vlm_provider)
        return PhaseResult(
            phase=vlm_result.phase,
            confidence=round(vlm_result.confidence, 4),
            source="vlm_fallback",
        )
    except Exception as exc:
        logger.warning("VLM fallback failed for %s: %s, using EfficientNet result", image_path.name, exc)
        return result


def classify_phase_batch_hybrid(
    image_paths: list[Path],
    vlm_provider: VLMProvider | None = None,
    *,
    threshold: float = CONFIDENCE_THRESHOLD,
    model_path: Path | None = None,
) -> list[PhaseResult]:
    """Batch hybrid classification: EfficientNet first, VLM fallback for low confidence."""
    classifier = EfficientNetPhaseClassifier.get_instance(model_path)
    en_results = classifier.classify_batch(image_paths)

    results: list[PhaseResult] = list(en_results)
    low_conf_indices = [i for i, r in enumerate(en_results) if r.confidence < threshold]

    if not low_conf_indices or vlm_provider is None:
        high = len(image_paths) - len(low_conf_indices)
        logger.info(
            "Phase batch: %d images, %d high-conf (EfficientNet), %d low-conf%s",
            len(image_paths), high, len(low_conf_indices),
            " (no VLM provider)" if vlm_provider and low_conf_indices else "",
        )
        return results

    logger.info("Phase batch: %d high-conf, %d low-conf → VLM fallback", len(image_paths) - len(low_conf_indices), len(low_conf_indices))

    try:
        from .vlm_source_classifier import classify_batch
        low_conf_paths = [image_paths[i] for i in low_conf_indices]
        vlm_results = classify_batch(low_conf_paths, vlm_provider, return_exceptions=True)

        for idx, vlm_result in zip(low_conf_indices, vlm_results):
            if isinstance(vlm_result, BaseException):
                logger.warning("VLM fallback failed for %s: %s", image_paths[idx].name, vlm_result)
                continue
            results[idx] = PhaseResult(
                phase=vlm_result.phase,
                confidence=round(vlm_result.confidence, 4),
                source="vlm_fallback",
            )
    except Exception as exc:
        logger.warning("VLM batch fallback failed: %s", exc)

    return results
