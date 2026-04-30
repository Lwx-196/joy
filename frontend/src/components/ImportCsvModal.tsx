/**
 * ImportCsvModal — bulk render-batch import from CSV/JSON paste.
 *
 * Workflow: paste CSV (single column = case_id; or up to 4 cols
 * `case_id,brand,template,semantic_judge`) → "校验" calls
 * /api/cases/render/batch/preview → show valid/invalid breakdown →
 * "确认入队" calls /api/cases/render/batch and navigates to the new batch.
 *
 * Triggered from Cases.tsx header next to the existing exportCsv button.
 */
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  type Brand,
  type RenderBatchPreview,
  BRAND_LABEL,
} from "../api";
import {
  useBatchRenderCases,
  usePreviewBatchRender,
} from "../hooks/queries";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { Ico } from "./atoms";

const ALLOWED_BRANDS: Brand[] = ["fumei", "shimei"];
const ALLOWED_SEMANTIC = ["off", "auto"] as const;
const DEFAULT_TEMPLATE = "tri-compare";
const MAX_BATCH_SIZE = 50;

interface ParsedRow {
  line: number;
  raw: string;
  caseId: number | null;
  error?: string;
}

interface ParseResult {
  caseIds: number[];
  errors: ParsedRow[];
}

/** Parse CSV text. We only consume the first column (case_id) — per-row brand
 * / template overrides are out-of-scope for v1 to keep the UI simple; the
 * brand/template selectors in the modal apply uniformly to the whole batch. */
function parseCsv(text: string): ParseResult {
  const caseIds: number[] = [];
  const errors: ParsedRow[] = [];
  const lines = text.split(/\r?\n/);
  let lineNo = 0;
  for (const raw of lines) {
    lineNo += 1;
    const trimmed = raw.trim();
    if (!trimmed) continue;
    if (trimmed.startsWith("#")) continue;
    const firstCol = trimmed.split(",")[0].trim();
    if (lineNo === 1 && /^[a-z_]+$/i.test(firstCol)) {
      // header row — skip
      continue;
    }
    const n = Number(firstCol);
    if (!Number.isInteger(n) || n <= 0) {
      errors.push({ line: lineNo, raw: trimmed, caseId: null, error: "not_integer" });
      continue;
    }
    caseIds.push(n);
  }
  return { caseIds, errors };
}

export interface ImportCsvModalProps {
  open: boolean;
  onClose: () => void;
}

