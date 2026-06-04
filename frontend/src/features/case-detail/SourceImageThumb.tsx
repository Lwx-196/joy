import { useTranslation } from "react-i18next";

import { caseFileUrl, type SkillImageMetadata } from "../../api";
import { Ico } from "../../components/atoms";
import type { SourceRole, SourceViewKey } from "./types";

type SourceImageThumbProps = {
  name: string;
  role: SourceRole;
  caseId: number;
  meta: SkillImageMetadata | undefined;
  view: SourceViewKey;
  isManual: boolean;
  needsManual: boolean;
  selected: boolean;
  trashPending: boolean;
  onToggleSelection: (name: string) => void;
  onEdit: (target: { filename: string; anchor: HTMLElement }) => void;
  onTrash: (name: string) => void;
};

function viewLabel(view: SourceViewKey, t: ReturnType<typeof useTranslation<"caseDetail">>["t"]): string {
  if (view === "front") return t("images.viewFront");
  if (view === "oblique") return t("images.viewOblique");
  if (view === "side") return t("images.viewSide");
  return "";
}

export function SourceImageThumb({
  name,
  role,
  caseId,
  meta,
  view,
  isManual,
  needsManual,
  selected,
  trashPending,
  onToggleSelection,
  onEdit,
  onTrash,
}: SourceImageThumbProps) {
  const { t } = useTranslation("caseDetail");
  const hasView = view !== "unknown";
  const viewText = hasView ? viewLabel(view, t) : "";
  const phaseTxt =
    role === "pre" ? t("images.preOp") : role === "post" ? t("images.postOp") : t("images.unlabeled");
  const rejection = meta?.rejection_reason ? `\n${t("images.rejectionTitle")}: ${meta.rejection_reason}` : "";
  const isManualPhase = meta?.phase_override_source === "manual";
  const isManualView = meta?.view_override_source === "manual";
  const reviewState = meta?.review_state ?? null;

  return (
    <div
      className={`thumb${selected ? " selected" : ""}`}
      draggable
      data-source-file={name}
      data-view={hasView ? view : ""}
      data-manual={isManual ? "1" : "0"}
      data-needs-manual={needsManual ? "1" : "0"}
      data-review-excluded={reviewState?.render_excluded ? "1" : "0"}
      data-selected={selected ? "1" : "0"}
      onDragStart={(e) => {
        e.dataTransfer.effectAllowed = "copy";
        e.dataTransfer.setData("application/x-case-image", name);
        e.dataTransfer.setData("text/plain", name);
      }}
      title={t("images.thumbnailTitle", {
        name,
        phase: phaseTxt,
        view: viewText || t("images.viewUnknown"),
        rejection,
      })}
      style={{ position: "relative" }}
    >
      <button
        type="button"
        className="thumb-select-btn"
        aria-label={selected ? t("images.unselectImage") : t("images.selectImage")}
        title={selected ? t("images.unselectImage") : t("images.selectImage")}
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          onToggleSelection(name);
        }}
      >
        {selected ? <Ico name="check" size={11} /> : null}
      </button>
      <a
        href={caseFileUrl(caseId, name)}
        target="_blank"
        rel="noreferrer"
        style={{ display: "block", color: "inherit" }}
      >
        <img src={caseFileUrl(caseId, name)} alt={name} loading="lazy" />
      </a>
      <span className={`role ${role}`}>
        {role === "pre" ? "PRE" : role === "post" ? "POST" : "UNL"}
        {isManualPhase && (
          <span
            data-testid="phase-manual-marker"
            style={{ marginLeft: 3, fontSize: 8, opacity: 0.85 }}
            title={t("images.manualBadge")}
          >
            ✎
          </span>
        )}
      </span>
      {hasView && (
        <span className={`view ${view}`}>
          {viewText}
          {isManualView && (
            <span
              data-testid="view-manual-marker"
              style={{ marginLeft: 2, fontSize: 8 }}
              title={t("images.manualBadge")}
            >
              ✎
            </span>
          )}
        </span>
      )}
      {meta?.rejection_reason && (
        <span className="reject" title={meta.rejection_reason}>
          {meta.rejection_reason}
        </span>
      )}
      {needsManual && (
        <span className="class-state" title={t("images.needsManual")}>
          {t("images.needsManualShort")}
        </span>
      )}
      {reviewState && (
        <span
          className={`review-state ${reviewState.verdict ?? (reviewState.copied_requires_review ? "copied-review" : "")}`}
          title={reviewState.note || reviewState.label || reviewState.verdict || ""}
        >
          {reviewState.label || (reviewState.verdict ? t(`preflight.reviewVerdicts.${reviewState.verdict}`, { defaultValue: reviewState.verdict }) : "待确认")}
        </span>
      )}
      <button
        type="button"
        className="thumb-edit-btn"
        data-testid="thumb-edit-btn"
        aria-label={t("images.editOverride")}
        title={t("images.editOverride")}
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          onEdit({ filename: name, anchor: e.currentTarget });
        }}
      >
        <Ico name="edit" size={11} />
      </button>
      <button
        type="button"
        className="thumb-trash-btn"
        data-testid="thumb-trash-btn"
        aria-label={t("trash.button")}
        title={t("trash.button")}
        disabled={trashPending}
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          onTrash(name);
        }}
      >
        <Ico name="x" size={11} />
      </button>
      <div className="name">{name}</div>
    </div>
  );
}
