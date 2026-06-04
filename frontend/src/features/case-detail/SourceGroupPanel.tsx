import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";

import {
  type SourceGroupCandidate,
  type SourceGroupImage,
  type SourceGroupResponse,
  type SourceGroupSlot,
} from "../../api";
import type { SourceGroupFilter } from "./types";

type SourceGroupFilterItem = {
  key: SourceGroupFilter;
  label: string;
  count: number;
};

type SourceGroupMissingFile = {
  case_id: number;
  source_title: string;
  filename: string;
};

type SourceGroupPanelProps = {
  caseId: number;
  sourceGroup: SourceGroupResponse | null;
  isLoading: boolean;
  isError: boolean;
  statusClass: string;
  missingSourceCount: number;
  filter: SourceGroupFilter;
  filterItems: SourceGroupFilterItem[];
  visibleImages: SourceGroupImage[];
  selectedImages: Set<string>;
  selectedCount: number;
  missingFiles: SourceGroupMissingFile[];
  allImages: string[];
  actionBusy: boolean;
  message: string | null;
  focusedSlot: string;
  focusedIssueCode: string;
  focusedIssueText: string;
  onFilterChange: (filter: SourceGroupFilter) => void;
  onSelectVisible: () => void;
  onClearSelection: () => void;
  onBulkOverride: (kind: "phase" | "view", value: string) => void;
  onReviewSelected: (verdict: "usable" | "deferred" | "needs_repick" | "excluded" | "reopen") => void;
  onClearLock: (slot: SourceGroupSlot) => void;
  onAcceptWarning: (slot: SourceGroupSlot, code: string, message?: string) => void;
  onLockPair: (slot: SourceGroupSlot, before: SourceGroupCandidate, after: SourceGroupCandidate) => void;
  onToggleSelection: (image: SourceGroupImage) => void;
  onApplyOverride: (image: SourceGroupImage, kind: "phase" | "view", value: string) => void;
  onReviewImage: (image: SourceGroupImage, verdict: "usable" | "deferred" | "excluded") => void;
};

const imageKey = (image: SourceGroupImage): string => `${image.case_id}::${image.filename}`;

function sourceKindLabel(kind?: string | null): string {
  if (kind === "ready_source") return "源图配齐";
  if (kind === "missing_before_after_pair") return "缺术前/术后配对";
  if (kind === "insufficient_source_photos") return "真实源图不足";
  if (kind === "generated_output_collection") return "成品集合";
  if (kind === "manual_not_case_source_directory") return "素材归档";
  if (kind === "missing_source_files") return "源文件缺失";
  if (kind === "unknown_not_scanned") return "未扫描";
  return kind || "未知";
}

function pairQualityLabel(label?: string | null): string {
  if (label === "strong") return "候选稳";
  if (label === "review") return "需复核";
  if (label === "risky") return "高风险";
  return "未评分";
}

function candidateLine(candidate: NonNullable<SourceGroupSlot["selected_before"]> | null, role: "前" | "后"): string {
  return candidate ? `${role} #${candidate.case_id} ${candidate.filename} · ${candidate.selection_score}` : `${role} 未选`;
}