export function ImportCsvModal({ open, onClose }: ImportCsvModalProps) {
  const { t } = useTranslation("importCsv");
  const navigate = useNavigate();
  const dialogRef = useFocusTrap<HTMLDivElement>(open);

  const [csvText, setCsvText] = useState<string>("");
  const [brand, setBrand] = useState<Brand>("fumei");
  const [template, setTemplate] = useState<string>(DEFAULT_TEMPLATE);
  const [semanticJudge, setSemanticJudge] = useState<"off" | "auto">("off");
  const [preview, setPreview] = useState<RenderBatchPreview | null>(null);
  const [parseErrors, setParseErrors] = useState<ParsedRow[]>([]);

  const previewMut = usePreviewBatchRender();
  const enqueueMut = useBatchRenderCases();

  const parsed: ParseResult = useMemo(() => parseCsv(csvText), [csvText]);

  // Reset on open.
  useEffect(() => {
    if (!open) return;
    setCsvText("");
    setPreview(null);
    setParseErrors([]);
    setBrand("fumei");
    setTemplate(DEFAULT_TEMPLATE);
    setSemanticJudge("off");
    previewMut.reset();
    enqueueMut.reset();
    // intentionally only on open transition
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // ESC closes.
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, onClose]);

  if (!open) return null;

  const overLimit = parsed.caseIds.length > MAX_BATCH_SIZE;

  const onValidate = async () => {
    setParseErrors(parsed.errors);
    if (parsed.caseIds.length === 0) {
      setPreview(null);
      return;
    }
    if (overLimit) {
      setPreview(null);
      return;
    }
    const result = await previewMut.mutateAsync({
      caseIds: parsed.caseIds,
      payload: { brand, template, semantic_judge: semanticJudge },
    });
    setPreview(result);
  };

  const onConfirm = async () => {
    if (!preview || preview.valid_count === 0) return;
    const data = await enqueueMut.mutateAsync({
      caseIds: preview.valid_case_ids,
      payload: { brand, template, semantic_judge: semanticJudge },
    });
    onClose();
    navigate(`/jobs/batches/${data.batch_id}?type=render`);
  };

  const previewError = previewMut.error as { response?: { data?: { detail?: string } }; message?: string } | null;
  const enqueueError = enqueueMut.error as { response?: { data?: { detail?: string } }; message?: string } | null;
  const apiError =
    previewError?.response?.data?.detail ??
    previewError?.message ??
    enqueueError?.response?.data?.detail ??
    enqueueError?.message ??
    null;

  return (
    <>
      <div
        onClick={onClose}
        aria-hidden
        style={{
          position: "fixed",
          inset: 0,
          background: "rgba(28, 25, 23, 0.32)",
          zIndex: 1100,
        }}
      />
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="import-csv-title"
        style={{
          position: "fixed",
          top: "50%",
          left: "50%",
          transform: "translate(-50%, -50%)",
          width: 560,
          maxWidth: "94vw",
          maxHeight: "88vh",
          background: "var(--panel)",
          borderRadius: 12,
          boxShadow: "var(--shadow-pop)",
          zIndex: 1101,
          padding: 22,
          display: "flex",
          flexDirection: "column",
          gap: 14,
          overflow: "hidden",
        }}
      >
        <header
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 12,
          }}
        >
          <div>
            <div id="import-csv-title" style={{ fontSize: 14, fontWeight: 600 }}>
              {t("title")}
            </div>
            <div style={{ fontSize: 11.5, color: "var(--ink-3)", marginTop: 4 }}>
              {t("subtitle", { max: MAX_BATCH_SIZE })}
            </div>
          </div>
          <button
            type="button"
            className="btn sm ghost"
            onClick={onClose}
            aria-label={t("close")}
            title={t("closeHint")}
            style={{ padding: 6 }}
          >
            <Ico name="x" size={12} />
          </button>
        </header>

        {/* Brand / template / semantic */}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 10 }}>
          <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <span style={{ fontSize: 11.5, color: "var(--ink-3)" }}>{t("brand")}</span>
            <select
              value={brand}
              onChange={(e) => setBrand(e.target.value as Brand)}
              className="input sm"
            >
              {ALLOWED_BRANDS.map((b) => (
                <option key={b} value={b}>{BRAND_LABEL[b]}</option>
              ))}
            </select>
          </label>
          <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <span style={{ fontSize: 11.5, color: "var(--ink-3)" }}>{t("template")}</span>
            <input
              type="text"
              value={template}
              onChange={(e) => setTemplate(e.target.value)}
              className="input sm"
              placeholder={DEFAULT_TEMPLATE}
            />
          </label>
          <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <span style={{ fontSize: 11.5, color: "var(--ink-3)" }}>{t("semanticJudge")}</span>
            <select
              value={semanticJudge}
              onChange={(e) => setSemanticJudge(e.target.value as "off" | "auto")}
              className="input sm"
            >
              {ALLOWED_SEMANTIC.map((s) => (
                <option key={s} value={s}>{t(`semantic.${s}` as never)}</option>
              ))}
            </select>
          </label>
        </div>

        {/* CSV textarea */}
        <div style={{ display: "flex", flexDirection: "column", gap: 4, minHeight: 0 }}>
          <span style={{ fontSize: 11.5, color: "var(--ink-3)" }}>{t("csvLabel")}</span>
          <textarea
            value={csvText}
            onChange={(e) => {
              setCsvText(e.target.value);
              setPreview(null);
              setParseErrors([]);
            }}
            placeholder={t("csvPlaceholder")}
            spellCheck={false}
            style={{
              fontFamily: "var(--mono)",
              fontSize: 12,
              padding: 10,
              minHeight: 140,
              maxHeight: 220,
              resize: "vertical",
              background: "var(--bg-2)",
              border: "1px solid var(--line)",
              borderRadius: 8,
              color: "var(--ink-1)",
              lineHeight: 1.5,
            }}
            aria-describedby="import-csv-help"
          />
          <div id="import-csv-help" style={{ fontSize: 11, color: "var(--ink-3)" }}>
            {t("csvHelp")}
          </div>
        </div>

        {/* Local parse errors */}
        {parseErrors.length > 0 && (
          <div
            role="alert"
            style={{
              fontSize: 11.5,
              padding: 8,
              background: "var(--err-50, rgba(254, 226, 226, 0.5))",
              color: "var(--err, rgb(153, 27, 27))",
              borderRadius: 6,
              maxHeight: 100,
              overflow: "auto",
            }}
          >
            <div style={{ fontWeight: 600, marginBottom: 4 }}>
              {t("parseErrorsTitle", { n: parseErrors.length })}
            </div>
            {parseErrors.slice(0, 5).map((e) => (
              <div key={e.line} style={{ fontFamily: "var(--mono)" }}>
                {t("parseErrorLine", { line: e.line, raw: e.raw })}
              </div>
            ))}
            {parseErrors.length > 5 && <div>… {t("more", { n: parseErrors.length - 5 })}</div>}
          </div>
        )}

        {/* Over-limit warning */}
        {overLimit && (
          <div role="alert" style={{ fontSize: 11.5, color: "var(--err)" }}>
            {t("overLimit", { n: parsed.caseIds.length, max: MAX_BATCH_SIZE })}
          </div>
        )}

        {/* Preview result */}
        {preview && (
          <div
            style={{
              fontSize: 12,
              padding: 10,
              background: "var(--bg-2)",
              border: "1px solid var(--line)",
              borderRadius: 8,
              maxHeight: 140,
              overflow: "auto",
            }}
          >
            <div style={{ fontWeight: 600, marginBottom: 6 }}>
              {t("previewTitle", { valid: preview.valid_count, invalid: preview.invalid_count })}
            </div>
            {preview.invalid.length > 0 && (
              <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                {preview.invalid.slice(0, 8).map((it) => (
                  <div key={`${it.case_id}-${it.reason}`} style={{ fontFamily: "var(--mono)", fontSize: 11.5, color: "var(--ink-2)" }}>
                    case_id={it.case_id} · {t(`reason.${it.reason}` as never)}
                  </div>
                ))}
                {preview.invalid.length > 8 && (
                  <div style={{ color: "var(--ink-3)" }}>… {t("more", { n: preview.invalid.length - 8 })}</div>
                )}
              </div>
            )}
          </div>
        )}

        {/* API error */}
        {apiError && (
          <div role="alert" style={{ fontSize: 11.5, color: "var(--err)" }}>
            {apiError}
          </div>
        )}

        {/* Footer */}
        <footer style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: "auto" }}>
          <button type="button" className="btn sm ghost" onClick={onClose}>
            {t("cancel")}
          </button>
          <button
            type="button"
            className="btn sm"
            onClick={onValidate}
            disabled={parsed.caseIds.length === 0 || overLimit || previewMut.isPending}
          >
            {previewMut.isPending ? t("validating") : t("validate", { n: parsed.caseIds.length })}
          </button>
          <button
            type="button"
            className="btn sm primary"
            onClick={onConfirm}
            disabled={!preview || preview.valid_count === 0 || enqueueMut.isPending}
          >
            {enqueueMut.isPending
              ? t("enqueuing")
              : t("confirm", { n: preview?.valid_count ?? 0 })}
          </button>
        </footer>
      </div>
    </>
  );
}