export function SourceGroupPanel({
  caseId,
  sourceGroup,
  isLoading,
  isError,
  statusClass,
  missingSourceCount,
  filter,
  filterItems,
  visibleImages,
  selectedImages,
  selectedCount,
  missingFiles,
  allImages,
  actionBusy,
  message,
  focusedSlot,
  focusedIssueCode,
  focusedIssueText,
  onFilterChange,
  onSelectVisible,
  onClearSelection,
  onBulkOverride,
  onReviewSelected,
  onClearLock,
  onAcceptWarning,
  onLockPair,
  onToggleSelection,
  onApplyOverride,
  onReviewImage,
}: SourceGroupPanelProps) {
  const { t } = useTranslation("caseDetail");
  const visibleKeys = new Set(visibleImages.map(imageKey));
  const sourceGroupPhaseLabel = (phase: SourceGroupImage["phase"]): string =>
    phase === "before" ? t("images.preOp") : phase === "after" ? t("images.postOp") : t("images.unlabeled");
  const sourceGroupViewLabel = (view: SourceGroupImage["view"]): string =>
    view === "front"
      ? t("images.viewFront")
      : view === "oblique"
        ? t("images.viewOblique")
        : view === "side"
          ? t("images.viewSide")
          : t("images.viewUnknown");
  const isFocusedSlot = (slot: SourceGroupSlot): boolean => focusedSlot === slot.view;

  return (
    <section className="source-group-panel" id="source-group-preflight" data-testid="source-group-panel">
      <div className="source-group-head">
        <div>
          <b>绑定源组</b>
          <span>
            {sourceGroup
              ? `${sourceGroup.source_count} 个目录 / ${sourceGroup.image_count} 张源图`
              : isLoading
                ? "正在读取真实来源目录…"
                : "未读取"}
          </span>
        </div>
        <span className={`preflight-state ${statusClass}`}>
          {sourceGroup
            ? sourceGroup.preflight.status === "ready"
              ? "预检 ready"
              : sourceGroup.preflight.status === "blocked"
                ? "硬门禁阻断"
                : "需复检"
            : "加载中"}
        </span>
      </div>
      {isError && <div className="empty">绑定源组加载失败</div>}
      {sourceGroup && (
        <>
          <div className="source-group-metrics">
            <span>主目录 #{sourceGroup.case_id}</span>
            <span>绑定 {sourceGroup.bound_case_ids.length}</span>
            <span>术前 {sourceGroup.effective_source_profile.before_count}</span>
            <span>术后 {sourceGroup.effective_source_profile.after_count}</span>
            <span>待补 {sourceGroup.preflight.needs_manual_count}</span>
            <span>已排除 {sourceGroup.preflight.render_excluded_count}</span>
            <span>ready 分 {sourceGroup.preflight.readiness_score ?? "—"}</span>
            <span>正式候选 {sourceGroup.preflight.formal_candidate_manifest?.selected_count ?? 0}/6</span>
            {missingSourceCount > 0 && <span>文件缺失 {missingSourceCount}</span>}
          </div>
          {sourceGroup.preflight.hard_blockers?.length ? (
            <div className="source-group-warnings">
              {sourceGroup.preflight.hard_blockers.slice(0, 4).map((blocker) => (
                <span key={blocker.code}>
                  {blocker.message} · {blocker.recommended_action}
                </span>
              ))}
            </div>
          ) : (
            <div className="source-group-audit">
              <span>正式候选 manifest</span>
              <b>{sourceGroup.preflight.formal_candidate_manifest?.policy ?? "source_selection_v1"} · 三联槽位已闭环</b>
            </div>
          )}
          {(focusedSlot || (sourceGroup.preflight.accepted_warnings?.length ?? 0) > 0) && (
            <div className="source-group-audit">
              <span>质检闭环</span>
              <b>
                {focusedSlot
                  ? `正在处理 ${sourceGroupViewLabel(focusedSlot as SourceGroupSlot["view"])} · ${focusedIssueCode || "issue"}`
                  : `已确认可接受 ${sourceGroup.preflight.accepted_warnings?.length ?? 0} 条`}
              </b>
            </div>
          )}
          <div className="source-group-filter-row" data-testid="source-group-filter-row">
            {filterItems.map((item) => (
              <button
                key={item.key}
                type="button"
                className={`source-group-filter-btn ${filter === item.key ? "active" : ""}`}
                onClick={() => onFilterChange(item.key)}
              >
                <span>{item.label}</span>
                <b>{item.count}</b>
              </button>
            ))}
          </div>
          <div className="source-group-bulk-bar" data-testid="source-group-bulk-bar">
            <span>已选 {selectedCount}</span>
            <button
              type="button"
              className="btn sm"
              onClick={onSelectVisible}
              disabled={visibleImages.length === 0 || actionBusy}
            >
              选择当前
            </button>
            <button
              type="button"
              className="btn sm ghost"
              onClick={onClearSelection}
              disabled={selectedCount === 0 || actionBusy}
            >
              清空
            </button>
            <button type="button" className="btn sm" onClick={() => onBulkOverride("phase", "before")} disabled={selectedCount === 0 || actionBusy}>批设术前</button>
            <button type="button" className="btn sm" onClick={() => onBulkOverride("phase", "after")} disabled={selectedCount === 0 || actionBusy}>批设术后</button>
            <button type="button" className="btn sm ghost" onClick={() => onBulkOverride("phase", "clear")} disabled={selectedCount === 0 || actionBusy}>清阶段</button>
            <button type="button" className="btn sm" onClick={() => onBulkOverride("view", "front")} disabled={selectedCount === 0 || actionBusy}>批设正面</button>
            <button type="button" className="btn sm" onClick={() => onBulkOverride("view", "oblique")} disabled={selectedCount === 0 || actionBusy}>批设45°</button>
            <button type="button" className="btn sm" onClick={() => onBulkOverride("view", "side")} disabled={selectedCount === 0 || actionBusy}>批设侧面</button>
            <button type="button" className="btn sm ghost" onClick={() => onBulkOverride("view", "clear")} disabled={selectedCount === 0 || actionBusy}>清角度</button>
            <button type="button" className="btn sm" onClick={() => onReviewSelected("usable")} disabled={selectedCount === 0 || actionBusy}>可用</button>
            <button type="button" className="btn sm ghost" onClick={() => onReviewSelected("deferred")} disabled={selectedCount === 0 || actionBusy}>低优先</button>
            <button type="button" className="btn sm ghost" onClick={() => onReviewSelected("needs_repick")} disabled={selectedCount === 0 || actionBusy}>需换片</button>
            <button type="button" className="btn sm danger" onClick={() => onReviewSelected("excluded")} disabled={selectedCount === 0 || actionBusy}>排除出图</button>
            <button type="button" className="btn sm ghost" onClick={() => onReviewSelected("reopen")} disabled={selectedCount === 0 || actionBusy}>重开</button>
          </div>
          {sourceGroup.audit.binding_note && (
            <div className="source-group-audit">
              <span>绑定备注</span>
              <b>{sourceGroup.audit.binding_note}</b>
            </div>
          )}
          <div className="source-group-slots">
            {sourceGroup.preflight.slots.map((slot) => {
              const manifestSlot = sourceGroup.preflight.formal_candidate_manifest?.slots?.[slot.view];
              const qualityPrediction = manifestSlot?.quality_prediction;
              return (
                <div key={slot.view} className={`source-group-slot ${slot.ready ? "ready" : "blocked"} ${isFocusedSlot(slot) ? "focused" : ""}`}>
                  <span>{slot.label}</span>
                  <b>{slot.before_count}/{slot.after_count}</b>
                  <em>
                    {slot.source_case_ids.length > 0
                      ? slot.source_case_ids.map((sourceCaseId) => `#${sourceCaseId}`).join(" / ")
                      : "未配"}
                  </em>
                  <div className="source-group-candidates">
                    <span title={slot.selected_before ? `${slot.selected_before.case_title} / ${slot.selected_before.filename} / ${slot.selected_before.selection_reasons.join("、")}` : ""}>
                      {candidateLine(slot.selected_before, "前")}
                    </span>
                    <span title={slot.selected_after ? `${slot.selected_after.case_title} / ${slot.selected_after.filename} / ${slot.selected_after.selection_reasons.join("、")}` : ""}>
                      {candidateLine(slot.selected_after, "后")}
                    </span>
                  </div>
                  {(slot.selected_before || slot.selected_after) && (
                    <div className="source-group-selection-reasons">
                      {slot.selected_before?.selection_reasons?.[0] && <span>前：{slot.selected_before.selection_reasons[0]}</span>}
                      {slot.selected_after?.selection_reasons?.[0] && <span>后：{slot.selected_after.selection_reasons[0]}</span>}
                    </div>
                  )}
                  {slot.pair_quality && (
                    <div className={`source-group-pair ${slot.pair_quality.severity}`}>
                      <b>{slot.pair_quality.score}</b>
                      <span>{pairQualityLabel(slot.pair_quality.label)}</span>
                      <em>{slot.pair_quality.reasons[0] ?? "首选候选已配齐"}</em>
                    </div>
                  )}
                  {qualityPrediction && (
                    <div className="source-group-selection-reasons">
                      <span>
                        出图预测：{qualityPrediction.decision === "render" ? "入选" : qualityPrediction.decision === "drop" ? "降级移除" : "阻断"}
                        {qualityPrediction.pair_score != null ? ` · pair ${qualityPrediction.pair_score}` : ""}
                      </span>
                      <span>{qualityPrediction.recommended_action}</span>
                    </div>
                  )}
                  {slot.pair_quality?.warnings?.length ? (
                    <div className="source-group-pair-warnings">
                      {slot.pair_quality.warnings.slice(0, 2).map((warning) => (
                        <span key={`${slot.view}-${warning.code}`}>{warning.message}</span>
                      ))}
                    </div>
                  ) : null}
                  {slot.selection_lock?.locked && (
                    <div className="source-group-lock-note">
                      <span>已锁片 · {slot.selection_lock.reviewer ?? "operator"}</span>
                      <button type="button" className="btn sm ghost" onClick={() => onClearLock(slot)} disabled={actionBusy}>解除</button>
                    </div>
                  )}
                  {isFocusedSlot(slot) && focusedIssueCode && (
                    <div className="source-group-lock-note">
                      <span>来自质检：{focusedIssueText || focusedIssueCode}</span>
                      <button
                        type="button"
                        className="btn sm"
                        onClick={() => onAcceptWarning(slot, focusedIssueCode, focusedIssueText)}
                        disabled={actionBusy}
                      >
                        确认可接受
                      </button>
                    </div>
                  )}
                  {slot.selected_before && slot.selected_after && (
                    <div className="source-group-lock-panel">
                      <div>
                        <b>候选重选</b>
                        <span>当前入选可锁定；也可从备选组合里改锁片</span>
                      </div>
                      <button
                        type="button"
                        className="btn sm"
                        onClick={() => onLockPair(slot, slot.selected_before!, slot.selected_after!)}
                        disabled={actionBusy}
                      >
                        锁定当前
                      </button>
                      <div className="source-group-pair-grid">
                        {slot.before_candidates.slice(0, 3).flatMap((before) =>
                          slot.after_candidates.slice(0, 3).map((after) => (
                            <button
                              key={`${slot.view}-${before.case_id}-${before.filename}-${after.case_id}-${after.filename}`}
                              type="button"
                              className="source-group-pair-option"
                              title={`前：${before.selection_reasons.join("、")} / 后：${after.selection_reasons.join("、")}`}
                              onClick={() => onLockPair(slot, before, after)}
                              disabled={actionBusy}
                            >
                              <span>#{before.case_id} {before.filename}</span>
                              <span>#{after.case_id} {after.filename}</span>
                            </button>
                          )),
                        )}
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
          {(sourceGroup.preflight.needs_manual_count > 0 || sourceGroup.preflight.missing_slots.length > 0) && (
            <div className="source-group-warnings">
              {sourceGroup.preflight.missing_slots.slice(0, 3).map((slot) => (
                <span key={slot.view}>
                  {slot.label} 缺 {slot.missing.map((role) => role === "before" ? "术前" : "术后").join(" / ")}
                </span>
              ))}
              {sourceGroup.preflight.needs_manual_count > 0 && (
                <span>{sourceGroup.preflight.needs_manual_count} 张需要补阶段/角度</span>
              )}
            </div>
          )}
          {sourceGroup.missing_bound_case_ids.length > 0 && (
            <div className="source-group-warnings">
              <span>绑定目录已失效：{sourceGroup.missing_bound_case_ids.map((sourceCaseId) => `#${sourceCaseId}`).join("、")}</span>
            </div>
          )}
          {filter === "missing_file" && (
            <div className="source-group-missing-files">
              {missingFiles.length > 0
                ? missingFiles.map((item) => (
                    <span key={`${item.case_id}-${item.filename}`}>
                      #{item.case_id} {item.source_title} / {item.filename}
                    </span>
                  ))
                : <span>当前源组没有缺失文件</span>}
            </div>
          )}
          <div className="source-group-source-list">
            {sourceGroup.sources.map((source) => {
              const images = source.images.filter((image) => visibleKeys.has(imageKey(image)));
              return (
                <article key={`${source.role}-${source.case_id}`} className="source-group-source">
                  <div className="source-group-source-head">
                    <div>
                      <b>
                        {source.role === "primary" ? "主目录" : "绑定目录"} #{source.case_id} · {source.case_title}
                      </b>
                      <span title={source.abs_path}>{source.abs_path}</span>
                    </div>
                    <div>
                      <span>{sourceKindLabel(source.source_profile.source_kind)}</span>
                      {source.missing_image_count > 0 && <span>缺失 {source.missing_image_count}</span>}
                      {source.case_id !== caseId && <Link to={`/cases/${source.case_id}`}>打开</Link>}
                    </div>
                  </div>
                  <div className="source-group-grid">
                    {images.map((image) => {
                      const selected = selectedImages.has(imageKey(image));
                      return (
                        <div
                          key={`${image.case_id}-${image.filename}`}
                          className={`source-group-card ${image.render_excluded ? "excluded" : ""} ${selected ? "selected" : ""}`}
                          draggable={image.case_id === caseId && allImages.includes(image.filename)}
                          data-source-file={image.filename}
                          onDragStart={(e) => {
                            if (image.case_id !== caseId || !allImages.includes(image.filename)) {
                              e.preventDefault();
                              return;
                            }
                            e.dataTransfer.effectAllowed = "copy";
                            e.dataTransfer.setData("application/x-case-image", image.filename);
                            e.dataTransfer.setData("text/plain", image.filename);
                          }}
                          title={
                            image.case_id === caseId && allImages.includes(image.filename)
                              ? "可拖到右侧『人工整理与出图』槽位"
                              : "互补目录或非当前 case 的图，无法拖到出图槽"
                          }
                        >
                          <button
                            type="button"
                            className="source-group-select-toggle"
                            onClick={() => onToggleSelection(image)}
                            aria-pressed={selected}
                            disabled={actionBusy}
                          >
                            {selected ? "已选" : "选择"}
                          </button>
                          <a href={image.preview_url} target="_blank" rel="noreferrer">
                            <img src={image.preview_url} alt={image.filename} loading="lazy" />
                          </a>
                          <div className="source-group-card-body">
                            <b title={image.filename}>{image.filename}</b>
                            <div className="source-group-tags">
                              <span className={image.phase === "before" ? "pre" : image.phase === "after" ? "post" : ""}>
                                {sourceGroupPhaseLabel(image.phase)}
                                {image.phase_source === "manual" ? " / 人工" : image.phase_source === "directory" ? " / 目录" : ""}
                              </span>
                              <span className={image.view ?? ""}>
                                {sourceGroupViewLabel(image.view)}
                                {image.view_source === "manual" ? " / 人工" : ""}
                              </span>
                              {image.review_state?.label && <span>{image.review_state.label}</span>}
                            </div>
                            <div className="source-group-card-quality">
                              <span>{image.manual ? "人工整理优先" : "自动识别"}</span>
                              {image.angle_confidence != null && <span>角度 {Math.round(image.angle_confidence * 100)}</span>}
                              {image.rejection_reason === "face_detection_failure" && <span className="warn">面检复核</span>}
                            </div>
                            <div className="source-group-controls">
                              <select
                                value={image.phase ?? ""}
                                onChange={(e) => onApplyOverride(image, "phase", e.target.value)}
                                disabled={actionBusy}
                                aria-label="源组阶段"
                              >
                                <option value="">阶段</option>
                                <option value="before">术前</option>
                                <option value="after">术后</option>
                              </select>
                              <select
                                value={image.view ?? ""}
                                onChange={(e) => onApplyOverride(image, "view", e.target.value)}
                                disabled={actionBusy}
                                aria-label="源组角度"
                              >
                                <option value="">角度</option>
                                <option value="front">正面</option>
                                <option value="oblique">45°</option>
                                <option value="side">侧面</option>
                              </select>
                            </div>
                            <div className="source-group-actions">
                              <button
                                type="button"
                                className="btn sm"
                                onClick={() => onReviewImage(image, "usable")}
                                disabled={actionBusy}
                              >
                                可用
                              </button>
                              <button
                                type="button"
                                className="btn sm ghost"
                                onClick={() => onReviewImage(image, "deferred")}
                                disabled={actionBusy}
                              >
                                低优先
                              </button>
                              <button
                                type="button"
                                className="btn sm danger"
                                onClick={() => onReviewImage(image, "excluded")}
                                disabled={actionBusy}
                              >
                                排除
                              </button>
                            </div>
                          </div>
                        </div>
                      );
                    })}
                    {images.length === 0 && (
                      <div className="empty">
                        {source.images.length === 0 ? "该目录没有可用于正式出图的真实源图" : "当前筛选没有可整理图片"}
                      </div>
                    )}
                  </div>
                </article>
              );
            })}
          </div>
          {message && <div className="source-group-message">{message}</div>}
        </>
      )}
    </section>
  );
}
